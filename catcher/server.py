"""
DHYAAN VOICE BRAIN — THE CATCHER
Receives Bolna's POST webhook for each call, extracts the slots, scores
quality, and writes one clean row to voicebrain.call_results.

TEST FIRST (no DB needed): run this, expose with a tunnel, point Bolna's
webhook URL at it, make ONE real call, and watch the payload land.

Run:  uvicorn catcher.server:app --host 0.0.0.0 --port 5005
Tunnel (Windows): cloudflared tunnel --url http://localhost:5005
                  (or) ngrok http 5005
Then paste the public https URL into the Bolna agent's Webhook URL field.
"""
import os
import sys
import json
import logging

# Ensure logs/ exists BEFORE logging.basicConfig() opens a FileHandler there,
# otherwise the Catcher crashes on import and the "save raw FIRST" rule breaks.
os.makedirs("logs", exist_ok=True)

from fastapi import FastAPI, Request
import psycopg2
from psycopg2.extras import Json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB
# REUSE the proven heat scorer (Layer 2) — import, never fork. Lets a result
# arrive fully scored so the dashboard shows heat the instant a call lands.
from intake.heat_score import compute_heat
# REUSE the single auto-verify rule (Layer 4) — import, never fork. Lets a result
# arrive already flagged so the dashboard's Verified list + CSV export are instant.
from intake.verify import is_verified, has_real_requirement
# REUSE the ONE slot-hygiene authority — import, never fork. So the catcher stores
# NULL over Bolna's garbage/placeholder slot values instead of fabricating data.
from intake.slots import is_real_value

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CATCHER] %(message)s",
    handlers=[logging.FileHandler("logs/catcher.log"), logging.StreamHandler()],
)
log = logging.getLogger("catcher")

app = FastAPI(title="Dhyaan Voice Brain — Catcher")


def score_quality(has_requirement: bool, duration: int) -> str:
    """Cheap heuristic on the CALL itself (Layer 1). A 'good' call captured a real
    requirement AND ran long enough to be a genuine exchange; a sub-8s call is
    junk; the rest are partial. Deliberately does NOT read call_outcome — the old
    version keyed on a FABRICATED outcome=='verified' and rated blips 'good'."""
    if has_requirement and duration and duration > 20:
        return "good"
    if not duration or duration < 8:
        return "junk"
    return "partial"


def derive_connection(status: str, duration: int, recording) -> str:
    """Did the call actually CONNECT to a person? TALK-TIME is the ONLY ground
    truth. Bolna returns a recording_url for EVERY call — including busy /
    no-answer / rejected (the URL just 404s when there is no audio) — so the
    presence of a recording must NOT count as connected. We mark 'connected'
    only when duration_sec > 0; otherwise we classify WHY it didn't connect from
    the telephony status/hangup-reason string (matched as a substring, since
    Bolna phrases it like 'Call recipient was busy').
    Returns one of: connected | no-answer | failed | not-connected."""
    # 1) Real talk-time = genuinely connected. This is the ONLY 'connected' path.
    #    (recording is intentionally ignored — it is always present.)
    if duration and duration > 0:
        return "connected"
    # 2) Zero talk-time -> classify the not-connected reason from the status.
    s = (status or "").strip().lower().replace("_", "-")
    if any(k in s for k in ("no-answer", "noanswer", "missed", "unanswered",
                            "ring-no-answer")):
        return "no-answer"
    if any(k in s for k in ("busy", "failed", "error", "cancel", "rejected",
                            "declined", "invalid")):
        return "failed"
    return "not-connected"


def first_present(d: dict, *keys):
    """Return the first non-empty value among keys (defensive key-name reader)."""
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _to_bool(v):
    """Coerce Bolna's 'interested' answer (bool or "yes"/"no"/"true"...) to a
    real bool for the BOOLEAN column. Unrecognised / blank -> None (NULL)."""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "yes", "1", "y", "interested"):
        return True
    if s in ("false", "no", "0", "n", "not interested", "not_interested"):
        return False
    return None


