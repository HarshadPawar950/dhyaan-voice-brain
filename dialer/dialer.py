"""
DHYAAN VOICE BRAIN — THE DIALER
===============================
The ONLY module that talks to Bolna. Sends leads to Dhyan to be called.
Engine-agnostic by design: swap Bolna later by changing just this file.

SAFE BY DEFAULT. Every entry point is DRY-RUN unless you pass --go-live.
A dry run reads the DB read-only and prints EXACTLY what it would POST to Bolna
(payload, lead count, target numbers) — it never calls the API and never writes.

Two jobs:
  trigger_one(phone)        POST /call           — one test call
  launch_campaign(camp_id)  POST /batches        — batch-call a campaign's leads

Guards (hard blocks on a live dial):
  - CALLING WINDOW : refuses to dial outside config CALL_WINDOW_START..END.
  - MAX_ATTEMPTS   : never dials a lead whose attempts >= config MAX_ATTEMPTS;
                     increments call_leads.attempts on a live launch.
  - ENV            : BOLNA_API_KEY / BOLNA_AGENT_ID / BOLNA_FROM_PHONE must be
                     set for a LIVE call; missing -> fail loud naming the var.

Isolation: only ever reads/writes voicebrain.* — never realestate_db.private.

CLI:
  python -m dialer.dialer one      --phone +919876543210            # dry run
  python -m dialer.dialer one      --phone +919876543210 --go-live  # real call
  python -m dialer.dialer campaign --campaign-id 2                   # dry run
  python -m dialer.dialer campaign --campaign-id 2 --go-live         # real batch
"""
import os
import sys
import io
import csv
import argparse
from datetime import datetime, timedelta, timezone

# Load .env BEFORE importing config — config reads os.getenv at import time, so
# the .env values must already be in os.environ when that runs.
from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import requests  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (  # noqa: E402
    DB, BOLNA_API_KEY, BOLNA_AGENT_ID, BOLNA_AGENTS, BOLNA_BASE_URL, FROM_PHONE,
    BOLNA_FROM_PHONES,
    CALL_WINDOW_START, CALL_WINDOW_END, MAX_ATTEMPTS,
    CHUNK_SIZE, CHUNK_DELAY_SEC, RETRY_DELAY_MIN,
)

INTAKE_STAGE = "whatsapp"   # current call list = freshly intaken leads (Stage 0)
HTTP_TIMEOUT = 30


# --- guards -----------------------------------------------------------------
def require_env(live: bool, agent_id=None, from_phone=None):
    """For a LIVE dial all three must be set. For a dry run we only WARN so the
    payload can still be previewed; the message names exactly what to set.

    agent_id / from_phone default to Dhyan's globals so existing callers are
    unchanged; a named caller (Priya) passes its own resolved pair so the check
    validates the number that will ACTUALLY dial, not just Dhyan's."""
    agent_id = agent_id or BOLNA_AGENT_ID
    from_phone = from_phone or FROM_PHONE
    missing = [name for name, val in (
        ("BOLNA_API_KEY", BOLNA_API_KEY),
        ("BOLNA_AGENT_ID", agent_id),
        ("BOLNA_FROM_PHONE", from_phone),
    ) if not val]
    if not missing:
        return
    msg = ("[DIALER] missing env var(s): " + ", ".join(missing)
           + "\n  Set them in .env (gitignored): "
           + ", ".join(f"{m}=<value>" for m in missing))
    if live:
        raise SystemExit("[DIALER] FATAL — cannot place a LIVE call.\n" + msg)
    print(msg + "\n  (dry run continues with placeholders so you can eyeball "
          "the payload shape.)")


def calling_window_open():
    """True if NOW is within [CALL_WINDOW_START, CALL_WINDOW_END)."""
    hour = datetime.now().hour
    return CALL_WINDOW_START <= hour < CALL_WINDOW_END, hour


def enforce_window_or_die():
    open_now, hour = calling_window_open()
    if not open_now:
        raise SystemExit(
            f"[DIALER] BLOCKED — it is {hour:02d}:xx local; calling window is "
            f"{CALL_WINDOW_START:02d}:00–{CALL_WINDOW_END:02d}:00. Refusing to "
            "dial outside compliance hours. Try again inside the window."
        )


def _agent(name=None):
    """Resolve an agent UUID. No name -> the default (Dhyan / BOLNA_AGENT_ID).
    A name (e.g. 'priya') -> its BOLNA_AGENTS entry; hard-stop if not configured
    so we never silently dial the wrong agent."""
    if name:
        uuid = BOLNA_AGENTS.get(name.lower())
        if not uuid:
            have = ", ".join(sorted(BOLNA_AGENTS)) or "none"
            raise SystemExit(
                f"[DIALER] agent '{name}' not configured — set BOLNA_AGENT_"
                f"{name.upper()} in .env (configured now: {have})")
        return uuid
    return BOLNA_AGENT_ID or "<BOLNA_AGENT_ID not set>"


def _from():
    return FROM_PHONE or "<BOLNA_FROM_PHONE not set>"


def resolve_caller(agent=None):
    """Resolve a named caller to its (agent_id, from_phone) pair for the BATCH
    path. No name -> the default Dhyan (BOLNA_AGENT_ID + FROM_PHONE), so every
    existing call site keeps its exact behaviour.

    A named caller (e.g. 'priya') must have BOTH its agent UUID and its own
    from-phone configured — we HARD-STOP otherwise so a batch can NEVER go out on
    the wrong number or the wrong agent. This mirrors _agent()'s fail-loud rule.
    Returns (agent_id, from_phone, label)."""
    if not agent:
        return BOLNA_AGENT_ID, FROM_PHONE, "dhyan (default)"
    name = agent.lower()
    agent_id = BOLNA_AGENTS.get(name)
    if not agent_id:
        have = ", ".join(sorted(BOLNA_AGENTS)) or "none"
        raise SystemExit(
            f"[DIALER] agent '{agent}' not configured — set BOLNA_AGENT_"
            f"{name.upper()} in .env (configured now: {have})")
    from_phone = BOLNA_FROM_PHONES.get(name)
    if not from_phone:
        have = ", ".join(sorted(BOLNA_FROM_PHONES)) or "none"
        raise SystemExit(
            f"[DIALER] caller '{agent}' has NO from-phone — set BOLNA_FROM_PHONE_"
            f"{name.upper()} in .env so the batch dials from the right number "
            f"(configured now: {have}). Refusing to fall back to Dhyan's number.")
    return agent_id, from_phone, name


def _auth_header():
    return {"Authorization": f"Bearer {BOLNA_API_KEY}"}


