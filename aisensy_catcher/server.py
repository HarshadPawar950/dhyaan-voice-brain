"""
DHYAAN VOICE BRAIN — THE AISENSY REPLY CATCHER  (the keystone)
==============================================================
Sibling to catcher/server.py (the Bolna catcher), same hardened discipline.
This is the piece that CLOSES THE FUNNEL LOOP. When a lead replies to the
WhatsApp blast (taps Interested / Not Interested, or sends free text / STOP),
AiSensy POSTs us a webhook. We catch it and AUTO-ROUTE the lead:

  any reply (Interested / a category / Call Me / free text)
        -> whatsapp_replied=true, funnel_stage='human_call'   (warm -> human)
  "Not Interested"
        -> whatsapp_replied=true, funnel_stage='closed', outcome='no_interest'
  "STOP" / unsubscribe
        -> whatsapp_replied=true, funnel_stage='closed', dnc=true
           + added to voicebrain.dnc_master   (permanent opt-out, Gate 4)

THE KEY RULE ENFORCER: anyone who replies LEAVES the 'whatsapp' stage. The dialer
only ever dials 'whatsapp'-stage leads, so Stage 4 (AI call) NEVER calls a
replier. This webhook is what makes that true automatically.

HARD RULES (mirrored from the Bolna catcher):
  1. Save the RAW payload to logs/aisensy_webhooks.jsonl FIRST, before parsing.
     Raw is the source of truth; if parsing explodes the payload is still safe.
  2. NEVER return a 5xx (or 4xx) to AiSensy. Always 200 {"received": true} so
     AiSensy doesn't retry-storm us. Errors are logged, never surfaced.
  3. DEFENSIVE parsing — AiSensy key names/nesting vary. We deep-search for the
     phone and the reply text across common shapes and keep raw_payload.
  4. SAVEPOINT-safe DB writes; voicebrain.* ONLY — never realestate_db.private.

REUSE, NEVER FORK:
  - intake.clean_leads.normalize_phone  (phone -> E.164, matches stored leads)
  - intake.dnc_filter.SQL_INSERT_DNC    (the exact DNC insert, for STOP)

Run:     uvicorn aisensy_catcher.server:app --host 0.0.0.0 --port 5007
Tunnel:  cloudflared tunnel --url http://localhost:5007   (or) ngrok http 5007
Webhook URL to give AiSensy:  https://<your-public-host>/webhook/aisensy
"""
import os
import sys
import json
import logging

# Ensure logs/ exists BEFORE logging opens a FileHandler there, else the catcher
# crashes on import and the "save raw FIRST" guarantee breaks.
os.makedirs("logs", exist_ok=True)

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
import psycopg2  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DB  # noqa: E402
# REUSE the proven helpers — never fork them.
from intake.clean_leads import normalize_phone  # noqa: E402
from intake.dnc_filter import SQL_INSERT_DNC     # noqa: E402

DEFAULT_PORT = int(os.getenv("AISENSY_CATCHER_PORT", "5007"))  # not 3000/5005/5006
RAW_LOG = "logs/aisensy_webhooks.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AISENSY-CATCHER] %(message)s",
    handlers=[logging.FileHandler("logs/aisensy_catcher.log"),
              logging.StreamHandler()],
)
log = logging.getLogger("aisensy_catcher")

app = FastAPI(title="Dhyaan Voice Brain — AiSensy Reply Catcher")

# --- candidate keys for the defensive deep-search ---------------------------
PHONE_KEYS = {
    "sender", "from", "mobile", "wa_id", "waid", "phone", "phone_number",
    "phonenumber", "contact_number", "contactnumber", "msisdn", "number",
}
# reply-text candidates, searched in PRIORITY ORDER (button label beats stray text)
TEXT_KEY_GROUPS = (
    {"button_text", "buttontext", "button_payload"},
    {"title"},                                   # interactive button_reply.title
    {"body", "text", "message_text", "caption", "reply", "response"},
)