def parse_bolna(payload: dict) -> dict:
    """
    Map Bolna's execution payload -> our flat result row.
    NOTE: exact key names vary by Bolna version. We read defensively and
    keep raw_payload so nothing is ever lost. Verify keys against your first
    real webhook.site capture, then tighten this.
    """
    # Post-call extraction variables. Our LIVE Bolna agent groups the 7 structured
    # fields under a named extraction CATEGORY called "lead data", so Bolna returns:
    #   extracted_data -> {"lead data": {branch, location, budget, configuration,
    #                                     possession, interested, call_outcome}}
    # We read that category FIRST, by name (space/underscore/case-insensitive), so
    # the fields land deterministically. Field types (parsed accordingly):
    #   pre-defined enums : branch, configuration, possession, interested,
    #                       call_outcome  (fixed values — kept as-is / coerced)
    #   free text         : location, budget  (garbage-filtered, never fabricated)
    # A GENERIC flatten then runs as a fallback for any OTHER shape Bolna might
    # send (flat {budget: ...}, or numbered {"1": {budget: {subjective: ...}}}),
    # so a config change never silently drops the data. raw_payload keeps the full
    # source of truth regardless (project rule #5). First real value wins; blanks
    # never clobber it.
    _SLOTS = {"branch", "location", "budget", "configuration", "possession",
              "property_type", "carpet_area", "call_outcome", "interested"}

    def _slot_value(v, _depth=0):
        """Scalar as-is; Bolna's dict wrapper -> its answer (subjective/value).

        Handles BOTH shapes Bolna sends:
          * single wrap  {objective/subjective/...: <answer>}      (fields grouped
            under the 'lead data' category, e.g. branch)
          * double wrap  {<field>: {objective/subjective: <answer>}}  (the SEPARATE
            top-level extraction fields — Bolna repeats the field name as the outer
            key). Only 'branch' is grouped under 'lead data'; the other 6 fields
            (location/budget/configuration/possession/interested/call_outcome) come
            back double-wrapped at the top level, so we MUST peel that extra layer
            or every one of them stores NULL. We recurse into a lone nested dict
            (bounded depth) to reach the real answer."""
        if isinstance(v, dict):
            for _kk in ("subjective", "value", "answer", "objective", "text"):
                _vv = v.get(_kk)
                if _vv not in (None, "", "null"):
                    return _vv
            # No wrapper key at this level -> Bolna double-wrapped it as
            # {field: {objective/subjective: ...}}. Peel one more layer.
            if _depth < 2:
                for _cv in v.values():
                    if isinstance(_cv, dict):
                        _got = _slot_value(_cv, _depth + 1)
                        if _got not in (None, "", "null"):
                            return _got
            return None
        return v

    ex = {}

    def _absorb(slot, v):
        slot = str(slot).strip().lower()
        if slot not in _SLOTS:
            return
        val = _slot_value(v)
        if ex.get(slot):
            return
        # interested / call_outcome are classification fields, kept raw (bool /
        # enum). The REQUIREMENT slots must be CLEAN real answers — reject Bolna's
        # "unknown"/"N/A"/placeholder garbage so we store NULL, never fabricate.
        if slot in ("interested", "call_outcome"):
            if val not in (None, "", "null"):
                ex[slot] = val
        elif is_real_value(val):
            ex[slot] = val

    def _norm(s):
        """Normalise a category/field name: lower, trim, spaces==underscores==dashes."""
        return str(s).strip().lower().replace("-", " ").replace("_", " ")

    # --- PRIMARY: read the named "lead data" category ------------------------
    for _src in ("extracted_data", "custom_extractions", "agent_extraction"):
        _sv = payload.get(_src)
        if not isinstance(_sv, dict):
            continue
        for _ck, _cv in _sv.items():
            if _norm(_ck).replace(" ", "") == "leaddata" and isinstance(_cv, dict):
                for _fk, _fv in _cv.items():           # each of the 7 fields
                    _absorb(_fk, _fv)

    # --- FALLBACK: generic flatten for any other Bolna shape -----------------
    for _src in ("agent_extraction", "custom_extractions", "extracted_data"):
        _sv = payload.get(_src)
        if not isinstance(_sv, dict):
            continue
        for _k, _v in _sv.items():
            if str(_k).strip().lower() in _SLOTS:      # FLAT: key is the slot
                _absorb(_k, _v)
            elif isinstance(_v, dict):                 # NESTED: category/numbered bucket
                for _ik, _iv in _v.items():            # inner key is the slot name
                    _absorb(_ik, _iv)
    tele = payload.get("telephony_data") or payload.get("telephony") or {}
    if not isinstance(tele, dict):
        tele = {}
    transcript = (
        payload.get("transcript")
        or payload.get("concatenated_transcript")
        or ""
    )
    # Keep Bolna's OWN classification / telephony status — do NOT fabricate.
    # The old code force-set outcome='verified' whenever ANY slot was present,
    # which is what marked 13-second blips as verified. Verification is now
    # decided by is_verified() from the CLEAN slots + real engagement, not by a
    # renamed outcome. raw_payload keeps the source of truth (project rule #5).
    outcome = ex.get("call_outcome") or payload.get("status") or "unknown"
    duration = (payload.get("conversation_duration")
                or payload.get("duration")
                or tele.get("duration") or 0)
    try:
        duration = int(duration)
    except (ValueError, TypeError):
        duration = 0

    # Telephony status drives connection; recording lives in telephony_data on
    # most Bolna versions, but we also check the top level just in case.
    tele_status = first_present(tele, "status", "call_status", "hangup_reason")
    status_raw = tele_status or payload.get("status") or ""
    recording = first_present(
        tele, "recording_url", "recording", "recording_link") \
        or first_present(payload, "recording_url", "recording", "recording_link")
    connection = derive_connection(status_raw, duration, recording)

    # Did the call capture a real, clean requirement? Drives call_quality (Layer 1)
    # and mirrors exactly what is_verified() checks — one definition, no drift.
    has_req = has_real_requirement(ex)

    # Recipient phone — lets us link a result to its lead by NUMBER when Bolna's
    # batch create didn't hand back per-recipient execution ids (so the lead has
    # no bolna_execution_id to match). Checked across telephony_data, top level,
    # and the context variables we passed in the batch CSV (contact_number).
    ctx = payload.get("context_details") or payload.get("variables") or {}
    if not isinstance(ctx, dict):
        ctx = {}
    recipient_phone = (
        first_present(tele, "to_number", "recipient_phone_number", "to", "phone")
        or first_present(payload, "to_number", "recipient_phone_number",
                         "contact_number", "phone")
        or first_present(ctx, "contact_number", "recipient_phone_number", "phone"))

    row = {
        "bolna_execution_id": payload.get("id") or payload.get("execution_id"),
        "recipient_phone": recipient_phone,
        "branch":        ex.get("branch"),
        "location":      ex.get("location"),
        "budget":        ex.get("budget"),
        "configuration": ex.get("configuration"),
        "possession":    ex.get("possession"),
        "property_type": ex.get("property_type"),
        "carpet_area":   ex.get("carpet_area"),
        "call_outcome":  outcome,
        # Bolna's pre-defined 'interested' field returns text ("yes"/"no"); the
        # DB column is BOOLEAN, so coerce here (unknown/blank -> NULL, never a
        # raw string that would break the INSERT).
        "interested":    _to_bool(ex.get("interested")),
        "call_quality":  score_quality(has_req, duration),
        "connection_status": connection,
        "recording_url": recording,
        "duration_sec":  duration,
        "cost":          payload.get("total_cost") or payload.get("cost"),
        "raw_transcript": transcript,
        "raw_payload":   payload,
    }
    # Layer 2 heat — score now so the card is complete the moment it lands.
    try:
        row["heat_score"] = compute_heat(row)
    except Exception:
        row["heat_score"] = None
    # Layer 4 auto-verify — flag a real qualified lead the instant it lands.
    try:
        row["verified"] = is_verified(row)
    except Exception:
        row["verified"] = False
    return row