# --- account / balance ------------------------------------------------------
def bolna_balance(timeout=8):
    """Read-only GET /user/me — returns the Bolna wallet balance + concurrency.
    The ONLY place we read account state (this module owns every Bolna call).

    NEVER raises: the dashboard calls this on every page load, so any failure
    (no key, network down, Bolna 5xx) must degrade gracefully to a dict with
    ok=False and wallet=None rather than break the board. Shape:
      {ok, wallet, currency, concurrency, error}
    Bolna's wallet figure is in rupees for our INR account; we label it ₹ in the
    UI but keep the currency key here so it's explicit and easy to change."""
    if not BOLNA_API_KEY:
        return {"ok": False, "wallet": None, "currency": "INR",
                "concurrency": None, "error": "BOLNA_API_KEY not set"}
    try:
        r = requests.get(f"{BOLNA_BASE_URL}/user/me",
                         headers=_auth_header(), timeout=timeout)
        if r.status_code >= 400:
            return {"ok": False, "wallet": None, "currency": "INR",
                    "concurrency": None,
                    "error": f"HTTP {r.status_code}: {_bolna_error(r)}"}
        j = r.json() if r.content else {}
        wallet = j.get("wallet")
        try:
            wallet = float(wallet) if wallet is not None else None
        except (TypeError, ValueError):
            wallet = None
        return {"ok": True, "wallet": wallet, "currency": "INR",
                "concurrency": j.get("concurrency"), "error": None}
    except Exception as exc:
        return {"ok": False, "wallet": None, "currency": "INR",
                "concurrency": None, "error": str(exc)}


# --- execution detail (authoritative post-call pull) ------------------------
def fetch_execution(execution_id, timeout=25):
    """Read-only GET /executions/{id} — the AUTHORITATIVE per-call record.

    Why this exists: for BATCH calls the webhook can land early / partial, and a
    missed webhook leaves a lead with no result at all. This endpoint returns the
    SAME payload shape as the webhook (id, status, transcript, extracted_data,
    telephony_data, total_cost, ...), so the Catcher's parse_bolna() reads it with
    zero changes. Used by tools/backfill_extractions.py to (re)populate + verify a
    call after the fact without re-dialing.

    NEVER raises — returns {ok, payload, error}. voicebrain.* only; pure read."""
    if not BOLNA_API_KEY:
        return {"ok": False, "payload": None, "error": "BOLNA_API_KEY not set"}
    if not execution_id:
        return {"ok": False, "payload": None, "error": "no execution_id"}
    try:
        r = requests.get(f"{BOLNA_BASE_URL}/executions/{execution_id}",
                         headers=_auth_header(), timeout=timeout)
        if r.status_code >= 400:
            return {"ok": False, "payload": None,
                    "error": f"HTTP {r.status_code}: {_bolna_error(r)}"}
        return {"ok": True, "payload": (r.json() if r.content else {}),
                "error": None}
    except Exception as exc:
        return {"ok": False, "payload": None, "error": str(exc)}


# --- trigger_one ------------------------------------------------------------
def trigger_one(phone, dry_run=True, agent=None):
    """POST /call for a single test call to one E.164 number.
    agent=None uses the default (Dhyan); agent='priya' targets that caller."""
    require_env(live=not dry_run)
    agent_id = _agent(agent)
    payload = {"agent_id": agent_id, "recipient_phone_number": phone}
    open_now, hour = calling_window_open()

    print("=" * 56)
    print("  DIALER — trigger_one" + ("   [DRY RUN]" if dry_run else "   [LIVE]"))
    print("=" * 56)
    print(f"  agent    : {agent or 'default (dhyan)'}  ({agent_id})")
    print(f"  endpoint : POST {BOLNA_BASE_URL}/call")
    print(f"  header   : Authorization: Bearer "
          f"{'<set>' if BOLNA_API_KEY else '<BOLNA_API_KEY not set>'}")
    print(f"  payload  : {payload}")
    print(f"  window   : {'OPEN' if open_now else 'CLOSED'} (now {hour:02d}:xx, "
          f"allowed {CALL_WINDOW_START:02d}–{CALL_WINDOW_END:02d})")
    print("=" * 56)

    if dry_run:
        print("[DIALER] DRY RUN — no API call made.")
        return None

    enforce_window_or_die()
    r = requests.post(f"{BOLNA_BASE_URL}/call", headers={
        **_auth_header(), "Content-Type": "application/json",
    }, json=payload, timeout=HTTP_TIMEOUT)
    print(f"[DIALER] Bolna responded {r.status_code}: {r.text[:400]}")
    r.raise_for_status()
    return r.json() if r.content else None


# --- launch_campaign --------------------------------------------------------
def fetch_callable_leads(campaign_id):
    """Read-only pull of this campaign's still-callable leads — the AI-call pool.
    Filters: live (deleted_at IS NULL), at the whatsapp stage, under MAX_ATTEMPTS,
    NOT do-not-contact (the dnc column AND the dnc_master block list — Gate 4),
    and NOT a WhatsApp replier (KEY RULE — repliers are human-bound).

    By staying at funnel_stage='whatsapp' this naturally includes BOTH groups who
    were never reached: the SILENT non-responders (WhatsApp sent OK, no reply) AND
    the FAILED sends (whatsapp_sent_at NULL, send failed) — same batch, no split.
    Repliers leave the whatsapp stage (the reply-catcher moves them to
    'human_call'); the whatsapp_replied=false guard below is belt-and-suspenders
    so a replier can NEVER be AI-called even if a stage move lagged.

    We IMPORT dnc_filter.dnc_phones(); we never fork it."""
    import psycopg2
    # DNC block list. Defensive: if dnc_master doesn't exist yet, the column
    # filter below still applies and we fall back to an empty block set.
    blocked = set()
    try:
        from intake.dnc_filter import dnc_phones
        blocked = dnc_phones()
    except Exception:
        blocked = set()
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            # The callable pool = FRESH leads (still at the whatsapp stage, never
            # AI-called) PLUS RETRY leads (a failed call requeued by
            # requeue_failed_leads -> call_status='retry_pending'). Both are gated
            # by attempts < MAX_ATTEMPTS, not-DNC, not-a-WhatsApp-replier, and are
            # never a human_call / closed lead. Guarded so a pre-011 DB (no
            # retry_pending rows) still returns exactly the fresh leads as before.
            cur.execute(
                "SELECT id, phone, first_name, attempts "
                "FROM voicebrain.call_leads "
                "WHERE campaign_id = %s AND deleted_at IS NULL "
                "  AND attempts < %s "
                "  AND COALESCE(dnc, false) = false "
                "  AND COALESCE(whatsapp_replied, false) = false "  # KEY RULE
                "  AND funnel_stage NOT IN ('human_call','closed') "
                "  AND (funnel_stage = %s OR call_status = 'retry_pending') "
                "ORDER BY id",
                (campaign_id, MAX_ATTEMPTS, INTAKE_STAGE),
            )
            rows = [{"id": r[0], "phone": r[1], "first_name": r[2] or "",
                     "attempts": r[3]} for r in cur.fetchall()]
    finally:
        conn.close()
    # belt-and-suspenders: drop anything on the dnc_master list too.
    return [r for r in rows if r["phone"] not in blocked]


