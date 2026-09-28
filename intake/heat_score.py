"""
DHYAAN VOICE BRAIN — heat_score.py   (Layer 2 of the 3-layer rating system)
===========================================================================
Computes a lead's INTENT HEAT (hot | warm | cold) from a completed call's
extracted data. Pure, side-effect-free `compute_heat(result)` so the Catcher can
call it the moment a result lands, PLUS a `--scan` runner to (re)score every
existing voicebrain.call_results row.

THE 3 LAYERS (this file is Layer 2):
  Layer 1  call_quality   — the call itself (good|partial|junk), set elsewhere.
  Layer 2  heat_score     — THIS FILE: how hot the lead's intent is.
  Layer 3  manual_rating  — Captain's 1-5 stars (dashboard /rate).

HEAT RULES (deliberately simple + easy to tweak — edit the token sets / the
compute_heat branches below; nothing else depends on the internals):
  HOT  = call_outcome == 'verified' AND has budget AND has location
         AND possession reads near-term / move-in.
  WARM = verified but only partial details, OR an investment / future timeline,
         OR a callback was requested with at least one concrete detail.
  COLD = explicitly not interested, vague, or too thin to act on.
  None = nothing to score yet (no outcome at all) — a neutral empty state.

ISOLATION: reads/writes voicebrain.* only — never realestate_db.private.
Does NOT import or fork clean_leads.
"""
import os
import sys
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB, VERIFY_MIN_DURATION_SEC  # noqa: E402  (psycopg2 lazy -> --dry-run is DB-free)
# ONE slot-hygiene authority — a placeholder/garbage slot must NOT count as detail.
from intake.slots import is_real_value  # noqa: E402

# --- tweakable vocabulary ----------------------------------------------------
# Possession text that signals the lead wants in SOON (drives HOT).
NEAR_TERM_TOKENS = (
    "ready", "ready to move", "rtm", "move-in", "move in", "movein",
    "immediate", "immediately", "now", "available", "ready possession",
    "possession ready", "shifting", "urgent",
)
# Possession / timeline text that signals a slower, investment horizon (drives WARM).
INVESTMENT_TOKENS = (
    "investment", "invest", "under construction", "underconstruction",
    "launch", "pre-launch", "prelaunch", "future", "later", "next year",
    "2 year", "3 year", "2-3", "1 year", "long term", "long-term",
)
VAGUE_TOKENS = ("vague", "not sure", "dont know", "don't know", "na", "n/a", "-")

# --- SQL (constants so the isolation guard below can scan them) --------------
SQL_SELECT_RESULTS = (
    "SELECT id, call_outcome, interested, budget, location, possession, "
    "configuration, property_type, carpet_area, duration_sec "
    "FROM voicebrain.call_results"
)
SQL_UPDATE_HEAT = (
    "UPDATE voicebrain.call_results SET heat_score = %s WHERE id = %s"
)

# ISOLATION GUARD — this module must NEVER reference the portal's schema.
for _sql in (SQL_SELECT_RESULTS, SQL_UPDATE_HEAT):
    _low = _sql.lower()
    assert "private" not in _low and "realestate_db" not in _low, (
        "[HEAT] ISOLATION VIOLATION - SQL references a forbidden schema/db"
    )
    assert "voicebrain." in _low, "[HEAT] SQL must be voicebrain-qualified"


# --- helpers ----------------------------------------------------------------
def _clean(v) -> str:
    """Lower-cased, stripped string; '' for None/empty/'nan'."""
    if v is None:
        return ""
    s = str(v).strip().lower()
    return "" if s in ("", "nan", "none", "null") else s


def _has(v) -> bool:
    """True if a field carries usable, non-vague content."""
    s = _clean(v)
    return bool(s) and s not in VAGUE_TOKENS


def _matches(text: str, tokens) -> bool:
    return any(tok in text for tok in tokens)


def _is_near_term(possession: str) -> bool:
    return _matches(possession, NEAR_TERM_TOKENS)


def _is_investment(possession: str) -> bool:
    return _matches(possession, INVESTMENT_TOKENS)


