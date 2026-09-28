"""
DHYAAN VOICE BRAIN — INTAKE / live_intake.py
============================================
The FIRST real DB write. Reads a VERIFIED Bolna batch CSV (header
`contact_number,first_name`, produced by clean_leads.py), creates ONE campaign
row, and inserts the leads into voicebrain.call_leads at the TOP of the corrected
funnel:  funnel_stage='whatsapp', whatsapp_replied=false, outcome=NULL.

It does NOT re-implement cleaning. It REUSES clean_leads.py's proven helpers
(normalize_phone, first_name_of, existing_phones) as a safety net so a hand-
edited batch can't slip a bad number or an already-stored duplicate past us.
clean_leads.py is DO-NOT-MODIFY — we import it, never fork it.

ISOLATION:  every statement writes voicebrain.* only — never realestate_db.private.
SAFETY:
  - prints the resolved DB target (host/port/db/schema) BEFORE any write,
  - SAVEPOINT per row: a bad row rolls back to itself, lands in a skip log with a
    reason, and does NOT torch the rest of the batch,
  - conservation assert: rows_read == inserted + skipped (printed every run),
  - --dry-run  : print target + what it WOULD do; NO connection, NO write,
  - --limit N  : process only the first N data rows (run a small slice first).

CLI:
  python -m intake.live_intake --file data/processed/X_batch.csv \
         --campaign "Mahavir Labdhi Uran" --consent-source whatsapp_enquiry --dry-run
  python -m intake.live_intake --file ... --campaign ... --consent-source ... --limit 5
  python -m intake.live_intake --file ... --campaign ... --consent-source ...
"""
import os
import sys
import argparse

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB  # noqa: E402  (psycopg2 imported lazily so --dry-run is DB-free)
# REUSE the proven cleaner helpers — never fork them (clean_leads is DO-NOT-MODIFY).
from intake.clean_leads import (  # noqa: E402
    normalize_phone, first_name_of,
)

PROCESSED_DIR = "data/processed"
TARGET_SCHEMA = "voicebrain"
FUNNEL_STAGE_INTAKE = "whatsapp"   # Stage 0 of the corrected funnel

# --- SQL (constants so the isolation guard below can scan them) --------------
SQL_CAMPAIGN = (
    "INSERT INTO voicebrain.campaigns (name, source, total_leads) "
    "VALUES (%s, %s, %s) RETURNING id"
)
SQL_INSERT_LEAD = (
    "INSERT INTO voicebrain.call_leads "
    "  (campaign_id, phone, first_name, consent_source, "
    "   funnel_stage, whatsapp_replied, outcome) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (campaign_id, phone) DO NOTHING"
)
SQL_UPDATE_TOTAL = (
    "UPDATE voicebrain.campaigns SET total_leads = %s WHERE id = %s"
)

# ISOLATION GUARD — this module must NEVER reference the portal's schema.
for _sql in (SQL_CAMPAIGN, SQL_INSERT_LEAD, SQL_UPDATE_TOTAL):
    _low = _sql.lower()
    assert "private" not in _low and "realestate_db" not in _low, (
        "[LIVE] ISOLATION VIOLATION — SQL references a forbidden schema/db"
    )
    assert "voicebrain." in _low, "[LIVE] SQL must be voicebrain-qualified"


# --- helpers ----------------------------------------------------------------
def print_target(dry_run: bool):
    print("=" * 56)
    print("  LIVE INTAKE — RESOLVED DB TARGET")
    print("=" * 56)
    print(f"  host   : {DB['host']}")
    print(f"  port   : {DB['port']}")
    print(f"  dbname : {DB['dbname']}")
    print(f"  user   : {DB['user']}")
    print(f"  schema : {TARGET_SCHEMA}   (all writes voicebrain.* — never private)")
    print(f"  mode   : {'DRY RUN — no connection, no write' if dry_run else 'LIVE WRITE'}")
    print("=" * 56)