def plan_campaign(campaign_id):
    """Read-only preview of what a LIVE launch WOULD call — identical filter to
    launch_campaign (stage + attempts + DNC). No API call, no DB write. The
    dashboard uses this for the confirmation modal and the dry-run path."""
    leads = fetch_callable_leads(campaign_id)
    open_now, hour = calling_window_open()
    # Read-only retry preview so the modal can show "N of these are failed-call
    # retries". find_retry_candidates never writes.
    try:
        retry_n = len(find_retry_candidates(campaign_id))
    except Exception:
        retry_n = 0
    return {
        "campaign_id": campaign_id,
        "count": len(leads),
        "leads": leads,
        "retry_candidates": retry_n,
        "window_open": open_now,
        "hour": hour,
        "window_start": CALL_WINDOW_START,
        "window_end": CALL_WINDOW_END,
    }


# --- failed-call retry / rescheduling --------------------------------------
def _retry_eligibility(conn_status, call_outcome, interested, duration):
    """Decide if a lead's LATEST call qualifies for an auto-retry.
    Returns (eligible: bool, reason: str).

    RETRY only a call that NEVER CONNECTED for a transient reason
    (no-answer / busy / network-failed / not-connected). NEVER retry:
      - a call that connected + talked (duration > 0 / connection='connected'),
      - an explicit no (interested=False, or a declined / DNC outcome).
    DNC-list membership is filtered separately (find_retry_candidates)."""
    try:
        dur = int(duration or 0)
    except (ValueError, TypeError):
        dur = 0
    cs = (conn_status or "").strip().lower()
    oc = (call_outcome or "").strip().lower()
    blob = f"{cs} {oc}"

    # 1) Connected + talked -> handled, never retry.
    if cs == "connected" or dur > 0:
        return False, "connected (talked) — not retried"
    # 2) Said no -> never retry (belongs to DNC / archive).
    if interested is False:
        return False, "said not-interested — not retried"
    _NON_RETRY = ("reject", "declin", "not_interested", "not interested",
                  "do_not_contact", "do not contact", "dnc", "opt-out",
                  "opted out", "unsubscrib", "removed")
    if any(k in blob for k in _NON_RETRY):
        return False, "declined / opted-out — not retried"
    # 3) A genuine non-connect -> retry. connection_status is ground truth;
    #    fall back to keyword-matching the raw telephony status.
    _RETRY = ("no-answer", "no answer", "noanswer", "busy", "fail", "error",
              "network", "cancel", "missed", "unanswered", "no-response",
              "no response", "not-connected", "not connected", "disconnect",
              "timeout", "ring")
    if cs in ("no-answer", "failed", "not-connected") or any(k in blob for k in _RETRY):
        return True, "never connected (no-answer/busy/failed) — retry"
    return False, "no failed-call result to retry"


def find_retry_candidates(campaign_id, min_gap_min=None):
    """READ-ONLY. Leads whose LATEST call never connected (a transient failure),
    still under MAX_ATTEMPTS, past the RETRY_DELAY_MIN cool-off, not DNC, not a
    WhatsApp replier, not already queued for retry. Returns a list of lead dicts.
    Imports dnc_phones; never forks. Guarded so a pre-009/011 DB still works."""
    import psycopg2
    gap = RETRY_DELAY_MIN if min_gap_min is None else min_gap_min
    blocked = set()
    try:
        from intake.dnc_filter import dnc_phones
        blocked = dnc_phones()
    except Exception:
        blocked = set()

    where = (
        "WHERE cl.campaign_id = %s AND cl.deleted_at IS NULL "
        "  AND cl.attempts < %s "
        "  AND COALESCE(cl.dnc, false) = false "
        "  AND COALESCE(cl.whatsapp_replied, false) = false "
        "  AND cl.funnel_stage NOT IN ('human_call','closed') "
        "  AND COALESCE(cl.call_status,'') <> 'retry_pending' "
    )
    lateral = (
        "LEFT JOIN LATERAL ("
        "  SELECT {cols} FROM voicebrain.call_results "
        "  WHERE call_lead_id = cl.id "
        "  ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 1"
        ") cr ON true "
    )
    full = (
        "SELECT cl.id, cl.phone, cl.first_name, cl.attempts, "
        "       COALESCE(cl.last_attempt_at, cr.received_at) AS last_try, "
        "       cr.connection_status, cr.call_outcome, cr.interested, cr.duration_sec "
        "FROM voicebrain.call_leads cl "
        + lateral.format(cols="connection_status, call_outcome, interested, "
                              "duration_sec, received_at")
        + where + "ORDER BY cl.id"
    )
    fallback = (
        "SELECT cl.id, cl.phone, cl.first_name, cl.attempts, "
        "       cr.received_at AS last_try, "
        "       NULL AS connection_status, cr.call_outcome, cr.interested, cr.duration_sec "
        "FROM voicebrain.call_leads cl "
        + lateral.format(cols="call_outcome, interested, duration_sec, received_at")
        + where + "ORDER BY cl.id"
    )

    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SAVEPOINT sp_rq")
                cur.execute(full, (campaign_id, MAX_ATTEMPTS))
                rows = cur.fetchall()
                cur.execute("RELEASE SAVEPOINT sp_rq")
            except psycopg2.errors.UndefinedColumn:
                # pre-009/011 DB: no connection_status / last_attempt_at columns.
                cur.execute("ROLLBACK TO SAVEPOINT sp_rq")
                cur.execute(fallback, (campaign_id, MAX_ATTEMPTS))
                rows = cur.fetchall()
    finally:
        conn.close()

    cutoff = datetime.now().astimezone() - timedelta(minutes=gap)
    cands = []
    for (lid, phone, fname, attempts, last_try, cs, oc, interested, dur) in rows:
        eligible, reason = _retry_eligibility(cs, oc, interested, dur)
        if not eligible:
            continue
        # Cool-off gate: skip if the last attempt was too recent.
        if last_try is not None:
            lt = last_try if last_try.tzinfo else last_try.astimezone()
            if lt > cutoff:
                continue
        if phone in blocked:
            continue
        cands.append({"id": lid, "phone": phone, "first_name": fname or "",
                      "attempts": attempts, "reason": reason,
                      "last_try": last_try.isoformat() if last_try else None})
    return cands