# --- routing SQL (constants so the isolation guard below can scan them) ------
SQL_SELECT_LEADS = (
    "SELECT id, funnel_stage FROM voicebrain.call_leads "
    "WHERE phone = %s AND deleted_at IS NULL ORDER BY id"
)
# Two variants per route: WITH reply columns (post-007) and WITHOUT (pre-007,
# fallback so routing still works before the migration is applied).
SQL_ROUTE = {
    "warm": {
        "with": ("UPDATE voicebrain.call_leads SET whatsapp_replied=true, "
                 "funnel_stage='human_call', reply_text=%s, reply_at=now() "
                 "WHERE id = ANY(%s)"),
        "no":   ("UPDATE voicebrain.call_leads SET whatsapp_replied=true, "
                 "funnel_stage='human_call' WHERE id = ANY(%s)"),
    },
    "not_interested": {
        "with": ("UPDATE voicebrain.call_leads SET whatsapp_replied=true, "
                 "funnel_stage='closed', outcome='no_interest', "
                 "reply_text=%s, reply_at=now() WHERE id = ANY(%s)"),
        "no":   ("UPDATE voicebrain.call_leads SET whatsapp_replied=true, "
                 "funnel_stage='closed', outcome='no_interest' "
                 "WHERE id = ANY(%s)"),
    },
    "stop": {
        "with": ("UPDATE voicebrain.call_leads SET whatsapp_replied=true, "
                 "funnel_stage='closed', dnc=true, reply_text=%s, reply_at=now() "
                 "WHERE id = ANY(%s)"),
        "no":   ("UPDATE voicebrain.call_leads SET whatsapp_replied=true, "
                 "funnel_stage='closed', dnc=true WHERE id = ANY(%s)"),
    },
}

# ISOLATION GUARD — this module must NEVER reference the portal's schema.
_ALL_SQL = [SQL_SELECT_LEADS, SQL_INSERT_DNC] + [
    v[k] for v in SQL_ROUTE.values() for k in ("with", "no")
]
for _sql in _ALL_SQL:
    _low = _sql.lower()
    assert "private" not in _low and "realestate_db" not in _low, (
        "[AISENSY-CATCHER] ISOLATION VIOLATION — SQL references a forbidden schema/db"
    )
    assert "voicebrain." in _low, "[AISENSY-CATCHER] SQL must be voicebrain-qualified"


# --- defensive parsing ------------------------------------------------------
def _deep_find(obj, keys, _depth=0):
    """Return the first non-empty scalar value whose key (case-insensitive) is in
    `keys`, searching nested dicts/lists. AiSensy nests differently per event, so
    we search rather than assume a fixed path."""
    if _depth > 8:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keys and isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        for v in obj.values():
            found = _deep_find(v, keys, _depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, keys, _depth + 1)
            if found:
                return found
    return None


def extract_phone(payload: dict):
    """Best-effort sender phone, then normalised to E.164 (+91…) to match leads."""
    raw = _deep_find(payload, PHONE_KEYS)
    return raw, normalize_phone(raw) if raw else None


def extract_reply_text(payload: dict):
    """Best-effort reply text — button label first, then interactive title, then
    a plain text body. None if the payload carries no readable text."""
    for group in TEXT_KEY_GROUPS:
        val = _deep_find(payload, group)
        if val:
            return val
    return None


def classify(reply_text: str) -> str:
    """Map the reply to a route: 'stop' | 'not_interested' | 'warm'.
    An inbound with NO readable text still counts as a reply -> 'warm' (a human
    should look). Order matters: STOP and a clear 'No' are checked before warm."""
    t = (reply_text or "").strip().lower()
    if not t:
        return "warm"
    tokens = set(t.replace(",", " ").replace(".", " ").split())
    if "stop" in tokens or "unsubscribe" in t or "opt out" in t or "optout" in t:
        return "stop"
    if "not interested" in t or "notinterested" in t or "not_interested" in t \
            or tokens == {"no"}:
        return "not_interested"
    return "warm"


