"""
DHYAAN VOICE BRAIN — INTAKE / clean_leads.py
=============================================
Takes a RAW lead CSV (messy headers OK), produces a Bolna-ready batch and,
unless --dry-run, registers the leads in the Ledger under a new campaign.

Compliance is built in: a lead WITHOUT a consent_source value never reaches
the batch — it goes to a rejects file. Invalid phones are dropped. We dedupe
both within the file AND against what is already stored in voicebrain.call_leads.

Pipeline per row:
  1. normalise phone -> E.164 (+91 default). Invalid  -> reject (invalid_phone)
  2. require consent_source.            Missing      -> reject (no_consent)
  3. duplicate phone within this file.               -> drop   (dup_in_file)
  4. duplicate phone already in the DB (live only).  -> drop   (dup_in_db)
  -> survivor: written to the batch CSV and inserted into call_leads.

Outputs (data/processed/):
  <stem>_batch.csv     header: contact_number,first_name   (E.164, Bolna rule)
  <stem>_rejects.csv   every dropped row + reason           (audit trail)

CLI:
  python -m intake.clean_leads --file data/inbound/x.csv --campaign "Meta Jun26"
  python -m intake.clean_leads --file data/inbound/x.csv --campaign "Meta Jun26" --dry-run

--dry-run does ALL cleaning, prints the summary, and writes the batch + rejects
CSVs, but does NOT connect to Postgres (no campaign row, no call_leads insert,
no dedupe-against-DB).
"""
import os
import sys
import argparse

import pandas as pd
import phonenumbers

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB  # noqa: E402  (DB dict only; psycopg2 imported lazily below)

PROCESSED_DIR = "data/processed"
DEFAULT_REGION = "IN"

# --- flexible column mapping ------------------------------------------------
# Lower-cased, stripped header -> role. First matching header per role wins.
NAME_KEYS = {
    "name", "first_name", "firstname", "full_name", "fullname",
    "lead_name", "customer", "customer_name", "contact_name",
}
PHONE_KEYS = {
    "phone", "phone_number", "phonenumber", "mobile", "mobile_number",
    "number", "contact", "contact_number", "contactnumber", "cell", "msisdn",
}
CONSENT_KEYS = {
    "consent_source", "consent", "source", "lead_source", "opt_in", "optin",
    "consent_proof", "origin", "channel",
}


def _norm_header(h: str) -> str:
    return str(h).strip().lower().replace(" ", "_")


def resolve_columns(df: pd.DataFrame, consent_optional: bool = False) -> dict:
    """Map the dataframe's real headers to our roles, or fail loud.

    consent_optional=True (set when a --consent-source stamp is supplied on the
    CLI) drops consent_source from the required set: a file with no consent
    column is then allowed, and the stamp value is applied to every row by the
    caller. With no stamp (default) a consent column is still mandatory.
    """
    lookup = {_norm_header(c): c for c in df.columns}
    roles = {}
    for role, keys in (("phone", PHONE_KEYS),
                       ("consent_source", CONSENT_KEYS),
                       ("name", NAME_KEYS)):
        match = next((lookup[k] for k in lookup if k in keys), None)
        if match:
            roles[role] = match

    required = ["phone"] if consent_optional else ["phone", "consent_source"]
    missing = [r for r in required if r not in roles]
    if missing:
        raise SystemExit(
            "[INTAKE] FATAL — could not find required column(s): "
            + ", ".join(missing) + "\n"
            f"  Headers seen in file: {list(df.columns)}\n"
            "  Expected one of:\n"
            f"    phone          -> {sorted(PHONE_KEYS)}\n"
            f"    consent_source -> {sorted(CONSENT_KEYS)}\n"
            "  Rename a column in your CSV to one of the above and re-run."
        )
    # name is optional — Bolna batch first_name can be blank.
    return roles