def requeue_failed_leads(campaign_id, dry_run=True, min_gap_min=None):
    """Mark eligible failed-call leads call_status='retry_pending' so the next
    launch dials them again (up to MAX_ATTEMPTS, within the window). DRY-RUN by
    default — previews the candidates and writes NOTHING. Returns a summary dict.
    This is the SINGLE source of truth for retry eligibility (never forked)."""
    cands = find_retry_candidates(campaign_id, min_gap_min)
    result = {"campaign_id": campaign_id, "candidates": len(cands),
              "requeued": 0, "dry_run": dry_run, "leads": cands}
    if not cands or dry_run:
        return result
    import psycopg2
    ids = [c["id"] for c in cands]
    conn = psycopg2.connect(**DB)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE voicebrain.call_leads SET call_status = 'retry_pending' "
                "WHERE id = ANY(%s)",
                (ids,),
            )
        result["requeued"] = len(ids)
    finally:
        conn.close()
    print(f"[DIALER] retry — requeued {len(ids)} failed lead(s) as 'retry_pending' "
          f"(campaign {campaign_id}, gap {RETRY_DELAY_MIN if min_gap_min is None else min_gap_min}m).")
    return result


def retry_board(campaign_id, min_gap_min=None):
    """READ-ONLY management view of a campaign's failed-call retry pipeline,
    bucketed by state. Reuses the SAME authorities the dialer uses — never forks:
      - find_retry_candidates() decides 'eligible now' (identical to what a run
        would dial), so the board can NEVER disagree with reality.
      - _retry_eligibility() classifies every other lead's latest call.
      - RETRY_DELAY_MIN drives the cool-off + next-eligible time.

    Buckets (each lead appears in exactly one):
      pending     — never-connected, under the cap, cool-off elapsed -> dials next.
      waiting     — never-connected, under the cap, still inside the cool-off
                    (carries next_eligible = last_try + RETRY_DELAY_MIN).
      in_progress — already queued for this/next run (call_status='retry_pending').
      exhausted   — never-connected but hit MAX_ATTEMPTS: won't retry again.
    Connected/talked, declined and DNC leads are NOT in the pipeline. voicebrain.*."""
    import psycopg2
    gap = RETRY_DELAY_MIN if min_gap_min is None else min_gap_min
    # 'eligible now' — the authoritative set (same call the run makes).
    elig_ids = {c["id"] for c in find_retry_candidates(campaign_id, min_gap_min)}

    lateral = (
        "LEFT JOIN LATERAL ("
        "  SELECT {cols} FROM voicebrain.call_results "
        "  WHERE call_lead_id = cl.id "
        "  ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 1"
        ") cr ON true "
    )
    where = ("WHERE cl.campaign_id = %s AND cl.deleted_at IS NULL "
             "  AND cl.funnel_stage NOT IN ('human_call','closed') ")
    full = (
        "SELECT cl.id, cl.phone, cl.first_name, cl.attempts, cl.call_status, "
        "       COALESCE(cl.last_attempt_at, cr.received_at) AS last_try, "
        "       cr.connection_status, cr.call_outcome, cr.interested, cr.duration_sec "
        "FROM voicebrain.call_leads cl "
        + lateral.format(cols="connection_status, call_outcome, interested, "
                              "duration_sec, received_at")
        + where + "ORDER BY cl.id"
    )
    fallback = (
        "SELECT cl.id, cl.phone, cl.first_name, cl.attempts, cl.call_status, "
        "       cr.received_at AS last_try, "
        "       NULL AS connection_status, cr.call_outcome, cr.interested, cr.duration_sec "
        "FROM voicebrain.call_leads cl "
        + lateral.format(cols="call_outcome, interested, duration_sec, received_at")
        + where + "ORDER BY cl.id"
    )
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SAVEPOINT sp_rb")
                cur.execute(full, (campaign_id,))
                rows = cur.fetchall()
                cur.execute("RELEASE SAVEPOINT sp_rb")
            except psycopg2.errors.UndefinedColumn:
                cur.execute("ROLLBACK TO SAVEPOINT sp_rb")
                cur.execute(fallback, (campaign_id,))
                rows = cur.fetchall()
    finally:
        conn.close()

    buckets = {"pending": [], "waiting": [], "in_progress": [], "exhausted": []}
    for (lid, phone, fname, attempts, cstatus, last_try, cs, oc, interested, dur) in rows:
        eligible, _reason = _retry_eligibility(cs, oc, interested, dur)
        if not eligible:
            continue  # connected/talked, declined or DNC — not a retry lead
        next_elig = None
        if last_try is not None:
            lt = last_try if last_try.tzinfo else last_try.astimezone()
            next_elig = (lt + timedelta(minutes=gap)).isoformat()
        lead = {
            "id": lid, "phone": phone, "first_name": fname or "",
            "attempts": attempts, "max_attempts": MAX_ATTEMPTS,
            "last_outcome": (cs or oc or "no-answer"),
            "last_try": last_try.isoformat() if last_try else None,
            "next_eligible": next_elig,
        }
        if attempts is not None and attempts >= MAX_ATTEMPTS:
            buckets["exhausted"].append(lead)
        elif cstatus == "retry_pending":
            buckets["in_progress"].append(lead)
        elif lid in elig_ids:
            buckets["pending"].append(lead)
        else:
            buckets["waiting"].append(lead)

    counts = {
        "pending_total": len(buckets["pending"]) + len(buckets["waiting"]),
        "eligible_now": len(buckets["pending"]),
        "waiting": len(buckets["waiting"]),
        "in_progress": len(buckets["in_progress"]),
        "exhausted": len(buckets["exhausted"]),
    }
    return {"campaign_id": campaign_id, "counts": counts, **buckets}


def build_batch_csv(leads) -> str:
    """Bolna batch CSV text: phone column header MUST be contact_number."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["contact_number", "first_name"])
    for ld in leads:
        w.writerow([ld["phone"], ld["first_name"]])
    return buf.getvalue()


def _map_executions(conn, response_json, leads):
    """Best-effort: if Bolna's batch response carries per-recipient execution
    ids, stamp them onto the matching call_leads row (matched by phone). Bolna's
    batch response shape varies by version, so this is defensive and silent if
    the data isn't present — the Catcher still links by execution id later."""
    if not isinstance(response_json, dict):
        return 0
    rows = (response_json.get("executions")
            or response_json.get("recipients")
            or response_json.get("calls") or [])
    by_phone = {ld["phone"]: ld["id"] for ld in leads}
    mapped = 0
    with conn.cursor() as cur:
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            phone = (item.get("recipient_phone_number") or item.get("phone")
                     or item.get("contact_number"))
            exec_id = (item.get("execution_id") or item.get("id")
                       or item.get("call_id"))
            if phone in by_phone and exec_id:
                cur.execute(
                    "UPDATE voicebrain.call_leads SET bolna_execution_id = %s "
                    "WHERE id = %s",
                    (exec_id, by_phone[phone]),
                )
                mapped += 1
    return mapped


def _bolna_error(r):
    """Pull a human-readable error out of a Bolna error response (low balance,
    bad field, unverified number, etc.) so the dashboard can show WHY it failed."""
    try:
        j = r.json()
        if isinstance(j, dict):
            return (j.get("message") or j.get("detail") or j.get("error")
                    or j.get("error_message") or str(j))[:300]
    except Exception:
        pass
    return (r.text or "")[:300]


