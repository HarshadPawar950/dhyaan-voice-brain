"""
DHYAAN VOICE BRAIN — auto-verify rule (THE single source of truth).

A call result is "verified" (a real qualified lead worth a human follow-up) ONLY
when BOTH of these hold:

  1. EXPLICIT INTEREST — Bolna's structured `interested` field is TRUE / "yes".
     Nothing else counts as interest: not a fabricated outcome, not talk-time,
     not "the call connected". If interested is not an explicit yes -> NOT verified.

  2. REAL REQUIREMENT — at least one concrete requirement slot carries a CLEAN,
     usable value (budget OR location OR configuration — plus the commercial
     equivalents property_type / carpet_area). Garbage / placeholder / blank
     values do NOT count (see intake.slots.is_real_value).

  ...AND the lead did NOT say no — an explicit negative (interested == False, or
  call_outcome in {not_interested, do_not_contact, no_answer, dnc, declined})
  ALWAYS wins and kills verification, whatever else was captured.

Why this rule: Bolna currently returns NO `interested` boolean and does not
reliably return `call_outcome`, and the old catcher FABRICATED
call_outcome='verified' whenever ANY slot was present — so a 13-second call with a
mis-heard "ninety" looked verified. call_outcome is now used ONLY as a NEGATIVE
signal. A lead verifies exclusively on EXPLICIT interest + a CLEAN requirement.

IMPORTANT (source data): with the current Bolna agent config, `interested` is
never sent, so NOTHING verifies — correct, and exactly why the 7 structured
extraction fields (branch, budget, location, configuration, possession,
interested, call_outcome) must be configured in Bolna. Once `interested` arrives
as a real boolean, genuine leads verify automatically.

Defined ONCE here and imported by the Catcher (sets the flag at save time) and the
backfill/audit tools — never forked, so the DB flag and the dashboard never drift.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from intake.slots import is_real_value          # noqa: E402

# Concrete "what the lead wants" — a human can act on any one of these.
# (possession alone is NOT a requirement: "investment" tells us nothing to act on.)
REQUIREMENT_SLOTS = ("budget", "location", "configuration",
                     "property_type", "carpet_area")

# Outcomes that mean the lead said NO — never verified, whatever was captured.
_NEGATIVE_OUTCOMES = {"not_interested", "not interested", "do_not_contact",
                      "do not contact", "dnc", "no_answer", "no-answer",
                      "declined", "refused"}

_TRUE = {"true", "yes", "1", "y", "interested"}


def has_real_requirement(d: dict) -> bool:
    """True if any requirement slot carries a clean, usable value."""
    return any(is_real_value(d.get(k)) for k in REQUIREMENT_SLOTS)


def is_interested_yes(d: dict) -> bool:
    """True only if Bolna's structured `interested` field is an explicit yes."""
    interested = d.get("interested")
    if interested is True:
        return True
    if interested is None:
        return False
    return str(interested).strip().lower() in _TRUE


def is_verified(d: dict) -> bool:
    """d is a flat result dict (the catcher's parsed row, or a DB row mapped to
    the same keys). Defensive: missing keys are fine, blanks/garbage don't count.

    Verified  <=>  interested == yes  AND  a clean requirement  AND  no explicit no.
    """
    if not isinstance(d, dict):
        return False

    outcome = (d.get("call_outcome") or "").strip().lower()

    # 1) Explicit NO always kills it (interested=False or a negative outcome).
    if d.get("interested") is False:
        return False
    if outcome in _NEGATIVE_OUTCOMES:
        return False

    # 2) Must be EXPLICITLY interested — the only accepted interest signal.
    if not is_interested_yes(d):
        return False

    # 3) Must carry a REAL requirement (clean value, not garbage/placeholder).
    return has_real_requirement(d)
