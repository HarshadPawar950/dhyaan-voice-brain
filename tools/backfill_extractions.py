# -*- coding: utf-8 -*-
"""
DHYAAN VOICE BRAIN — extraction backfill / reconcile tool.

WHY: batch calls can land with an early/partial webhook (or a missed one), so a
result row may carry the wrong (or NULL) extraction slots. This tool re-pulls the
AUTHORITATIVE record from Bolna's GET /executions/{id}, re-parses it through the
SAME parse_bolna() the Catcher uses, re-runs the SAME is_verified() rule, and
updates voicebrain.call_results in place. No re-dialing, no forked logic.

SAFE BY DEFAULT: dry run prints a before/after table and writes NOTHING. Pass
--apply to persist. voicebrain.* ONLY — never touches realestate_db.private.

CLI:
  python -m tools.backfill_extractions --campaign-id 26                 # dry run, connected only
  python -m tools.backfill_extractions --campaign-id 26 --all           # dry run, every row w/ exec id
  python -m tools.backfill_extractions --campaign-id 26 --apply         # WRITE (connected only)
"""
import os
import sys
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv          # noqa: E402
load_dotenv()

import psycopg2                          # noqa: E402
from psycopg2.extras import Json         # noqa: E402
from config import DB                    # noqa: E402
from dialer.dialer import fetch_execution        # noqa: E402  (authoritative pull)
from catcher.server import parse_bolna            # noqa: E402  (the ONE parser)

# Columns the backfill is allowed to correct — the extraction slots + the flags
# derived purely from them. We deliberately DO NOT touch connection_status,
# recording_url, duration_sec, cost or raw_payload (the received record stays the
# source of truth); we only fix what the parser now reads correctly.
UPDATE_COLS = ("branch", "location", "budget", "configuration", "possession",
               "property_type", "carpet_area", "call_outcome", "interested",
               "call_quality", "heat_score", "verified")


def fetch_rows(cur, campaign_id, connected_only):
    where = "l.campaign_id = %s AND cr.bolna_execution_id IS NOT NULL"
    if connected_only:
        where += " AND cr.connection_status = 'connected'"
    cur.execute(f"""
        SELECT cr.id, cr.bolna_execution_id, l.phone, cr.duration_sec,
               cr.interested, cr.verified, cr.branch, cr.location, cr.budget,
               cr.configuration, cr.possession, cr.call_outcome
        FROM voicebrain.call_results cr
        JOIN voicebrain.call_leads l ON l.id = cr.call_lead_id
        WHERE {where}
        ORDER BY cr.received_at
    """, (campaign_id,))
    return cur.fetchall()


def _fmt(v):
    if v is None:
        return "-"
    s = str(v)
    return s if len(s) <= 22 else s[:19] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--all", action="store_true",
                    help="every row with an exec id (default: connected only)")
    ap.add_argument("--apply", action="store_true", help="WRITE (default: dry run)")
    args = ap.parse_args()

    mode = "APPLY (writing)" if args.apply else "DRY RUN (no writes)"
    print(f"=== BACKFILL campaign {args.campaign_id} | {mode} | "
          f"{'ALL rows' if args.all else 'CONNECTED only'} ===\n")

    conn = psycopg2.connect(**DB)
    rows = fetch_rows(conn.cursor(), args.campaign_id, not args.all)
    print(f"{len(rows)} result row(s) to reconcile.\n")

    verified_now = []
    changed = 0
    for (rid, eid, phone, dur, o_int, o_ver, o_br, o_loc, o_bud,
         o_cfg, o_pos, o_out) in rows:
        res = fetch_execution(eid)
        if not res["ok"]:
            print(f"[{rid}] {phone} dur={dur}s  FETCH FAILED: {res['error']}")
            continue
        row = parse_bolna(res["payload"])   # SAME parser as the live Catcher

        n_int, n_ver = row.get("interested"), row.get("verified")
        print(f"[{rid}] {phone}  dur={dur}s")
        print(f"    BEFORE  interested={_fmt(o_int)}  verified={_fmt(o_ver)}  "
              f"branch={_fmt(o_br)} loc={_fmt(o_loc)} budget={_fmt(o_bud)} "
              f"cfg={_fmt(o_cfg)} poss={_fmt(o_pos)} outcome={_fmt(o_out)}")
        print(f"    AFTER   interested={_fmt(n_int)}  verified={_fmt(n_ver)}  "
              f"branch={_fmt(row.get('branch'))} loc={_fmt(row.get('location'))} "
              f"budget={_fmt(row.get('budget'))} cfg={_fmt(row.get('configuration'))} "
              f"poss={_fmt(row.get('possession'))} outcome={_fmt(row.get('call_outcome'))}")
        print(f"    ==> {'VERIFIED' if n_ver else 'not verified'}\n")
        if n_ver:
            verified_now.append((rid, phone))

        if args.apply:
            vals = [row.get(c) for c in UPDATE_COLS]
            set_clause = ", ".join(f"{c} = %s" for c in UPDATE_COLS)
            with conn, conn.cursor() as cur:
                cur.execute(
                    f"UPDATE voicebrain.call_results SET {set_clause} WHERE id = %s",
                    vals + [rid],
                )
            changed += 1

    print("=" * 70)
    print(f"Reconciled {len(rows)} row(s). "
          f"{'WROTE ' + str(changed) if args.apply else 'DRY RUN — no writes'}.")
    print(f"VERIFIED after backfill: {len(verified_now)}")
    for rid, phone in verified_now:
        print(f"   - result {rid}  {phone}")
    conn.close()


if __name__ == "__main__":
    main()