def _is_insufficient_funds(text):
    """True only when a Bolna error string is an ACTUAL out-of-money refusal —
    the REAL zero, not an estimate. This is the ONLY signal that halts a chunked
    run; every other failure is treated as transient and the run keeps going.
    We match generously (a false positive just PAUSES safely and is resumable),
    because Bolna's exact wording varies by version."""
    if not text:
        return False
    t = str(text).lower()
    needles = ("insufficient", "balance", "fund", "recharge", "top up", "top-up",
               "add money", "add funds", "out of credit", "no credit",
               "payment required", "402", "not enough")
    return any(n in t for n in needles)


def _create_batch(csv_text, agent_id=None, from_phone=None):
    """Create the Bolna batch — POST /batches (multipart).
    `from_phone_numbers` is documented as an ARRAY, but in a multipart body a
    Python list of ONE value and a bare string encode to the SAME bytes (requests
    flattens `['+91..']` to a single `from_phone_numbers=+91..` field) — so those
    two are NOT a real fallback pair. We therefore try two genuinely DIFFERENT
    encodings: the bare single field first (the form we have seen Bolna accept),
    then a real JSON-array string `["+91.."]` as a distinct fallback. Whichever
    Bolna accepts is reported back so the dashboard can show which shape worked.

    agent_id / from_phone default to Dhyan (BOLNA_AGENT_ID / FROM_PHONE) so every
    existing call site is unchanged; a named caller (e.g. Priya) passes its own.
    Returns (response_or_None, form_used, error_or_None)."""
    import json
    agent_id = agent_id or BOLNA_AGENT_ID
    from_phone = from_phone or FROM_PHONE
    last_err = None
    attempts = (
        ("string", from_phone),                    # bare field  -> from_phone_numbers=+91..
        ("json-array", json.dumps([from_phone])),  # JSON array  -> from_phone_numbers=["+91.."]
    )
    for form_name, val in attempts:
        files = {"file": ("batch.csv", csv_text.encode("utf-8"), "text/csv")}
        data = {"agent_id": agent_id, "from_phone_numbers": val}
        try:
            r = requests.post(f"{BOLNA_BASE_URL}/batches", headers=_auth_header(),
                              data=data, files=files, timeout=HTTP_TIMEOUT)
        except Exception as exc:
            last_err = f"network error: {exc}"
            print(f"[DIALER] create-batch ({form_name}) network error: {exc}")
            continue
        print(f"[DIALER] create-batch ({form_name} from_phone_numbers) "
              f"-> {r.status_code}: {r.text[:300]}")
        if r.status_code < 400:
            return r, form_name, None
        last_err = _bolna_error(r)
    return None, "both-failed", last_err


def schedule_batch(batch_id, dry_run=True, run_at=None):
    """Trigger a CREATED batch to actually RUN — Bolna's Schedule Batch API.
    POST /batches/{batch_id}/schedule.

    TWO things the docs require (and that a 400 on this call usually means we got
    wrong):
      1. CONTENT-TYPE — scheduled_at must be sent as a `multipart/form-data`
         field, NOT url-encoded and NOT JSON. We force multipart by passing it
         through requests' `files=` with a (None, value) tuple (a form field, not
         a real file) — same multipart shape the create call uses.
      2. DATETIME FORMAT — ISO-8601 in UTC with milliseconds and a `Z` suffix,
         e.g. `2026-06-29T11:13:34.000Z` (the doc example). An offset like
         `+05:30` (what we sent before) gets rejected.
    We aim a small buffer ahead of now (UTC) so the time is never in the past;
    Bolna then queues + paces the dialing itself.

    ENGINE-SPECIFIC: if your Bolna account expects a different field name or
    route, THIS is the single place to change it."""
    if run_at is None:
        # Two hard rules confirmed by live responses:
        #   - TIMING: must be >= 2 min in the future (400 "atleast 2 minutes in
        #     the future"). We use 3 min for a safe margin.
        #   - FORMAT: Bolna parses with datetime.fromisoformat(), which REJECTS a
        #     'Z' suffix (500 "Invalid isoformat string"). So we send a local
        #     OFFSET timestamp like 2026-06-29T17:11:41+05:30 (no 'Z', no micros).
        run_at = (datetime.now().astimezone() + timedelta(seconds=180)) \
            .replace(microsecond=0).isoformat()
    url = f"{BOLNA_BASE_URL}/batches/{batch_id}/schedule"
    print(f"  schedule    : POST {url}")
    print(f"  scheduled_at: {run_at}  (multipart/form-data field, ISO offset, "
          "+3 min — Bolna requires >= 2 min ahead, fromisoformat-compatible)")
    if dry_run:
        print("[DIALER] DRY RUN — would schedule the batch to run ~3 min out.")
        return None
    # (None, value) => a multipart/form-data FORM field (not a file upload).
    r = requests.post(url, headers=_auth_header(),
                      files={"scheduled_at": (None, run_at)}, timeout=HTTP_TIMEOUT)
    print(f"[DIALER] Bolna schedule responded {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json() if r.content else None


def _launch_batch_for_leads(campaign_id, leads, dry_run=False,
                            agent_id=None, from_phone=None):
    """Create + schedule ONE Bolna batch for an EXPLICIT lead list, then persist
    batch_id + attempts++ + stage->ai_call for exactly those leads.

    This is the SHARED core (no fork): launch_campaign passes the whole callable
    list; launch_campaign_chunked passes one small chunk at a time. The caller
    owns the env / window / empty-list checks. Returns the same structured result
    dict launch_campaign always returned, so the dashboard self-reports per batch.

    agent_id / from_phone default to Dhyan; a named caller passes its own pair so
    the batch fires on the right agent + number."""
    csv_text = build_batch_csv(leads)
    result = {
        "ok": False, "leads": len(leads), "form_used": None,
        "batch_id": None, "state": None, "scheduled": False,
        "schedule_state": None, "error": None, "http_status": None,
    }
    if dry_run:
        print(f"[DIALER] DRY RUN — would create+schedule a {len(leads)}-lead batch.")
        return result

    # Step 1 — CREATE the batch (array from_phone_numbers, string fallback).
    r, form_used, create_err = _create_batch(csv_text, agent_id=agent_id,
                                             from_phone=from_phone)
    result["form_used"] = form_used
    if r is None:
        result["error"] = create_err or "create-batch failed"
        print(f"[DIALER] FAILED — create-batch did not succeed: {result['error']}")
        return result

    result["http_status"] = r.status_code
    resp = r.json() if r.content else {}
    batch_id = (resp.get("batch_id") or resp.get("id")
                or (resp.get("batch", {}) or {}).get("id")) \
        if isinstance(resp, dict) else None
    result["batch_id"] = batch_id
    result["state"] = resp.get("state") if isinstance(resp, dict) else None

    if not batch_id:
        result["error"] = "create returned 2xx but no batch_id in the response"
        print("[DIALER] WARNING — no batch_id returned; not scheduling.")
        return result

    # Persist batch id + bump attempts for the leads we just sent.
    import psycopg2
    conn = psycopg2.connect(**DB)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE voicebrain.campaigns SET bolna_batch_id = %s WHERE id = %s",
                (str(batch_id), campaign_id),
            )
            ids = [ld["id"] for ld in leads]
            # attempts++ + queue + stage->ai_call, and stamp last_attempt_at for
            # the retry cool-off. Guarded: if migration 011 (last_attempt_at) is
            # not applied yet, fall back to the same UPDATE without that column so
            # a launch is NEVER blocked — retries just lose the precise gap timer.
            try:
                cur.execute("SAVEPOINT sp_att")
                cur.execute(
                    "UPDATE voicebrain.call_leads "
                    "SET attempts = attempts + 1, call_status = 'queued', "
                    "    funnel_stage = 'ai_call', last_attempt_at = now() "
                    "WHERE id = ANY(%s)",
                    (ids,),
                )
                cur.execute("RELEASE SAVEPOINT sp_att")
            except psycopg2.errors.UndefinedColumn:
                cur.execute("ROLLBACK TO SAVEPOINT sp_att")
                cur.execute(
                    "UPDATE voicebrain.call_leads "
                    "SET attempts = attempts + 1, call_status = 'queued', "
                    "    funnel_stage = 'ai_call' "
                    "WHERE id = ANY(%s)",
                    (ids,),
                )
            mapped = _map_executions(conn, resp, leads)
        print(f"[DIALER] LIVE — batch_id={batch_id} state={result['state']}, "
              f"attempts++ + stage->ai_call for {len(leads)} lead(s), "
              f"exec ids mapped: {mapped}.")
    finally:
        conn.close()

    # Step 2 — SCHEDULE/RUN the batch (it is only STAGED until scheduled).
    try:
        sched = schedule_batch(batch_id, dry_run=False)
        result["scheduled"] = True
        result["schedule_state"] = (sched.get("state")
                                    if isinstance(sched, dict) else None)
        print(f"[DIALER] LIVE — batch {batch_id} scheduled to run now. "
              "Bolna will queue + pace the dialing.")
    except Exception as sched_exc:
        # Batch + batch_id are already saved; surface the reason, don't crash.
        result["error"] = f"created OK but schedule/run failed: {sched_exc}"
        print(f"[DIALER] WARNING — batch {batch_id} was CREATED but the "
              f"schedule/run call failed: {sched_exc}\n"
              "  The leads are staged in Bolna; run the batch from the Bolna "
              "dashboard, or retry the schedule step.")

    result["ok"] = bool(batch_id)
    return result