def save(row: dict):
    conn = psycopg2.connect(**DB)
    try:
        with conn, conn.cursor() as cur:
            # link back to the call_lead via execution id (best effort)
            cur.execute(
                "SELECT id FROM voicebrain.call_leads WHERE bolna_execution_id = %s",
                (row["bolna_execution_id"],),
            )
            hit = cur.fetchone()
            call_lead_id = hit[0] if hit else None

            # Fallback link BY PHONE — batch create often returns no per-recipient
            # execution id, so the lead has none to match. Match the webhook's
            # recipient number to the most-recent live lead by its last 10 digits
            # (ignores +91 / formatting), then back-fill bolna_execution_id so any
            # later webhook for this call links directly.
            if call_lead_id is None and row.get("recipient_phone"):
                cur.execute(
                    "SELECT id FROM voicebrain.call_leads "
                    "WHERE deleted_at IS NULL "
                    "  AND right(regexp_replace(phone, '\\D', '', 'g'), 10) "
                    "      = right(regexp_replace(%s, '\\D', '', 'g'), 10) "
                    "ORDER BY id DESC LIMIT 1",
                    (str(row["recipient_phone"]),),
                )
                ph = cur.fetchone()
                if ph:
                    call_lead_id = ph[0]
                    if row.get("bolna_execution_id"):
                        cur.execute(
                            "UPDATE voicebrain.call_leads SET bolna_execution_id = %s "
                            "WHERE id = %s",
                            (row["bolna_execution_id"], call_lead_id),
                        )
                    log.info("Linked result to lead %s by phone %s (no exec-id "
                             "match)", call_lead_id, row["recipient_phone"])

            # Preferred insert — includes the migration-009 columns
            # (connection_status, recording_url) + heat_score (migration 005).
            # If 009 hasn't been applied yet, fall back to the legacy column set
            # so a result is NEVER lost — only connection/recording are deferred.
            full_cols = (
                "call_lead_id, bolna_execution_id, branch, location, budget, "
                "configuration, possession, property_type, carpet_area, "
                "call_outcome, interested, call_quality, connection_status, "
                "recording_url, heat_score, verified, duration_sec, cost, "
                "raw_transcript, raw_payload"
            )
            full_vals = (
                call_lead_id, row["bolna_execution_id"], row["branch"],
                row["location"], row["budget"], row["configuration"],
                row["possession"], row["property_type"], row["carpet_area"],
                row["call_outcome"], row["interested"], row["call_quality"],
                row.get("connection_status"), row.get("recording_url"),
                row.get("heat_score"), row.get("verified", False),
                row["duration_sec"], row["cost"],
                row["raw_transcript"], Json(row["raw_payload"]),
            )
            try:
                cur.execute("SAVEPOINT ins")
                cur.execute(
                    f"INSERT INTO voicebrain.call_results ({full_cols}) "
                    "VALUES (" + ",".join(["%s"] * len(full_vals)) + ")",
                    full_vals,
                )
                cur.execute("RELEASE SAVEPOINT ins")
            except psycopg2.errors.UndefinedColumn as col_exc:
                # ONLY a missing column (e.g. migration 009/005 not yet applied)
                # triggers the fallback — any OTHER DB error must propagate so we
                # never silently drop connection_status on a transient failure.
                # The fallback is the MINIMAL base-schema insert (no heat_score,
                # no connection/recording) so a result is NEVER lost even if 005
                # is also absent. Raw payload is already on disk regardless.
                log.error("Full insert failed (run migrations 005/009?) — minimal "
                          "base insert without connection/recording/heat: %s", col_exc)
                cur.execute("ROLLBACK TO SAVEPOINT ins")
                cur.execute(
                    """
                    INSERT INTO voicebrain.call_results
                      (call_lead_id, bolna_execution_id, branch, location, budget,
                       configuration, possession, property_type, carpet_area,
                       call_outcome, interested, call_quality,
                       duration_sec, cost, raw_transcript, raw_payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        call_lead_id, row["bolna_execution_id"], row["branch"],
                        row["location"], row["budget"], row["configuration"],
                        row["possession"], row["property_type"], row["carpet_area"],
                        row["call_outcome"], row["interested"], row["call_quality"],
                        row["duration_sec"], row["cost"],
                        row["raw_transcript"], Json(row["raw_payload"]),
                    ),
                )
            if call_lead_id:
                cur.execute(
                    "UPDATE voicebrain.call_leads SET call_status='completed' WHERE id=%s",
                    (call_lead_id,),
                )
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"status": "alive", "service": "voice-brain-catcher"}


@app.post("/webhook/bolna")
async def bolna_webhook(request: Request):
    # 1) Save raw bytes FIRST — before any parsing. Raw is the source of truth.
    #    If anything below fails we still have the exact payload on disk.
    raw = await request.body()
    with open("logs/raw_webhooks.jsonl", "ab") as f:
        f.write(raw + b"\n")
    log.info("Webhook received (%d bytes): %s", len(raw), raw[:300])

    # 2) Try to parse JSON. If it isn't valid JSON, log and STILL return 200 —
    #    never hand Bolna a 4xx/5xx; the raw bytes are already safe.
    try:
        payload = json.loads(raw)
    except Exception as e:
        log.error("Webhook body was not valid JSON (raw is safe): %s", e)
        return {"received": True}

    # 3) Parse + save into the Ledger. Still never 5xx back to Bolna.
    try:
        row = parse_bolna(payload)
        save(row)
        log.info("Saved result for execution %s (connection=%s, outcome=%s, "
                 "quality=%s, dur=%ss, rec=%s)",
                 row["bolna_execution_id"], row.get("connection_status"),
                 row["call_outcome"], row["call_quality"], row["duration_sec"],
                 "yes" if row.get("recording_url") else "no")
    except Exception as e:
        # Never 500 back to Bolna — we already saved raw. Log and move on.
        log.error("Parse/save failed (raw is safe): %s", e)

    return {"received": True}