# --- THE SCORER (pure function — import this in the Catcher) -----------------
def compute_heat(result: dict):
    """Return 'hot' | 'warm' | 'cold' | None for a call_results-shaped dict.

    Heat reflects REAL engagement + CLEAN detail — NOT call duration alone and NOT
    a fabricated outcome. Keys read (all optional, all defensive): interested,
    budget, location, configuration, property_type, carpet_area, possession,
    duration_sec, call_outcome (negative signal only).

      HOT  = a real exchange with a fully-qualified requirement wanting in soon
             (clean budget AND clean location AND near-term possession).
      WARM = a real exchange with at least one clean requirement slot.
      COLD = explicitly not interested, OR detail too thin, OR just a blip
             (some detail but too short to be a genuine exchange).
      None = nothing to score yet (no detail, no talk-time, no outcome).
    """
    interested = result.get("interested")
    outcome = _clean(result.get("call_outcome"))

    # Explicit "not interested" / DNC always wins -> cold.
    if interested is False or outcome in (
            "not_interested", "not interested", "do_not_contact",
            "do not contact", "dnc", "declined", "refused"):
        return "cold"

    budget = is_real_value(result.get("budget"))
    location = is_real_value(result.get("location"))
    has_req = budget or location or any(
        is_real_value(result.get(k))
        for k in ("configuration", "property_type", "carpet_area"))
    possession = _clean(result.get("possession"))
    near_term = _is_near_term(possession)
    try:
        dur = int(result.get("duration_sec") or 0)
    except (ValueError, TypeError):
        dur = 0

    # Nothing to score yet (no detail, no talk-time, no outcome) -> neutral.
    if not has_req and not possession and not dur and not outcome:
        return None

    # A genuine exchange = interested=yes OR enough talk-time. Detail without a
    # real exchange (a blip that mis-extracted a number) is NOT warm.
    engaged = interested is True or dur >= VERIFY_MIN_DURATION_SEC

    if has_req and engaged:
        if budget and location and near_term:
            return "hot"
        return "warm"

    # Had some signal but not a qualified, genuine lead -> cold.
    return "cold"


# --- runner: (re)score existing results -------------------------------------
def score_existing(dry_run: bool = False):
    import psycopg2
    print("=" * 56)
    print("  HEAT SCORE — SCAN call_results" + ("   [DRY RUN]" if dry_run else ""))
    print("=" * 56)
    print(f"  host   : {DB['host']}   dbname: {DB['dbname']}")
    print(f"  schema : voicebrain   (call_results only - never private)")
    print(f"  mode   : {'DRY RUN - no writes' if dry_run else 'LIVE WRITE'}")
    print("=" * 56)

    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_SELECT_RESULTS)
            rows = cur.fetchall()

        tally = {"hot": 0, "warm": 0, "cold": 0, "none": 0}
        updates = []
        for (rid, outcome, interested, budget, location, possession,
             configuration, property_type, carpet_area, duration_sec) in rows:
            heat = compute_heat({
                "call_outcome": outcome, "interested": interested,
                "budget": budget, "location": location, "possession": possession,
                "configuration": configuration, "property_type": property_type,
                "carpet_area": carpet_area, "duration_sec": duration_sec,
            })
            tally[heat or "none"] += 1
            updates.append((rid, heat))

        if not dry_run and updates:
            with conn, conn.cursor() as cur:
                for rid, heat in updates:
                    cur.execute(SQL_UPDATE_HEAT, (heat, rid))
    finally:
        conn.close()

    print(f"  results scanned : {len(rows)}")
    print(f"  HOT  : {tally['hot']}")
    print(f"  WARM : {tally['warm']}")
    print(f"  COLD : {tally['cold']}")
    print(f"  none : {tally['none']}  (nothing to score yet)")
    verb = "WOULD update" if dry_run else "updated"
    print(f"  {verb} heat_score on : {len(updates)} row(s)")
    print("=" * 56)
    return tally


def main():
    ap = argparse.ArgumentParser(
        prog="python -m intake.heat_score",
        description="Layer 2 — compute lead heat (hot/warm/cold) from call data.")
    ap.add_argument("--scan", action="store_true",
                    help="(re)score every existing voicebrain.call_results row")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --scan: show the tally, write NOTHING")
    args = ap.parse_args()
    if args.scan:
        score_existing(dry_run=args.dry_run)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