def launch_campaign(campaign_id, dry_run=True, agent=None):
    """Create + RUN a Bolna batch for all callable leads of a campaign.
    Two Bolna calls: POST /batches (create, returns batch_id) then
    POST /batches/{id}/schedule (run). Bolna queues + paces the dialing.

    agent=None -> the default Dhyan caller (unchanged). agent='priya' -> that
    caller's own agent_id + from-phone (hard-stops if either is unconfigured)."""
    # Resolve the caller FIRST so a bad --agent fails before anything else.
    agent_id, from_phone, caller = resolve_caller(agent)
    require_env(live=not dry_run, agent_id=agent_id, from_phone=from_phone)
    # AUTO-RETRY: before building the call list, requeue any failed-call leads
    # that are past the cool-off (no-op on dry-run — preview only). fetch_callable
    # then picks them up alongside fresh leads in this same launch.
    rq = requeue_failed_leads(campaign_id, dry_run=dry_run)
    if rq["candidates"]:
        print(f"[DIALER] retry: {rq['candidates']} failed lead(s) eligible — "
              + ("dry run, not requeued." if dry_run
                 else f"{rq['requeued']} requeued for another attempt."))
    leads = fetch_callable_leads(campaign_id)
    csv_text = build_batch_csv(leads)
    open_now, hour = calling_window_open()
    numbers = [ld["phone"] for ld in leads]

    print("=" * 56)
    print("  DIALER — launch_campaign" + ("   [DRY RUN]" if dry_run else "   [LIVE]"))
    print("=" * 56)
    print(f"  campaign_id : {campaign_id}")
    print(f"  endpoint    : POST {BOLNA_BASE_URL}/batches  (multipart)")
    print(f"  caller      : {caller}")
    print(f"  agent_id    : {agent_id}")
    print(f"  from_phone  : {from_phone}")
    print(f"  header      : Authorization: Bearer "
          f"{'<set>' if BOLNA_API_KEY else '<BOLNA_API_KEY not set>'}")
    print(f"  lead count  : {len(leads)}  (live, stage='{INTAKE_STAGE}', "
          f"attempts < {MAX_ATTEMPTS})")
    print(f"  window      : {'OPEN' if open_now else 'CLOSED'} (now {hour:02d}:xx, "
          f"allowed {CALL_WINDOW_START:02d}–{CALL_WINDOW_END:02d})")
    preview = numbers[:5]
    print(f"  first 5 #   : {preview}")
    print("  batch.csv preview:")
    for line in csv_text.splitlines()[:6]:
        print(f"    {line}")
    print("=" * 56)

    if not leads:
        print("[DIALER] No callable leads (none live at stage, or all at "
              "MAX_ATTEMPTS). Nothing to do.")
        return {"ok": False, "leads": 0, "error": "no callable leads"}

    if dry_run:
        print(f"[DIALER] DRY RUN — step 1: POST {BOLNA_BASE_URL}/batches (create) "
              f"with agent_id={agent_id}, from_phone_numbers={from_phone} "
              "(bare field first, JSON-array fallback), and the "
              f"{len(leads)}-row batch.csv shown above.")
        print("[DIALER] DRY RUN — step 2: schedule the returned batch to run:")
        schedule_batch("<batch_id_from_step_1>", dry_run=True)
        print("[DIALER] DRY RUN — nothing fired: no API call, no DB write, "
              "attempts NOT incremented.")
        return None

    enforce_window_or_die()

    # Single-shot launch = the shared core over the WHOLE callable list (no fork).
    return _launch_batch_for_leads(campaign_id, leads, dry_run=False,
                                   agent_id=agent_id, from_phone=from_phone)


# --- launch_campaign_chunked (auto-chunking to beat the carrier spam-block) --
def chunk_plan(campaign_id, chunk_size=None, delay_sec=None):
    """Read-only preview of how a chunked run WOULD pace: count, number of
    sub-batches, and a rough wall-clock estimate. Drives the Start Calling modal
    ('will call 88 in 18 chunks of 5, ~51 min'). No API call, no DB write."""
    chunk_size = chunk_size or CHUNK_SIZE
    delay_sec = CHUNK_DELAY_SEC if delay_sec is None else delay_sec
    leads = fetch_callable_leads(campaign_id)
    n = len(leads)
    n_chunks = (n + chunk_size - 1) // chunk_size if n else 0
    # wall-clock ~= the gaps BETWEEN chunks (the last chunk has no trailing wait).
    eta_sec = max(n_chunks - 1, 0) * delay_sec
    return {
        "count": n, "chunk_size": chunk_size, "delay_sec": delay_sec,
        "n_chunks": n_chunks, "eta_sec": eta_sec,
        "eta_min": round(eta_sec / 60),
    }


