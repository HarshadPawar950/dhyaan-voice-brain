"""
DHYAAN VOICE BRAIN — slot value hygiene (THE single source of truth).

Bolna's post-call extraction frequently returns PLACEHOLDER / garbage text for a
slot the lead never actually answered — "unknown", "not specified", "N/A", or a
stray ASR fragment. Storing those as if they were a real budget / location is
exactly what made leads look "verified" when they weren't. This module is the ONE
place that decides whether an extracted slot value is a REAL, usable answer.

Imported by:
  - catcher/server.py   -> store NULL instead of garbage (clean fields only)
  - intake/verify.py    -> the verified rule (needs a REAL requirement)
  - intake/heat_score.py-> the heat rule (score real detail, not placeholders)
so all three agree and never drift. voicebrain.* only; pure string hygiene,
no DB, no side effects, no forks.
"""

# Exact-match junk: common Bolna / ASR placeholders that mean "the lead did not
# actually answer this slot". Compared case-insensitively against the trimmed
# value. Keep additions here — this is the single list everyone reads.
GARBAGE_TOKENS = {
    "", "-", "--", "---", "—", ".", "..", "...",
    "n/a", "na", "n.a", "n.a.", "none", "null", "nil", "nan",
    "unknown", "unspecified", "not specified", "not mentioned",
    "not provided", "not given", "not available", "not applicable",
    "not sure", "not decided", "not yet decided", "undecided", "unclear",
    "no answer", "no response", "no idea", "no data", "no comment",
    "don't know", "dont know", "do not know", "idk", "tbd",
    "declined", "refused", "not disclosed", "did not specify",
    "customer did not specify", "no preference", "any", "anything",
    "whatever", "n a",
}


def is_real_value(v) -> bool:
    """True only if v is a genuine, usable slot answer.

    Rejects None, blanks, known placeholder tokens, pure-punctuation values, and
    absurdly long strings (a transcript / call-summary that leaked into a slot).
    """
    if v is None:
        return False
    s = str(v).strip().lower()
    if not s or s in GARBAGE_TOKENS:
        return False
    if not any(ch.isalnum() for ch in s):      # pure punctuation / dashes
        return False
    if len(s) > 120:                            # a transcript/summary leaked in
        return False
    return True


def clean_value(v):
    """Return v unchanged if it is a real answer, else None — so callers store
    NOTHING over garbage rather than fabricating a value."""
    return v if is_real_value(v) else None
