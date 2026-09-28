"""
DHYAAN VOICE BRAIN - INTAKE / dnc_filter.py   (Gate 4 - permanent do-not-contact)
=================================================================================
The compliance block. A number on the DNC list is NEVER callable - once DNC,
always DNC. This tool owns the list (voicebrain.dnc_master) and enforces it on
stored leads (voicebrain.call_leads.dnc = true).

It does NOT re-implement phone cleaning. It REUSES clean_leads.py's proven
helpers (normalize_phone, PHONE_KEYS, _norm_header). clean_leads is
DO-NOT-MODIFY - we import it, never fork it.

ISOLATION:  every statement writes/reads voicebrain.* only - never private.
SAFETY:
  - prints the resolved DB target before any write,
  - --dry-run on every mode: shows what WOULD happen, makes NO DB writes,
  - never hard-deletes; un-blocking a number is a separate, reviewed action,
  - adds are idempotent (ON CONFLICT DO NOTHING) - re-adding is harmless,
  - no mode flag => prints help and does nothing (zero surprise behaviour).

MODES (exactly one per run):
  --add <PHONE>      add ONE number to the DNC list
  --add-file <CSV>   bulk-add numbers from a CSV (flexible phone column)
  --scan-db          flag every call_leads row whose phone is on the DNC list
                     (dnc=true) and write a dnc audit CSV with reasons

CLI:
  python -m intake.dnc_filter --add +919876543210 --reason "opted out"
  python -m intake.dnc_filter --add-file data/inbound/stop_requests.csv --dry-run
  python -m intake.dnc_filter --scan-db --dry-run
  python -m intake.dnc_filter --scan-db
"""
import os
import sys
import argparse
from datetime import datetime

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB  # noqa: E402  (psycopg2 imported lazily so dry-add is DB-free)
# REUSE the proven cleaner helpers - never fork them (clean_leads is DO-NOT-MODIFY).
from intake.clean_leads import (  # noqa: E402
    normalize_phone, PHONE_KEYS, _norm_header,
)

PROCESSED_DIR = "data/processed"
TARGET_SCHEMA = "voicebrain"

# --- SQL (constants so the isolation guard below can scan them) --------------
SQL_INSERT_DNC = (
    "INSERT INTO voicebrain.dnc_master (phone, reason, source) "
    "VALUES (%s, %s, %s) ON CONFLICT (phone) DO NOTHING"
)
SQL_SELECT_DNC = (
    "SELECT phone, reason FROM voicebrain.dnc_master WHERE deleted_at IS NULL"
)
SQL_SELECT_LEADS = (
    "SELECT id, phone, dnc FROM voicebrain.call_leads WHERE deleted_at IS NULL"
)
SQL_FLAG_LEAD = (
    "UPDATE voicebrain.call_leads SET dnc = true WHERE id = %s AND dnc = false"
)

# ISOLATION GUARD - this module must NEVER reference the portal's schema.
for _sql in (SQL_INSERT_DNC, SQL_SELECT_DNC, SQL_SELECT_LEADS, SQL_FLAG_LEAD):
    _low = _sql.lower()
    assert "private" not in _low and "realestate_db" not in _low, (
        "[DNC] ISOLATION VIOLATION - SQL references a forbidden schema/db"
    )
    assert "voicebrain." in _low, "[DNC] SQL must be voicebrain-qualified"


# --- helpers ----------------------------------------------------------------
def print_target(dry_run: bool, mode: str):
    print("=" * 56)
    print(f"  DNC FILTER - {mode}")
    print("=" * 56)
    print(f"  host   : {DB['host']}")
    print(f"  port   : {DB['port']}")
    print(f"  dbname : {DB['dbname']}")
    print(f"  user   : {DB['user']}")
    print(f"  schema : {TARGET_SCHEMA}   (all I/O voicebrain.* - never private)")
    print(f"  mode   : {'DRY RUN - no DB writes' if dry_run else 'LIVE WRITE'}")
    print("=" * 56)


def _connect():
    import psycopg2
    return psycopg2.connect(**DB)


