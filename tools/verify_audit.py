"""
DHYAAN VOICE BRAIN — verified/heat AUDIT (READ-ONLY, no writes ever).

Re-evaluates every voicebrain.call_results row under the CURRENT rules
(intake.verify.is_verified + intake.heat_score.compute_heat) and compares the
result to the flag stored in the DB. Shows exactly which currently-"verified"
leads would flip to NOT verified (the false positives) and any that would newly
qualify. Pure diagnostics — it opens a read-only connection and never UPDATEs.

Run:  python -m tools.verify_audit
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB, VERIFY_MIN_DURATION_SEC   # noqa: E402
from intake.verify import is_verified            # noqa: E402
from intake.heat_score import compute_heat        # noqa: E402

# READ-ONLY SQL — asserted below so this file can NEVER mutate the ledger.
SQL = (
    "SELECT id, call_lead_id, verified, heat_score, call_outcome, interested, "
    "duration_sec, budget, location, configuration, property_type, "
    "carpet_area, possession "
    "FROM voicebrain.call_results ORDER BY id"
)
assert SQL.strip().lower().startswith("select"), "audit must be read-only"
assert "private" not in SQL.lower() and "realestate_db" not in SQL.lower()

_COLS = ("id", "call_lead_id", "verified", "heat_score", "call_outcome",
         "interested", "duration_sec", "budget", "location", "configuration",
         "property_type", "carpet_area", "possession")


def _fmt(d):
    bits = []
    for k in ("budget", "location", "configuration", "property_type",
              "carpet_area", "possession"):
        if d.get(k):
            bits.append(f"{k}={d[k]!r}")
    return ", ".join(bits) or "(no slots)"


def main():
    import psycopg2
    print("=" * 72)
    print("  VERIFIED / HEAT AUDIT  (READ-ONLY)   min_dur=%ss" % VERIFY_MIN_DURATION_SEC)
    print("=" * 72)
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL)
            rows = [dict(zip(_COLS, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    old_true = [r for r in rows if r["verified"]]
    new_flags = {r["id"]: is_verified(r) for r in rows}

    false_pos = [r for r in old_true if not new_flags[r["id"]]]     # was T, now F
    newly_true = [r for r in rows if new_flags[r["id"]] and not r["verified"]]
    stay_true = [r for r in old_true if new_flags[r["id"]]]

    print(f"  rows total            : {len(rows)}")
    print(f"  verified NOW (stored) : {len(old_true)}")
    print(f"  verified UNDER FIX    : {sum(new_flags.values())}")
    print("-" * 72)
    print(f"  FALSE POSITIVES removed (was verified -> now NOT): {len(false_pos)}")
    for r in false_pos:
        print(f"    id {r['id']:>5}  dur={r['duration_sec']}s  {_fmt(r)}")
        print(f"            reason: {_why_not(r)}")
    print("-" * 72)
    print(f"  STILL verified (correctly): {len(stay_true)}")
    for r in stay_true:
        print(f"    id {r['id']:>5}  dur={r['duration_sec']}s  {_fmt(r)}")
    print("-" * 72)
    print(f"  NEWLY verified (was NOT, now qualifies): {len(newly_true)}")
    for r in newly_true[:50]:
        print(f"    id {r['id']:>5}  dur={r['duration_sec']}s  {_fmt(r)}")
    if len(newly_true) > 50:
        print(f"    ... and {len(newly_true) - 50} more")
    print("=" * 72)
    print("  READ-ONLY — nothing was written. Use tools/reflag_verified.py --apply")
    print("  to persist these flags to the DB.")
    print("=" * 72)


def _why_not(r):
    """One-line human reason a row failed the new verified rule."""
    from intake.verify import (has_real_requirement, is_interested_yes,
                               _NEGATIVE_OUTCOMES)
    oc = (r.get("call_outcome") or "").strip().lower()
    if r.get("interested") is False or oc in _NEGATIVE_OUTCOMES:
        return "explicit negative (said no / DNC)"
    if not is_interested_yes(r):
        return (f"interested is not an explicit yes (interested={r.get('interested')!r}) "
                "— Bolna isn't sending the field yet")
    if not has_real_requirement(r):
        return "no clean requirement slot (garbage/empty)"
    return "unknown"


if __name__ == "__main__":
    main()