# --- cleaning helpers -------------------------------------------------------
def normalize_phone(raw, region: str = DEFAULT_REGION):
    """Return E.164 (+91…) string, or None if unparseable/invalid."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        num = phonenumbers.parse(s, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def first_name_of(raw) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return ""
    return s.split()[0]


# --- DB (imported lazily so --dry-run never needs psycopg2) -----------------
def existing_phones() -> set:
    import psycopg2
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT phone FROM voicebrain.call_leads "
                "WHERE deleted_at IS NULL"
            )
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def insert_campaign_and_leads(campaign_name, source_label, survivors) -> tuple:
    import psycopg2
    conn = psycopg2.connect(**DB)
    try:
        inserted = 0
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO voicebrain.campaigns (name, source, total_leads) "
                "VALUES (%s, %s, %s) RETURNING id",
                (campaign_name, source_label, len(survivors)),
            )
            campaign_id = cur.fetchone()[0]
            for s in survivors:
                cur.execute(
                    "INSERT INTO voicebrain.call_leads "
                    "  (campaign_id, phone, first_name, consent_source) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (campaign_id, phone) DO NOTHING",
                    (campaign_id, s["phone"], s["first_name"], s["consent_source"]),
                )
                inserted += cur.rowcount
        return campaign_id, inserted
    finally:
        conn.close()


# --- main pipeline ----------------------------------------------------------
def process(file_path, campaign, dry_run=False, region=DEFAULT_REGION,
            consent_source=None):
    if not os.path.isfile(file_path):
        raise SystemExit(f"[INTAKE] FATAL — file not found: {file_path}")

    stamp = str(consent_source).strip() if consent_source else ""

    df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
    cols = resolve_columns(df, consent_optional=bool(stamp))
    p_col = cols["phone"]
    c_col = cols.get("consent_source")  # may be None when a stamp is supplied
    n_col = cols.get("name")

    print(f"[INTAKE] file={file_path}")
    consent_desc = f"'{c_col}'" if c_col else f"(none — stamp '{stamp}')"
    if stamp and c_col:
        consent_desc = f"'{c_col}' (blanks stamped '{stamp}')"
    print(f"[INTAKE] column map -> phone='{p_col}', consent={consent_desc}, "
          f"name='{n_col or '(none)'}'")

    db_phones = set()
    if not dry_run:
        db_phones = existing_phones()
        print(f"[INTAKE] {len(db_phones)} phone(s) already in voicebrain.call_leads")
    else:
        print("[INTAKE] DRY RUN — skipping DB dedupe and all Postgres writes")

    total = len(df)
    survivors, rejects = [], []
    seen_in_file = set()

    for _, row in df.iterrows():
        raw_phone = row.get(p_col, "")
        consent = str(row.get(c_col, "")).strip() if c_col else ""
        if (not consent or consent.lower() == "nan") and stamp:
            consent = stamp
        name_raw = row.get(n_col, "") if n_col else ""

        e164 = normalize_phone(raw_phone, region)
        if not e164:
            rejects.append({"name": name_raw, "phone_raw": raw_phone,
                            "consent_source": consent, "reason": "invalid_phone"})
            continue
        if not consent or consent.lower() == "nan":
            rejects.append({"name": name_raw, "phone_raw": raw_phone,
                            "consent_source": consent, "reason": "no_consent"})
            continue
        if e164 in seen_in_file:
            rejects.append({"name": name_raw, "phone_raw": raw_phone,
                            "consent_source": consent, "reason": "dup_in_file"})
            continue
        if e164 in db_phones:
            rejects.append({"name": name_raw, "phone_raw": raw_phone,
                            "consent_source": consent, "reason": "dup_in_db"})
            continue

        seen_in_file.add(e164)
        survivors.append({"phone": e164,
                          "first_name": first_name_of(name_raw),
                          "consent_source": consent})

    # --- write outputs ------------------------------------------------------
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    batch_path = os.path.join(PROCESSED_DIR, f"{stem}_batch.csv")
    rejects_path = os.path.join(PROCESSED_DIR, f"{stem}_rejects.csv")

    # Bolna rule: phone column header MUST be 'contact_number'.
    pd.DataFrame(
        [{"contact_number": s["phone"], "first_name": s["first_name"]}
         for s in survivors],
        columns=["contact_number", "first_name"],
    ).to_csv(batch_path, index=False)

    pd.DataFrame(
        rejects, columns=["name", "phone_raw", "consent_source", "reason"],
    ).to_csv(rejects_path, index=False)

    # --- DB write (live only) ----------------------------------------------
    campaign_id, inserted = None, 0
    if not dry_run and survivors:
        # source label = most common consent_source among survivors
        labels = [s["consent_source"] for s in survivors]
        source_label = max(set(labels), key=labels.count)
        campaign_id, inserted = insert_campaign_and_leads(
            campaign, source_label, survivors)

    # --- conservation guard -------------------------------------------------
    # Every input row is either a survivor or a reject — nothing is dropped by
    # a pandas op (all dedupe is an explicit rejects.append inside the loop).
    # Assert it loud, AND show it green every run so the evidence is visible.
    conserved = len(survivors) + len(rejects)
    assert conserved == total, (
        f"[INTAKE] ROW LEAK — in={total} batch={len(survivors)} "
        f"rejects={len(rejects)} (sum={conserved})"
    )

    # --- summary ------------------------------------------------------------
    n_invalid = sum(1 for r in rejects if r["reason"] == "invalid_phone")
    n_noconsent = sum(1 for r in rejects if r["reason"] == "no_consent")
    n_dupfile = sum(1 for r in rejects if r["reason"] == "dup_in_file")
    n_dupdb = sum(1 for r in rejects if r["reason"] == "dup_in_db")

    print("\n" + "=" * 48)
    print("  INTAKE SUMMARY" + ("   [DRY RUN]" if dry_run else ""))
    print("=" * 48)
    print(f"  total in            : {total}")
    print(f"  valid (E.164)       : {total - n_invalid}")
    print(f"  invalid phone       : {n_invalid}")
    print(f"  no-consent rejected : {n_noconsent}")
    print(f"  duplicates dropped  : {n_dupfile + n_dupdb}  "
          f"(in-file {n_dupfile}, in-db {n_dupdb})")
    print(f"  FINAL -> batch      : {len(survivors)}")
    print(f"  conservation        : PASS (in={total} -> "
          f"batch+rejects={conserved})")
    print("-" * 48)
    print(f"  batch CSV   : {batch_path}")
    print(f"  rejects CSV : {rejects_path}")
    if dry_run:
        print("  DB          : (dry run — nothing written)")
    else:
        print(f"  campaign_id : {campaign_id}  (leads inserted: {inserted})")
    print("=" * 48)

    return {"total": total, "final": len(survivors),
            "campaign_id": campaign_id, "inserted": inserted}


def main():
    ap = argparse.ArgumentParser(
        description="Dhyaan Voice Brain — clean a raw lead CSV into a "
                    "Bolna-ready batch and (live) register it in the Ledger.")
    ap.add_argument("--file", required=True, help="raw CSV in data/inbound/")
    ap.add_argument("--campaign", required=True, help="campaign name to create")
    ap.add_argument("--dry-run", action="store_true",
                    help="clean + write batch CSV only; never touch Postgres")
    ap.add_argument("--region", default=DEFAULT_REGION,
                    help="phonenumbers region for bare numbers (default IN)")
    ap.add_argument("--consent-source", default=None,
                    help="stamp this consent value on every row; makes the "
                         "consent column optional (use for already-consented "
                         "files with no consent column, e.g. whatsapp_enquiry)")
    args = ap.parse_args()
    process(args.file, args.campaign, dry_run=args.dry_run, region=args.region,
            consent_source=args.consent_source)


if __name__ == "__main__":
    main()
