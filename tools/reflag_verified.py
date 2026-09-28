"""
DHYAAN VOICE BRAIN — re-flag verified + heat on existing rows.

Recomputes intake.verify.is_verified + intake.heat_score.compute_heat for every
voicebrain.call_results row and writes the corrected `verified` / `heat_score`
values. DRY-RUN by default (prints the delta, writes NOTHING). Pass --apply to
persist. voicebrain.* only — never touches realestate_db.private.

Run (safe preview):  python -m tools.reflag_verified
Run (persist)     :  python -m tools.reflag_verified --apply
"""
import os
import sys
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB                             # noqa: E402
from intake.verify import is_verified            # noqa: E402
from intake.heat_score import compute_heat        # noqa: E402

SQL_SELECT = (
    "SELECT id, verified, heat_score, call_outcome, interested, duration_sec, "
    "budget, location, configuration, property_type, carpet_area, possession "
    "FROM voicebrain.call_results ORDER BY id"
)
SQL_UPDATE = ("UPDATE voicebrain.call_results "
              "SET verified = %s, heat_score = %s WHERE id = %s")
# ISOLATION GUARD — never the portal schema.
for _s in (SQL_SELECT, SQL_UPDATE):
    assert "private" not in _s.lower() and "realestate_db" not in _s.lower()
    assert "voicebrain." in _s.lower()

_COLS = ("id", "verified", "heat_score", "call_outcome", "interested",
         "duration_sec", "budget", "location", "configuration",
         "property_type", "carpet_area", "possession")


def main():
    ap = argparse.ArgumentParser(prog="python -m tools.reflag_verified")
    ap.add_argument("--apply", action="store_true",
                    help="persist the corrected flags (default: dry-run only)")
    args = ap.parse_args()
    import psycopg2

    print("=" * 64)
    print("  RE-FLAG verified + heat  " + ("[APPLY]" if args.apply else "[DRY RUN]"))
    print("=" * 64)
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_SELECT)
            rows = [dict(zip(_COLS, r)) for r in cur.fetchall()]

        changes = []
        for r in rows:
            nv = is_verified(r)
            nh = compute_heat(r)
            if nv != bool(r["verified"]) or nh != r["heat_score"]:
                changes.append((r["id"], nv, nh, r))

        v_gain = sum(1 for c in changes if c[1] and not c[3]["verified"])
        v_loss = sum(1 for c in changes if not c[1] and c[3]["verified"])
        print(f"  rows scanned      : {len(rows)}")
        print(f"  rows changing     : {len(changes)}")
        print(f"  verified +{v_gain}  /  -{v_loss}")
        print("-" * 64)
        for cid, nv, nh, r in changes[:40]:
            print(f"  id {cid:>5}  verified {bool(r['verified'])}->{nv}   "
                  f"heat {r['heat_score']}->{nh}")
        if len(changes) > 40:
            print(f"  ... and {len(changes) - 40} more")
        print("-" * 64)

        if not args.apply:
            print("  DRY RUN — nothing written. Re-run with --apply to persist.")
        elif changes:
            with conn, conn.cursor() as cur:
                for cid, nv, nh, _ in changes:
                    cur.execute(SQL_UPDATE, (nv, nh, cid))
            print(f"  APPLIED — updated {len(changes)} row(s).")
        else:
            print("  Nothing to change.")
    finally:
        conn.close()
    print("=" * 64)


if __name__ == "__main__":
    main()