def _write_progress(path, progress):
    """Atomically write the chunk-progress JSON the dashboard polls. Best-effort:
    a progress-write hiccup must never abort the calling run."""
    if not path:
        return
    try:
        import json
        progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(progress, f)
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[DIALER] progress write failed (harmless): {exc}")


def launch_campaign_chunked(campaign_id, dry_run=False, progress_path=None,
                            chunk_size=None, delay_sec=None, agent=None,
                            max_chunks=None):
    """Auto-chunked launch — split the callable leads into small sub-batches and
    pace them so the carrier never sees a big burst:
        fire CHUNK_SIZE calls -> wait CHUNK_DELAY_SEC -> fire the next -> ...

    SAFE BY DESIGN:
      - WINDOW : re-checked before EVERY chunk; if it closes mid-run we stop and
                 leave the remaining (undialed) leads callable for a later run.
      - DNC / MAX_ATTEMPTS / stage filter : inherited unchanged via
                 fetch_callable_leads + the shared _launch_batch_for_leads core.
      - RESILIENT : a chunk that fails is logged and we CONTINUE — one bad
                 sub-batch never kills the whole run.
    Designed to run in a BACKGROUND thread; it writes progress to progress_path
    after every chunk so the dashboard can show 'chunk 3 of 18, 15/88 dialed'."""
    import time
    chunk_size = chunk_size or CHUNK_SIZE
    delay_sec = CHUNK_DELAY_SEC if delay_sec is None else delay_sec
    now = datetime.now().isoformat(timespec="seconds")

    progress = {
        "campaign_id": campaign_id, "status": "running",
        "chunk_total": 0, "chunk_done": 0,
        "leads_total": 0, "leads_dialed": 0,
        "chunk_size": chunk_size, "delay_sec": delay_sec,
        "message": "starting...", "started_at": now, "updated_at": now,
    }

    # Resolve the caller (Dhyan default / named). resolve_caller raises
    # SystemExit on a mis/under-configured named caller — but we are usually in a
    # thread here, so convert that to a failed progress state instead of killing
    # the process.
    try:
        agent_id, from_phone, caller = resolve_caller(agent)
    except SystemExit as exc:
        progress["status"] = "failed"
        progress["message"] = str(exc)
        _write_progress(progress_path, progress)
        return {"ok": False, "error": str(exc)}
    progress["caller"] = caller

    # Soft env check (we are usually in a thread — never raise SystemExit here;
    # surface it as a failed progress state the dashboard can show instead).
    missing = [n for n, v in (("BOLNA_API_KEY", BOLNA_API_KEY),
                              ("BOLNA_AGENT_ID", agent_id),
                              ("BOLNA_FROM_PHONE", from_phone)) if not v]
    if missing and not dry_run:
        progress["status"] = "failed"
        progress["message"] = "missing env: " + ", ".join(missing)
        _write_progress(progress_path, progress)
        return {"ok": False, "error": progress["message"]}

    try:
        # AUTO-RETRY: requeue failed-call leads past the cool-off before we build
        # the chunk list (no-op on dry-run). They then dial in this run.
        if not dry_run:
            rq = requeue_failed_leads(campaign_id, dry_run=False)
            if rq["requeued"]:
                progress["message"] = f"requeued {rq['requeued']} failed lead(s) for retry"
                _write_progress(progress_path, progress)
        leads = fetch_callable_leads(campaign_id)
        total = len(leads)
        chunks = [leads[i:i + chunk_size] for i in range(0, total, chunk_size)]
        # CHECKPOINT CAP: --max-chunks stops the run after N sub-batches so a small
        # verification burst (e.g. 1 chunk = 5 calls) can NEVER roll on into the
        # full campaign. Purely additive: max_chunks=None keeps the old unlimited
        # behaviour. We shrink the lead list too so total/attempts stay honest.
        if max_chunks is not None and max_chunks > 0 and max_chunks < len(chunks):
            chunks = chunks[:max_chunks]
            leads = [ld for c in chunks for ld in c]
            total = len(leads)
        n_chunks = len(chunks)
        progress.update(chunk_total=n_chunks, leads_total=total,
                        message=f"{total} leads in {n_chunks} chunks of {chunk_size}")
        _write_progress(progress_path, progress)

        if total == 0:
            progress.update(status="done", message="no callable leads to dial")
            _write_progress(progress_path, progress)
            return {"ok": False, "leads": 0, "error": "no callable leads"}

        if dry_run:
            eta = max(n_chunks - 1, 0) * delay_sec
            progress.update(status="done",
                            message=f"DRY RUN — would dial {total} in {n_chunks} "
                                    f"chunks of {chunk_size}, ~{round(eta/60)} min")
            _write_progress(progress_path, progress)
            return {"ok": True, "dry_run": True, "leads": total,
                    "n_chunks": n_chunks, "eta_sec": eta}

        dialed = 0
        for i, chunk in enumerate(chunks, 1):
            # WINDOW guard — re-check before each chunk; stop cleanly if closed.
            open_now, hour = calling_window_open()
            if not open_now:
                progress.update(status="stopped", chunk_done=i - 1,
                    leads_dialed=dialed,
                    message=(f"window closed at chunk {i}/{n_chunks} ({hour:02d}:xx); "
                             f"{dialed}/{total} dialed; remaining stay callable"))
                _write_progress(progress_path, progress)
                print(f"[DIALER] CHUNKED — window closed, stopping at chunk {i}.")
                return {"ok": True, "stopped": "window", "dialed": dialed,
                        "total": total}

            # Fire this sub-batch via the SHARED core. TWO distinct failure modes:
            #   - REAL out-of-funds refusal from Bolna -> PAUSE the run cleanly
            #     right here. The undialed leads never got attempts++ (that only
            #     happens on a successful create), so they STAY callable and a
            #     re-run after top-up resumes exactly where we left off.
            #   - any OTHER failure (transient / network / bad field) -> log and
            #     KEEP GOING; one bad sub-batch never kills the run.
            err = None
            try:
                res = _launch_batch_for_leads(campaign_id, chunk, dry_run=False,
                                              agent_id=agent_id,
                                              from_phone=from_phone)
                ok = bool(res and res.get("ok"))
                err = (res or {}).get("error")
                if ok:
                    dialed += len(chunk)
                    note = f"OK batch {res.get('batch_id')}"
                else:
                    note = f"FAILED: {err}"
            except Exception as chunk_exc:
                ok = False
                err = str(chunk_exc)
                note = f"crashed: {chunk_exc}"
                print(f"[DIALER] CHUNKED — chunk {i} crashed (continuing): {chunk_exc}")

            # ONLY stop on the REAL zero — an actual Bolna insufficient-funds
            # refusal. No estimate, no pre-emptive cap: we run until Bolna says no.
            if not ok and _is_insufficient_funds(err):
                lead_no = dialed + 1  # first lead we could NOT get out
                remaining = total - dialed
                progress.update(status="paused_no_funds", chunk_done=i - 1,
                    leads_dialed=dialed,
                    message=(f"stopped: Bolna balance empty at lead {lead_no} "
                             f"(chunk {i}/{n_chunks}); {dialed}/{total} dialed; "
                             f"{remaining} remaining stay callable — top up and "
                             f"re-run to resume. [Bolna: {err}]"))
                _write_progress(progress_path, progress)
                print(f"[DIALER] CHUNKED — PAUSED (Bolna out of funds) at chunk "
                      f"{i}/{n_chunks}, lead ~{lead_no}: {dialed}/{total} dialed, "
                      f"{remaining} still callable. Bolna said: {err}")
                return {"ok": True, "stopped": "no_funds", "dialed": dialed,
                        "total": total, "remaining": remaining,
                        "at_lead": lead_no, "bolna_error": err}

            progress.update(chunk_done=i, leads_dialed=dialed,
                message=f"chunk {i}/{n_chunks} {note}; {dialed}/{total} dialed")
            _write_progress(progress_path, progress)
            print(f"[DIALER] CHUNKED — chunk {i}/{n_chunks}: {note} "
                  f"({dialed}/{total} dialed)")

            # Pace before the NEXT chunk (no trailing wait after the last).
            if i < n_chunks:
                time.sleep(delay_sec)

        progress.update(status="done", message=f"complete: {dialed}/{total} dialed "
                        f"across {n_chunks} chunks of {chunk_size}")
        _write_progress(progress_path, progress)
        print(f"[DIALER] CHUNKED — DONE: {dialed}/{total} dialed.")
        return {"ok": True, "dialed": dialed, "total": total, "n_chunks": n_chunks}

    except Exception as exc:
        progress.update(status="failed", message=f"run crashed: {exc}")
        _write_progress(progress_path, progress)
        print(f"[DIALER] CHUNKED — run crashed: {exc}")
        return {"ok": False, "error": str(exc)}