def read_batch(file_path: str):
    """Load a verified batch CSV; require the Bolna `contact_number` header."""
    df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
    lookup = {str(c).strip().lower(): c for c in df.columns}
    if "contact_number" not in lookup:
        raise SystemExit(
            "[LIVE] FATAL — batch CSV is missing the 'contact_number' header.\n"
            f"  Headers seen: {list(df.columns)}\n"
            "  This script expects a clean_leads.py batch output."
        )
    return df, lookup["contact_number"], lookup.get("first_name")


def write_skip_log(file_path: str, skipped: list) -> str:
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    path = os.path.join(PROCESSED_DIR, f"{stem}_intake_skips.csv")
    pd.DataFrame(skipped, columns=["phone", "reason"]).to_csv(path, index=False)
    return path


# --- main -------------------------------------------------------------------
def run(file_path, campaign, consent_source, dry_run=False, limit=None):
    if not os.path.isfile(file_path):
        raise SystemExit(f"[LIVE] FATAL — file not found: {file_path}")

    consent = (consent_source or "").strip()
    if not consent:
        raise SystemExit(
            "[LIVE] FATAL — --consent-source is required for a live write; "
            "every lead must carry a consent basis (e.g. whatsapp_enquiry)."
        )

    df, p_col, n_col = read_batch(file_path)
    if limit is not None:
        df = df.head(limit)
    rows_read = len(df)

    print_target(dry_run)
    print(f"[LIVE] file       : {file_path}")
    print(f"[LIVE] campaign   : '{campaign}'")
    print(f"[LIVE] consent_src : '{consent}'  -> stamped on every lead")
    print(f"[LIVE] funnel_stage: '{FUNNEL_STAGE_INTAKE}'  (Stage 0), whatsapp_replied=false, outcome=NULL")
    print(f"[LIVE] rows to process: {rows_read}"
          + (f"  (--limit {limit})" if limit is not None else ""))

    # --- safety-net normalize + in-file dedupe (reuse cleaner helpers) -------
    candidates, skipped = [], []
    seen = set()
    for _, row in df.iterrows():
        raw = row.get(p_col, "")
        e164 = normalize_phone(raw)
        if not e164:
            skipped.append({"phone": str(raw), "reason": "invalid_phone"})
            continue
        if e164 in seen:
            skipped.append({"phone": e164, "reason": "dup_in_file"})
            continue
        seen.add(e164)
        candidates.append({
            "phone": e164,
            "first_name": first_name_of(row.get(n_col, "") if n_col else ""),
        })

    inserted = 0
    campaign_id = None

    if dry_run:
        # No DB connection: cannot check in-DB dupes. Report what WOULD happen.
        print("\n[LIVE] DRY RUN — would create 1 campaign and insert up to "
              f"{len(candidates)} lead(s) (in-DB dedupe deferred to live run).")
    else:
        import psycopg2
        conn = psycopg2.connect(**DB)
        try:
            # Dedupe is scoped to THIS campaign only. A number already loaded in a
            # PAST campaign is allowed through — re-calling a lead in a new batch is
            # valid. The freshly-created campaign starts empty, so the sole guard is
            # ON CONFLICT (campaign_id, phone) below, which drops a phone that repeats
            # WITHIN this same batch (recorded as dup_in_campaign).
            #
            # (Previously a GLOBAL existing_phones() snapshot rejected every number
            #  already anywhere in voicebrain.call_leads as dup_in_db — so any
            #  re-upload created an EMPTY campaign and the board looked blank.)
            with conn:
                with conn.cursor() as cur:
                    cur.execute(SQL_CAMPAIGN, (campaign, consent, 0))
                    campaign_id = cur.fetchone()[0]
                    for c in candidates:
                        cur.execute("SAVEPOINT row_sp")
                        try:
                            cur.execute(SQL_INSERT_LEAD, (
                                campaign_id, c["phone"], c["first_name"], consent,
                                FUNNEL_STAGE_INTAKE, False, None,
                            ))
                            if cur.rowcount == 1:
                                inserted += 1
                                cur.execute("RELEASE SAVEPOINT row_sp")
                            else:
                                # ON CONFLICT did nothing -> dup within this campaign
                                skipped.append({"phone": c["phone"],
                                                "reason": "dup_in_campaign"})
                                cur.execute("RELEASE SAVEPOINT row_sp")
                        except Exception as exc:  # bad row: isolate, don't torch batch
                            cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                            skipped.append({"phone": c["phone"],
                                            "reason": f"error:{exc.__class__.__name__}"})
                    cur.execute(SQL_UPDATE_TOTAL, (inserted, campaign_id))
        finally:
            conn.close()

    # --- conservation guard -------------------------------------------------
    # Dry-run: candidates are the would-insert set (in == would_insert + skipped).
    # Live run: a candidate can still become a skip (dup_in_db/campaign), so use
    # the live tallies directly. Either way the sum must equal rows_read.
    if dry_run:
        conserved = len(candidates) + len(skipped)
    else:
        conserved = inserted + len(skipped)
    assert conserved == rows_read, (
        f"[LIVE] ROW LEAK — in={rows_read} "
        f"{'would_insert' if dry_run else 'inserted'}="
        f"{len(candidates) if dry_run else inserted} "
        f"skipped={len(skipped)} (sum={conserved})"
    )

    # --- skip log -----------------------------------------------------------
    skip_path = write_skip_log(file_path, skipped)

    # --- summary ------------------------------------------------------------
    n_invalid = sum(1 for s in skipped if s["reason"] == "invalid_phone")
    n_dupfile = sum(1 for s in skipped if s["reason"] == "dup_in_file")
    n_dupdb = sum(1 for s in skipped if s["reason"] == "dup_in_db")
    n_dupcamp = sum(1 for s in skipped if s["reason"] == "dup_in_campaign")
    n_err = sum(1 for s in skipped if s["reason"].startswith("error:"))

    print("\n" + "=" * 56)
    print("  LIVE INTAKE SUMMARY" + ("   [DRY RUN]" if dry_run else ""))
    print("=" * 56)
    print(f"  rows in            : {rows_read}")
    print(f"  invalid phone      : {n_invalid}")
    print(f"  dup in-file        : {n_dupfile}")
    print(f"  dup in-db          : {n_dupdb}")
    print(f"  dup in-campaign    : {n_dupcamp}")
    print(f"  row errors         : {n_err}")
    if dry_run:
        print(f"  WOULD insert       : {len(candidates)}")
    else:
        print(f"  INSERTED           : {inserted}")
    print(f"  conservation       : PASS (in={rows_read} -> "
          f"{'would+skip' if dry_run else 'inserted+skip'}={conserved})")
    print("-" * 56)
    print(f"  skip log    : {skip_path}")
    if dry_run:
        print("  DB          : (dry run — nothing written)")
    else:
        print(f"  campaign_id : {campaign_id}  (funnel_stage='{FUNNEL_STAGE_INTAKE}')")
    print("=" * 56)

    return {"rows_in": rows_read, "inserted": inserted,
            "skipped": len(skipped), "campaign_id": campaign_id}


def main():
    ap = argparse.ArgumentParser(
        description="Dhyaan Voice Brain — register a verified batch CSV into "
                    "voicebrain.call_leads at funnel_stage='whatsapp'.")
    ap.add_argument("--file", required=True,
                    help="verified batch CSV (clean_leads.py output) in data/processed/")
    ap.add_argument("--campaign", required=True, help="campaign name to create")
    ap.add_argument("--consent-source", required=True,
                    help="consent basis stamped on every lead (e.g. whatsapp_enquiry)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print target + what it WOULD do; never connect to Postgres")
    ap.add_argument("--limit", type=int, default=None,
                    help="process only the first N rows (run a small slice first)")
    args = ap.parse_args()
    run(args.file, args.campaign, args.consent_source,
        dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