def fetch_dnc() -> dict:
    """Return {phone: reason} for the ACTIVE DNC list (deleted_at IS NULL)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_SELECT_DNC)
            return {r[0]: (r[1] or "") for r in cur.fetchall()}
    finally:
        conn.close()


def dnc_phones() -> set:
    """The active DNC phone set. The Dialer imports this to exclude blocked
    numbers from any call list (contract: never dial a phone in this set)."""
    return set(fetch_dnc().keys())


def find_phone_column(df: pd.DataFrame) -> str:
    """Locate the phone column with the SAME flexible mapping as the cleaner."""
    lookup = {_norm_header(c): c for c in df.columns}
    match = next((lookup[k] for k in lookup if k in PHONE_KEYS), None)
    if not match:
        raise SystemExit(
            "[DNC] FATAL - could not find a phone column.\n"
            f"  Headers seen: {list(df.columns)}\n"
            f"  Expected one of: {sorted(PHONE_KEYS)}\n"
            "  Rename a column to one of the above and re-run."
        )
    return match


def normalized_from_file(file_path: str):
    """Read a CSV, normalise every phone, collapse in-file dups.
    Returns (rows_read, unique_valid_list, invalid_list, dup_count, phone_col)."""
    df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
    p_col = find_phone_column(df)
    seen, valid, invalid, dup = set(), [], [], 0
    for _, row in df.iterrows():
        raw = row.get(p_col, "")
        e164 = normalize_phone(raw)
        if not e164:
            invalid.append(str(raw))
            continue
        if e164 in seen:
            dup += 1
            continue
        seen.add(e164)
        valid.append(e164)
    return len(df), valid, invalid, dup, p_col


def write_audit(matched: list, dry_run: bool) -> str:
    """Write the scan-db audit trail. matched = [(lead_id, phone, reason,
    already_flagged), ...]. Written in both modes (it's a report, not a state
    mutation); the filename marks whether the DB was actually changed."""
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "dryrun" if dry_run else "live"
    path = os.path.join(PROCESSED_DIR, f"dnc_audit_{ts}_{tag}.csv")
    pd.DataFrame(
        [{"lead_id": m[0], "phone": m[1], "reason": m[2],
          "already_flagged": m[3]} for m in matched],
        columns=["lead_id", "phone", "reason", "already_flagged"],
    ).to_csv(path, index=False)
    return path


# --- mode: --add ------------------------------------------------------------
def cmd_add(phone: str, reason: str, source: str, dry_run: bool):
    e164 = normalize_phone(phone)
    if not e164:
        raise SystemExit(f"[DNC] FATAL - not a valid phone number: {phone!r}")

    print_target(dry_run, "ADD (single)")
    print(f"[DNC] normalized : {phone!r} -> {e164}")
    print(f"[DNC] reason     : '{reason}'   source: '{source}'")

    if dry_run:
        print(f"\n[DNC] DRY RUN - would add {e164}; "
              "already-blocked check deferred to live run.")
        return

    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(SQL_INSERT_DNC, (e164, reason, source))
            if cur.rowcount == 1:
                print(f"\n[DNC] ADDED {e164}")
            else:
                print(f"\n[DNC] ALREADY BLOCKED {e164} (idempotent - no change)")
    finally:
        conn.close()


# --- mode: --add-file -------------------------------------------------------
def cmd_add_file(file_path: str, reason: str, source: str, dry_run: bool):
    if not os.path.isfile(file_path):
        raise SystemExit(f"[DNC] FATAL - file not found: {file_path}")

    total, valid, invalid, dup, p_col = normalized_from_file(file_path)

    print_target(dry_run, "ADD-FILE (bulk)")
    print(f"[DNC] file       : {file_path}  (phone column: '{p_col}')")
    print(f"[DNC] reason     : '{reason}'   source: '{source}'")

    # conservation: every row is valid-unique, invalid, or an in-file dup.
    conserved = len(valid) + len(invalid) + dup
    assert conserved == total, (
        f"[DNC] ROW LEAK - in={total} valid={len(valid)} "
        f"invalid={len(invalid)} dup={dup} (sum={conserved})"
    )

    added = already = 0
    if dry_run:
        print(f"\n[DNC] DRY RUN - would add up to {len(valid)} number(s); "
              "already-blocked classification deferred to live run.")
    else:
        conn = _connect()
        try:
            with conn, conn.cursor() as cur:
                for e164 in valid:
                    cur.execute(SQL_INSERT_DNC, (e164, reason, source))
                    if cur.rowcount == 1:
                        added += 1
                    else:
                        already += 1
        finally:
            conn.close()

    print("\n" + "=" * 56)
    print("  DNC ADD-FILE SUMMARY" + ("   [DRY RUN]" if dry_run else ""))
    print("=" * 56)
    print(f"  rows in            : {total}")
    print(f"  valid (unique)     : {len(valid)}")
    print(f"  invalid phone      : {len(invalid)}")
    print(f"  dup in-file        : {dup}")
    if dry_run:
        print(f"  WOULD add          : {len(valid)}  (already-blocked TBD at live run)")
    else:
        print(f"  ADDED (new)        : {added}")
        print(f"  already blocked    : {already}")
    print(f"  conservation       : PASS (in={total} -> "
          f"valid+invalid+dup={conserved})")
    print("=" * 56)


# --- mode: --scan-db --------------------------------------------------------
def cmd_scan_db(dry_run: bool):
    print_target(dry_run, "SCAN-DB (flag leads)")

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_SELECT_DNC)
            dnc = {r[0]: (r[1] or "") for r in cur.fetchall()}
            cur.execute(SQL_SELECT_LEADS)
            leads = cur.fetchall()  # rows of (id, phone, dnc)

        print(f"[DNC] dnc_master active entries : {len(dnc)}")
        print(f"[DNC] call_leads scanned        : {len(leads)}")

        matched = []  # (lead_id, phone, reason, already_flagged)
        for lid, phone, is_dnc in leads:
            if phone in dnc:
                matched.append((lid, phone, dnc[phone], bool(is_dnc)))

        to_flag = [m for m in matched if not m[3]]
        already = [m for m in matched if m[3]]

        if not dry_run and to_flag:
            with conn, conn.cursor() as cur:
                for lid, _phone, _reason, _flag in to_flag:
                    cur.execute(SQL_FLAG_LEAD, (lid,))

        audit_path = write_audit(matched, dry_run)
    finally:
        conn.close()

    print("\n" + "=" * 56)
    print("  DNC SCAN-DB SUMMARY" + ("   [DRY RUN]" if dry_run else ""))
    print("=" * 56)
    print(f"  leads scanned      : {len(leads)}")
    print(f"  matched DNC        : {len(matched)}")
    print(f"  already flagged    : {len(already)}")
    if dry_run:
        print(f"  WOULD flag dnc=true: {len(to_flag)}")
    else:
        print(f"  FLAGGED dnc=true   : {len(to_flag)}")
    print("-" * 56)
    print(f"  audit CSV   : {audit_path}")
    if dry_run:
        print("  DB          : (dry run - no leads flagged)")
    print("=" * 56)


# --- CLI --------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m intake.dnc_filter",
        description="Dhyaan Voice Brain - Gate 4: permanent do-not-contact. "
                    "Manage the voicebrain.dnc_master block list and enforce it "
                    "on stored leads. Once DNC, always DNC.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--add", metavar="PHONE",
                      help="add ONE phone to the DNC list (normalized to E.164)")
    mode.add_argument("--add-file", metavar="CSV",
                      help="bulk-add phones from a CSV (flexible phone column)")
    mode.add_argument("--scan-db", action="store_true",
                      help="flag matching call_leads as dnc=true + write audit CSV")
    ap.add_argument("--reason", default="opted out",
                    help="reason stored with added numbers (default: 'opted out')")
    ap.add_argument("--source", default="manual",
                    help="source tag for added numbers (default: 'manual')")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what WOULD happen; make NO DB writes")
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()

    # No mode flag => print help and do nothing (deliberate, compliance-safe).
    if not (args.add or args.add_file or args.scan_db):
        ap.print_help()
        return

    if args.add:
        cmd_add(args.add, args.reason, args.source, args.dry_run)
    elif args.add_file:
        cmd_add_file(args.add_file, args.reason, args.source, args.dry_run)
    elif args.scan_db:
        cmd_scan_db(args.dry_run)


if __name__ == "__main__":
    main()