# --- CLI --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Dhyaan Voice Brain — Dialer (Bolna). DRY-RUN by default; "
                    "pass --go-live to actually dial.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_one = sub.add_parser("one", help="single test call (POST /call)")
    p_one.add_argument("--phone", required=True, help="E.164 number, e.g. +919876543210")
    p_one.add_argument("--agent", default=None,
                       help="named caller (e.g. dhyan, priya); default = Dhyan")
    p_one.add_argument("--go-live", action="store_true",
                       help="actually place the call (default is dry-run)")

    p_camp = sub.add_parser("campaign", help="batch-call a campaign (POST /batches)")
    p_camp.add_argument("--campaign-id", type=int, required=True)
    p_camp.add_argument("--agent", default=None,
                        help="named caller (e.g. dhyan, priya); default = Dhyan")
    p_camp.add_argument("--go-live", action="store_true",
                        help="actually upload + dial (default is dry-run)")

    p_chunk = sub.add_parser("chunked",
        help="auto-chunked campaign call (paced sub-batches, beats spam-block)")
    p_chunk.add_argument("--campaign-id", type=int, required=True)
    p_chunk.add_argument("--agent", default=None,
                         help="named caller (e.g. dhyan, priya); default = Dhyan")
    p_chunk.add_argument("--chunk-size", type=int, default=None,
                         help=f"calls per sub-batch (default {CHUNK_SIZE})")
    p_chunk.add_argument("--delay", type=int, default=None,
                         help=f"seconds between sub-batches (default {CHUNK_DELAY_SEC})")
    p_chunk.add_argument("--max-chunks", type=int, default=None,
                         help="stop after this many sub-batches (e.g. 1 = a 5-call "
                              "checkpoint). Default: run the whole campaign.")
    p_chunk.add_argument("--go-live", action="store_true",
                         help="actually upload + dial (default is dry-run)")

    p_retry = sub.add_parser("retry",
        help="requeue failed-call leads (no-answer/busy/failed) for another attempt")
    p_retry.add_argument("--campaign-id", type=int, required=True)
    p_retry.add_argument("--gap", type=int, default=None,
                         help=f"cool-off minutes before a retry (default {RETRY_DELAY_MIN})")
    p_retry.add_argument("--go-live", action="store_true",
                         help="actually mark them retry_pending (default is dry-run preview)")

    args = ap.parse_args()
    dry = not args.go_live
    if args.cmd == "one":
        trigger_one(args.phone, dry_run=dry, agent=args.agent)
    elif args.cmd == "campaign":
        launch_campaign(args.campaign_id, dry_run=dry, agent=args.agent)
    elif args.cmd == "retry":
        rq = requeue_failed_leads(args.campaign_id, dry_run=dry, min_gap_min=args.gap)
        print("=" * 56)
        print("  DIALER — retry" + ("   [DRY RUN]" if dry else "   [LIVE]"))
        print("=" * 56)
        print(f"  campaign_id      : {args.campaign_id}")
        print(f"  cool-off (min)   : {args.gap if args.gap is not None else RETRY_DELAY_MIN}")
        print(f"  eligible leads   : {rq['candidates']}")
        for c in rq["leads"][:15]:
            print(f"    lead {c['id']:>6}  att={c['attempts']}  {c['reason']}")
        if rq["candidates"] > 15:
            print(f"    ... and {rq['candidates'] - 15} more")
        print(f"  requeued         : {rq['requeued']}"
              + ("   (dry run — nothing written)" if dry else ""))
        print("=" * 56)
    elif args.cmd == "chunked":
        import os as _os
        prog = _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "logs", "chunk_progress.json")
        plan = chunk_plan(args.campaign_id, args.chunk_size, args.delay)
        capped = (args.max_chunks and args.max_chunks < plan['n_chunks'])
        eff_chunks = args.max_chunks if capped else plan['n_chunks']
        print(f"[DIALER] chunked plan: {plan['count']} leads -> {plan['n_chunks']} "
              f"chunks of {plan['chunk_size']}, ~{plan['eta_min']} min "
              f"(delay {plan['delay_sec']}s)."
              + (f"  [CAPPED to {eff_chunks} chunk(s) = "
                 f"{eff_chunks * plan['chunk_size']} calls]" if capped else ""))
        launch_campaign_chunked(args.campaign_id, dry_run=dry, progress_path=prog,
                                chunk_size=args.chunk_size, delay_sec=args.delay,
                                agent=args.agent, max_chunks=args.max_chunks)


if __name__ == "__main__":
    main()