# --- routing (the auto-split) -----------------------------------------------
def route_reply(phone_e164: str, reply_text: str, category: str) -> dict:
    """Apply the funnel route to EVERY live lead with this phone. SAVEPOINT-safe:
    if the reply-text columns aren't there yet (pre-007) the per-lead UPDATE rolls
    back to its savepoint and re-runs flag-only so routing still happens. For
    'stop' the phone is also added to dnc_master (permanent opt-out)."""
    conn = psycopg2.connect(**DB)
    summary = {"phone": phone_e164, "category": category,
               "matched": 0, "ids": [], "reply_stored": False, "dnc_added": False}
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_SELECT_LEADS, (phone_e164,))
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            summary["matched"] = len(ids)
            summary["ids"] = ids

            if not ids:
                conn.rollback()
                return summary

            tmpl = SQL_ROUTE[category]
            # try WITH reply columns; fall back to flag-only on any failure.
            try:
                cur.execute("SAVEPOINT sp_route")
                cur.execute(tmpl["with"], (reply_text, ids))
                cur.execute("RELEASE SAVEPOINT sp_route")
                summary["reply_stored"] = True
            except Exception as e:
                log.warning("reply_text columns unavailable (run 007?) — "
                            "flag-only route: %s", e)
                cur.execute("ROLLBACK TO SAVEPOINT sp_route")
                cur.execute(tmpl["no"], (ids,))

            # STOP -> permanent block list (reuse dnc_filter's exact insert).
            if category == "stop":
                try:
                    cur.execute("SAVEPOINT sp_dnc")
                    cur.execute(SQL_INSERT_DNC,
                                (phone_e164, "whatsapp STOP", "aisensy_webhook"))
                    cur.execute("RELEASE SAVEPOINT sp_dnc")
                    summary["dnc_added"] = cur.rowcount == 1
                except Exception as e:
                    log.warning("dnc_master insert failed (lead still flagged "
                                "dnc=true): %s", e)
                    cur.execute("ROLLBACK TO SAVEPOINT sp_dnc")
        conn.commit()
    finally:
        conn.close()
    return summary


# --- routes -----------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "alive", "service": "voice-brain-aisensy-catcher"}


@app.post("/webhook/aisensy")
async def aisensy_webhook(request: Request):
    # 1) Save raw bytes FIRST — before any parsing. Raw is the source of truth.
    raw = await request.body()
    try:
        with open(RAW_LOG, "ab") as f:
            f.write(raw + b"\n")
    except Exception as e:
        # even logging must not 5xx back to AiSensy.
        log.error("Failed to write raw log (continuing): %s", e)
    log.info("Reply webhook received (%d bytes): %s", len(raw), raw[:300])

    # 2) Parse JSON. If invalid, log and STILL return 200 — raw is safe.
    try:
        payload = json.loads(raw)
    except Exception as e:
        log.error("Webhook body was not valid JSON (raw is safe): %s", e)
        return {"received": True}

    # 3) Extract + route. NEVER 5xx back — we already saved raw.
    try:
        raw_phone, phone_e164 = extract_phone(payload)
        reply_text = extract_reply_text(payload)
        category = classify(reply_text)

        if not phone_e164:
            log.warning("No usable phone in payload (raw_sender=%r) — logged only, "
                        "no routing.", raw_phone)
            return {"received": True}

        result = route_reply(phone_e164, reply_text, category)
        log.info("Routed %s reply=%r -> %s | matched=%d ids=%s reply_stored=%s "
                 "dnc_added=%s",
                 phone_e164, reply_text, category, result["matched"],
                 result["ids"], result["reply_stored"], result["dnc_added"])
        if result["matched"] == 0:
            log.warning("Reply from %s matched NO stored lead (logged only).",
                        phone_e164)
    except Exception as e:
        log.error("Parse/route failed (raw is safe): %s", e)

    return {"received": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("aisensy_catcher.server:app", host="0.0.0.0", port=DEFAULT_PORT)
