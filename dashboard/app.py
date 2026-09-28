"""
DHYAAN VOICE BRAIN — AI CALLING DASHBOARD (backend)
===================================================
FastAPI service on port 5006. One job: upload a CSV of any size, click ONE
button, Dhyan (Bolna) calls everyone on it, results stream back here.

Flow:
  1. Upload CSV  -> /upload  -> clean_leads (dedupe/validate) -> live_intake
     (DB write) -> dnc_filter.scan-db (flag any do-not-contact). REUSE, never fork.
  2. Start Calling -> /start-calling -> dialer.launch_campaign (Bolna /batches).
     SAME safe gate as the old Release button: a confirmation modal, then calls
     fire ONLY on Confirm. The dialer keeps its guards (window, MAX_ATTEMPTS, DNC).
  3. Catcher (port 5005) writes each Bolna result; this page shows them live.

Pacing: launch_campaign hands the WHOLE list to Bolna's BATCH API in one upload.
Bolna queues + paces the dialing itself (its own concurrency / from-number
limits) — we do NOT fan out N simultaneous /call requests, so Bolna is never
overloaded. "Feed steadily, let Bolna queue the rest" is exactly the batch model.

Queries live data from Postgres schema `voicebrain` (deleted_at IS NULL).
No dependency on the AiSensy / funnel modules — they stay on disk, unused here.
"""
from dotenv import load_dotenv
import os
import sys
import shutil
import logging
import urllib.parse

# Load env variables FIRST before importing project config
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

sys.path.append(BASE_DIR)
from config import (
    DB, EST_COST_PER_CALL, LOW_BALANCE_THRESHOLD, CHUNK_SIZE, CHUNK_DELAY_SEC,
    MAX_ATTEMPTS, CALL_WINDOW_START, CALL_WINDOW_END, RETRY_DELAY_MIN,
)
from fastapi import FastAPI, Request, File, UploadFile, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
import psycopg2

# Initialize Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dashboard")

# Auto-chunked runs write their live progress here; the dashboard polls it each
# load so a long (~50 min) paced run shows 'chunk 3 of 18, 15/88 dialed' and
# survives the Captain navigating away (the run is a server-side background
# thread, not tied to the request).
PROGRESS_PATH = os.path.join(BASE_DIR, "logs", "chunk_progress.json")


def read_chunk_progress():
    """Best-effort read of the chunk-progress JSON. Returns {} if absent/garbage
    so the board never breaks when no chunked run has happened yet."""
    try:
        import json
        if os.path.exists(PROGRESS_PATH):
            with open(PROGRESS_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        log.error("chunk progress read failed: %s", exc)
    return {}

app = FastAPI(title="Dhyaan Voice Brain — AI Calling Dashboard")

# Initialize Templates
templates_dir = os.path.join(BASE_DIR, "dashboard", "templates")
os.makedirs(templates_dir, exist_ok=True)
templates = Jinja2Templates(directory=templates_dir)


def mask_phone(phone: str) -> str:
    """Return masked phone number in the format +91 ····· last4."""
    if not phone:
        return ""
    phone_str = str(phone).strip()
    if len(phone_str) >= 4:
        return f"+91 ····· {phone_str[-4:]}"
    return phone_str


def get_db_stats(campaign_id=None):
    """Query live metrics + the connection-split board from voicebrain, SCOPED to
    ONE batch/campaign.

    campaign_id=None  -> the MOST RECENT campaign (the live main-dashboard board).
    campaign_id=<id>  -> that specific past batch (the History drill-down view).
    Same code path either way — the History detail page is just this board scoped
    to an older campaign (no fork).

    The board is driven by each lead's LATEST call_result.connection_status:
      connected                          -> CONNECTED section (rich + recording)
      no-answer / failed / not-connected -> NOT CONNECTED section
      (no result yet)                    -> counted as 'pending' (in flight)
    """
    stats = {
        "db_online": False,
        "campaign_name": "No Active Campaign",
        "active_campaign_id": None,
        "campaign_created": None,
        "batch_id": None,
        # --- top metric tiles ---
        "total": 0,
        "connected": 0,
        "not_connected": 0,
        "verified": 0,
        "spend": 0.0,
        "pending": 0,
        "retry_pending": 0,   # failed-call leads queued for another attempt
        # --- the two board sections ---
        "connected_leads": [],
        "not_connected_leads": [],
        # --- Start Calling preview (for the confirmation modal) ---
        "calling_count": 0,
        "calling_window_open": False,
        "calling_window_label": "",
        "calling_numbers": [],
        "calling_est_cost": 0.0,
        "cost_per_call": EST_COST_PER_CALL,
        # --- Bolna wallet (balance awareness) ---
        "bolna_wallet": None,
        "bolna_balance_ok": False,
        "bolna_balance_error": None,
        "low_balance": False,
        "low_balance_threshold": LOW_BALANCE_THRESHOLD,
        # --- Auto-chunking (preview for the modal + live run progress) ---
        "chunk_size": CHUNK_SIZE,
        "chunk_delay_sec": CHUNK_DELAY_SEC,
        "chunk_count_preview": 0,
        "chunk_eta_min": 0,
        "chunk_active": False,
        "chunk_status": None,
        "chunk_done": 0,
        "chunk_total": 0,
        "chunk_leads_dialed": 0,
        "chunk_leads_total": 0,
        "chunk_message": None,
        # --- Verified leads (Layer 4) ---
        "verified_leads": [],
        "verified_count": 0,
    }

    # Live chunked-run progress (if any) — drives the progress banner.
    prog = read_chunk_progress()
    if prog:
        stats["chunk_status"] = prog.get("status")
        stats["chunk_active"] = prog.get("status") == "running"
        stats["chunk_done"] = prog.get("chunk_done", 0)
        stats["chunk_total"] = prog.get("chunk_total", 0)
        stats["chunk_leads_dialed"] = prog.get("leads_dialed", 0)
        stats["chunk_leads_total"] = prog.get("leads_total", 0)
        stats["chunk_message"] = prog.get("message")

    # Bolna wallet balance — REUSE the Dialer's reader (the only Bolna-talking
    # module); never fork. Read-only, never raises, short timeout so a slow Bolna
    # never stalls the board. Drives the balance tile + the modal low-balance
    # warning. A failure just leaves the tile showing "—" with the reason.
    try:
        from dialer.dialer import bolna_balance
        bal = bolna_balance(timeout=8)
        stats["bolna_balance_ok"] = bal.get("ok", False)
        stats["bolna_wallet"] = bal.get("wallet")
        stats["bolna_balance_error"] = bal.get("error")
        if bal.get("ok") and bal.get("wallet") is not None:
            stats["low_balance"] = bal["wallet"] < LOW_BALANCE_THRESHOLD
    except Exception as bal_exc:
        stats["bolna_balance_error"] = str(bal_exc)
        log.error("Bolna balance read failed: %s", bal_exc)

    try:
        conn = psycopg2.connect(**DB)
        stats["db_online"] = True
        try:
            with conn.cursor() as cur:
                # 1. The campaign this board is scoped to: a specific past batch
                #    (History drill-down) or the latest uploaded batch (main board).
                if campaign_id is not None:
                    cur.execute(
                        "SELECT id, name, bolna_batch_id, created_at "
                        "FROM voicebrain.campaigns "
                        "WHERE deleted_at IS NULL AND id = %s LIMIT 1",
                        (campaign_id,),
                    )
                else:
                    cur.execute(
                        "SELECT id, name, bolna_batch_id, created_at "
                        "FROM voicebrain.campaigns "
                        "WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 1"
                    )
                camp_row = cur.fetchone()
                if camp_row:
                    stats["active_campaign_id"] = camp_row[0]
                    stats["campaign_name"] = camp_row[1]
                    stats["batch_id"] = camp_row[2]
                    stats["campaign_created"] = (
                        camp_row[3].strftime("%d %b %Y, %H:%M")
                        if camp_row[3] else None
                    )

                    # Start Calling preview — reuse the Dialer's EXACT callable-lead
                    # filter (stage + attempts + DNC). Import, never fork.
                    try:
                        from dialer.dialer import plan_campaign
                        plan = plan_campaign(stats["active_campaign_id"])
                        stats["calling_count"] = plan["count"]
                        stats["calling_window_open"] = plan["window_open"]
                        stats["calling_window_label"] = (
                            f"{plan['window_start']:02d}:00-{plan['window_end']:02d}:00"
                        )
                        stats["calling_numbers"] = [
                            mask_phone(ld["phone"]) for ld in plan["leads"]
                        ]
                        stats["calling_est_cost"] = round(
                            plan["count"] * EST_COST_PER_CALL, 0)
                        # Chunk preview for the modal: how many paced sub-batches
                        # and the rough wall-clock (gaps between chunks).
                        cnt = plan["count"]
                        n_chunks = (cnt + CHUNK_SIZE - 1) // CHUNK_SIZE if cnt else 0
                        stats["chunk_count_preview"] = n_chunks
                        stats["chunk_eta_min"] = round(
                            max(n_chunks - 1, 0) * CHUNK_DELAY_SEC / 60)
                    except Exception as plan_exc:
                        log.error("Calling plan failed: %s", plan_exc)

                # Every metric below is SCOPED to this one campaign so the board
                # shows ONE clean batch (the rest live in History). scope_id is the
                # resolved campaign id; if there are no campaigns it stays None and
                # every `campaign_id = NULL` filter yields zero rows (empty board).
                scope_id = stats["active_campaign_id"]

                # 2. Total leads in THIS batch.
                cur.execute(
                    "SELECT COUNT(*) FROM voicebrain.call_leads "
                    "WHERE deleted_at IS NULL AND campaign_id = %s",
                    (scope_id,),
                )
                stats["total"] = cur.fetchone()[0]

                # 3. Spend ₹ — Bolna all-in for THIS batch (lifetime, not month).
                #    Bolna posts a webhook at EVERY status change (initiated →
                #    ringing → in-progress → … → completed) and the catcher saves
                #    each as its own call_results row, several of which carry a
                #    (partial) cost. A flat SUM(cost) therefore DOUBLE-COUNTS a
                #    single call. We collapse to ONE cost per call — MAX(cost) per
                #    execution id (the terminal webhook carries the final, highest
                #    figure) — then sum those. COALESCE(exec_id, id) so any legacy
                #    row without an execution id still counts once on its own.
                cur.execute(
                    """
                    SELECT COALESCE(SUM(call_cost), 0) FROM (
                        SELECT MAX(cr.cost) AS call_cost
                        FROM voicebrain.call_results cr
                        JOIN voicebrain.call_leads cl ON cr.call_lead_id = cl.id
                        WHERE cl.deleted_at IS NULL
                          AND cl.campaign_id = %s
                        GROUP BY COALESCE(cr.bolna_execution_id, cr.id::text)
                    ) per_call
                    """,
                    (scope_id,),
                )
                stats["spend"] = float(cur.fetchone()[0])

                # 4. Every lead + its latest call result, split by connection.
                #    connection_status / recording_url / raw_transcript need
                #    migration 009; if absent we fall back to the base columns
                #    (no result splits as 'pending' until 009 + a call land).
                base_sql = """
                    SELECT cl.id, cl.first_name, cl.phone, cl.attempts,
                           cl.call_status, cr.call_outcome, cr.call_quality,
                           cr.heat_score, cr.manual_rating, cr.rating_note,
                           cr.cost, cr.duration_sec{extra}
                    FROM voicebrain.call_leads cl
                    LEFT JOIN LATERAL (
                        SELECT call_outcome, call_quality, heat_score,
                               manual_rating, rating_note, cost, duration_sec{extra2}
                        FROM voicebrain.call_results
                        WHERE call_lead_id = cl.id
                        ORDER BY received_at DESC NULLS LAST, id DESC
                        LIMIT 1
                    ) cr ON true
                    WHERE cl.deleted_at IS NULL AND cl.campaign_id = %s
                    ORDER BY cl.id DESC
                """
                extra = ", cr.connection_status, cr.recording_url, cr.raw_transcript"
                extra2 = ", connection_status, recording_url, raw_transcript"
                has_conn = True
                try:
                    cur.execute("SAVEPOINT sp_board")
                    cur.execute(
                        base_sql.format(extra=extra, extra2=extra2), (scope_id,))
                    rows = cur.fetchall()
                    cur.execute("RELEASE SAVEPOINT sp_board")
                except Exception as col_exc:
                    log.error("connection cols unavailable (run 009?) — %s", col_exc)
                    has_conn = False
                    cur.execute("ROLLBACK TO SAVEPOINT sp_board")
                    cur.execute(base_sql.format(extra="", extra2=""), (scope_id,))
                    rows = cur.fetchall()

                for row in rows:
                    conn_status = row[12] if has_conn else None
                    recording = row[13] if has_conn else None
                    transcript = row[14] if has_conn else None
                    outcome = row[5]
                    dur = row[11] or 0
                    # Legacy rows (written before migration 009 / the new catcher)
                    # have NULL connection_status. If a result EXISTS (outcome is
                    # set) derive connection from talk-time / outcome so the call
                    # doesn't silently vanish into 'pending'. Fresh, not-yet-
                    # returned leads (no result row) correctly stay pending.
                    if conn_status is None and outcome is not None:
                        conn_status = ("connected"
                                       if (outcome == "verified" or dur > 0)
                                       else "not-connected")
                    call_status = row[4] or "pending"
                    # A failed-call lead the dialer requeued for another attempt,
                    # still under the cap. Powers the "pending retry" badge/count.
                    pending_retry = (call_status == "retry_pending"
                                     and (row[3] or 0) < MAX_ATTEMPTS)
                    lead = {
                        "id": row[0],
                        "first_name": row[1],
                        "phone_masked": mask_phone(row[2]),
                        "attempts": row[3],
                        "max_attempts": MAX_ATTEMPTS,
                        "call_status": call_status,
                        "pending_retry": pending_retry,
                        "outcome": row[5],
                        "call_quality": row[6],
                        "heat_score": row[7],
                        "manual_rating": row[8],
                        "rating_note": row[9],
                        "cost": float(row[10]) if row[10] is not None else None,
                        "duration_sec": row[11],
                        "connection_status": conn_status,
                        "recording_url": recording,
                        "transcript": transcript,
                    }
                    if pending_retry:
                        stats["retry_pending"] += 1

                    if conn_status == "connected":
                        stats["connected_leads"].append(lead)
                        stats["connected"] += 1
                        if lead["outcome"] == "verified":
                            stats["verified"] += 1
                    elif conn_status in ("no-answer", "failed", "not-connected"):
                        stats["not_connected_leads"].append(lead)
                        stats["not_connected"] += 1
                    else:
                        # dialed but no result yet (queued / in-flight) or fresh
                        stats["pending"] += 1

                # --- VERIFIED LEADS (Layer 4) — each lead whose LATEST result is
                # flagged verified by the Catcher (intake.verify rule). Powers the
                # Verified section + the CSV export. Guarded so a pre-migration DB
                # (no 'verified' column) degrades to an empty list, never an error.
                try:
                    cur.execute("SAVEPOINT sp_ver")
                    cur.execute(
                        """
                        SELECT cl.id, cl.first_name, cl.phone,
                               cr.branch, cr.location, cr.budget, cr.configuration,
                               cr.possession, cr.property_type, cr.interested,
                               cr.call_outcome, cr.recording_url, cr.raw_transcript,
                               cr.duration_sec, cr.cost, cr.heat_score
                        FROM voicebrain.call_leads cl
                        JOIN LATERAL (
                            SELECT * FROM voicebrain.call_results
                            WHERE call_lead_id = cl.id
                            ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 1
                        ) cr ON true
                        WHERE cl.deleted_at IS NULL AND cl.campaign_id = %s
                          AND cr.verified = true
                        ORDER BY cl.id DESC
                        """,
                        (scope_id,),
                    )
                    vrows = cur.fetchall()
                    cur.execute("RELEASE SAVEPOINT sp_ver")
                except Exception as ver_exc:
                    log.error("verified query failed (run migration 010?): %s", ver_exc)
                    cur.execute("ROLLBACK TO SAVEPOINT sp_ver")
                    vrows = []
                for v in vrows:
                    stats["verified_leads"].append({
                        "id": v[0], "first_name": v[1],
                        "phone": v[2],                       # full # for sales to dial
                        "phone_masked": mask_phone(v[2]),
                        "branch": v[3],
                        "location": v[4], "budget": v[5], "configuration": v[6],
                        "possession": v[7], "property_type": v[8],
                        "interested": v[9], "outcome": v[10],
                        "recording_url": v[11], "transcript": v[12],
                        "duration_sec": v[13],
                        "cost": float(v[14]) if v[14] is not None else None,
                        "heat_score": v[15],
                    })
                stats["verified_count"] = len(stats["verified_leads"])

        except Exception as query_exc:
            log.error("Failed to execute metrics query: %s", query_exc)
        finally:
            conn.close()
    except Exception as db_exc:
        log.error("Failed to connect to database: %s", db_exc)

    return stats


def get_history_list():
    """Roll every campaign up into one archive row for the History page.

    Per batch: total leads, connected, not-connected, verified, spend — using the
    EXACT same 'latest result per lead' + connection logic as the live board (so
    the History numbers match what the drill-down shows). Newest first.
    Returns {"batches": [...], "db_online": bool}. Never raises to the caller.
    """
    out = {"batches": [], "db_online": False}
    try:
        conn = psycopg2.connect(**DB)
        out["db_online"] = True
        try:
            with conn.cursor() as cur:
                # connection + verified rollup per campaign. The CASE mirrors the
                # board's fallback for legacy rows with NULL connection_status.
                # Guarded: if connection_status/verified cols are missing (pre-009
                # /010), fall back to a leads-only count so History still renders.
                rollup_sql = """
                    WITH latest AS (
                        SELECT cl.id AS lead_id, cl.campaign_id,
                               cr.connection_status, cr.call_outcome,
                               cr.duration_sec, cr.verified
                        FROM voicebrain.call_leads cl
                        LEFT JOIN LATERAL (
                            SELECT connection_status, call_outcome,
                                   duration_sec, verified
                            FROM voicebrain.call_results
                            WHERE call_lead_id = cl.id
                            ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 1
                        ) cr ON true
                        WHERE cl.deleted_at IS NULL
                    ),
                    eff AS (
                        SELECT lead_id, campaign_id, verified,
                            CASE
                              WHEN connection_status IS NOT NULL
                                   THEN connection_status
                              WHEN call_outcome IS NOT NULL
                                   AND (call_outcome = 'verified'
                                        OR COALESCE(duration_sec, 0) > 0)
                                   THEN 'connected'
                              WHEN call_outcome IS NOT NULL THEN 'not-connected'
                              ELSE NULL
                            END AS eff_conn
                        FROM latest
                    )
                    SELECT c.id, c.name, c.created_at,
                           COUNT(e.lead_id) AS total,
                           COUNT(*) FILTER (WHERE e.eff_conn = 'connected')
                               AS connected,
                           COUNT(*) FILTER (WHERE e.eff_conn IN
                               ('no-answer', 'failed', 'not-connected'))
                               AS not_connected,
                           COUNT(*) FILTER (WHERE e.verified IS TRUE)
                               AS verified,
                           (SELECT COALESCE(SUM(pc.call_cost), 0) FROM (
                               SELECT MAX(cr2.cost) AS call_cost
                               FROM voicebrain.call_results cr2
                               JOIN voicebrain.call_leads cl2
                                    ON cr2.call_lead_id = cl2.id
                               WHERE cl2.deleted_at IS NULL
                                 AND cl2.campaign_id = c.id
                               GROUP BY COALESCE(cr2.bolna_execution_id,
                                                 cr2.id::text)
                           ) pc) AS spend
                    FROM voicebrain.campaigns c
                    LEFT JOIN eff e ON e.campaign_id = c.id
                    WHERE c.deleted_at IS NULL
                    GROUP BY c.id, c.name, c.created_at
                    ORDER BY c.id DESC
                """
                try:
                    cur.execute("SAVEPOINT sp_hist")
                    cur.execute(rollup_sql)
                    hrows = cur.fetchall()
                    cur.execute("RELEASE SAVEPOINT sp_hist")
                except Exception as roll_exc:
                    log.error("history rollup failed (run 009/010?): %s", roll_exc)
                    cur.execute("ROLLBACK TO SAVEPOINT sp_hist")
                    # Minimal fallback: names + lead counts only.
                    cur.execute(
                        """
                        SELECT c.id, c.name, c.created_at,
                               (SELECT COUNT(*) FROM voicebrain.call_leads cl
                                 WHERE cl.campaign_id = c.id
                                   AND cl.deleted_at IS NULL),
                               0, 0, 0, 0
                        FROM voicebrain.campaigns c
                        WHERE c.deleted_at IS NULL
                        ORDER BY c.id DESC
                        """
                    )
                    hrows = cur.fetchall()

                for i, r in enumerate(hrows):
                    out["batches"].append({
                        "id": r[0],
                        "name": r[1] or f"Batch {r[0]}",
                        "created": (r[2].strftime("%d %b %Y, %H:%M")
                                    if r[2] else "—"),
                        "total": r[3] or 0,
                        "connected": r[4] or 0,
                        "not_connected": r[5] or 0,
                        "verified": r[6] or 0,
                        "spend": float(r[7]) if r[7] is not None else 0.0,
                        "is_current": i == 0,   # newest = the live main-board batch
                    })
        finally:
            conn.close()
    except Exception as db_exc:
        log.error("history list DB connect failed: %s", db_exc)
    return out


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, error: str = None, notice: str = None):
    stats = get_db_stats()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "error": error,
            "notice": notice,
            "history_view": False,
            **stats,
        },
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    """The archive: every past batch as a summary card, newest first."""
    data = get_history_list()
    return templates.TemplateResponse(
        "history.html",
        {"request": request, **data},
    )


@app.get("/history/{campaign_id}", response_class=HTMLResponse)
def history_detail(request: Request, campaign_id: int,
                   error: str = None, notice: str = None):
    """Drill into ONE past batch — the SAME board (leads, verified cards,
    recordings, transcripts) scoped to that campaign, in read-only history mode."""
    stats = get_db_stats(campaign_id=campaign_id)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "error": error,
            "notice": notice,
            "history_view": True,
            **stats,
        },
    )


@app.get("/retries", response_class=HTMLResponse)
def retries_page(request: Request, campaign_id: int = None,
                 error: str = None, notice: str = None):
    """Dedicated failed-call retry manager — every lead in the retry pipeline,
    bucketed pending / waiting / in-progress / exhausted. READ-ONLY view; the
    Retry-now and Stop actions are separate guarded POSTs. Reuses the dialer's
    retry_board (no fork). Scopes to the newest campaign with leads unless a
    ?campaign_id is given."""
    ctx = {
        "request": request, "error": error, "notice": notice,
        "db_online": False, "campaign_id": None, "campaign_name": None,
        "window_open": False, "window_start": CALL_WINDOW_START,
        "window_end": CALL_WINDOW_END, "retry_delay_min": RETRY_DELAY_MIN,
        "max_attempts": MAX_ATTEMPTS,
        "counts": {"pending_total": 0, "eligible_now": 0, "waiting": 0,
                   "in_progress": 0, "exhausted": 0},
        "pending": [], "waiting": [], "in_progress": [], "exhausted": [],
    }
    try:
        from dialer.dialer import retry_board, calling_window_open
        conn = psycopg2.connect(**DB)
        try:
            with conn.cursor() as cur:
                if campaign_id is None:
                    cur.execute(
                        "SELECT id, name FROM voicebrain.campaigns "
                        "WHERE deleted_at IS NULL AND COALESCE(total_leads,0) > 0 "
                        "ORDER BY id DESC LIMIT 1")
                else:
                    cur.execute("SELECT id, name FROM voicebrain.campaigns "
                                "WHERE id = %s", (campaign_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        ctx["db_online"] = True
        if row:
            ctx["campaign_id"], ctx["campaign_name"] = row[0], row[1]
            ctx.update(retry_board(row[0]))          # counts + 4 buckets
        ctx["window_open"], _ = calling_window_open()
    except Exception as e:
        log.error("retries page failed: %s", e)
        ctx["error"] = ctx["error"] or str(e)
    return templates.TemplateResponse("retries.html", ctx)


@app.post("/retries/run")
async def retries_run(campaign_id: int = Form(...), confirm: str = Form("")):
    """Retry-now: requeue the eligible failed leads then fire a paced chunked run
    for JUST this campaign (fresh leads are already exhausted on a completed batch,
    so in practice this dials only the retries). SAME guards as Start Calling —
    requires confirm=yes, enforces window/DNC/MAX_ATTEMPTS. Reuses the dialer's
    requeue_failed_leads + launch_campaign_chunked (no fork)."""
    if confirm != "yes":
        return RedirectResponse(url="/retries?error=retry+not+confirmed",
                                status_code=303)
    try:
        from dialer.dialer import (requeue_failed_leads, launch_campaign_chunked)
        prog = read_chunk_progress()
        if prog.get("status") == "running":
            return RedirectResponse(url="/retries?notice=" + urllib.parse.quote(
                "A run is already in progress: " + str(prog.get("message", ""))),
                status_code=303)
        rq = requeue_failed_leads(campaign_id, dry_run=False)
        if not rq["requeued"]:
            return RedirectResponse(url="/retries?notice=" + urllib.parse.quote(
                "No eligible retries right now (cool-off or max attempts)."),
                status_code=303)
        import threading
        threading.Thread(
            target=launch_campaign_chunked,
            kwargs={"campaign_id": campaign_id, "dry_run": False,
                    "progress_path": PROGRESS_PATH},
            daemon=True,
        ).start()
        return RedirectResponse(url="/retries?notice=" + urllib.parse.quote(
            f"Retry run started — {rq['requeued']} lead(s) re-queued and dialing "
            "(paced, within the calling window)."), status_code=303)
    except SystemExit as e:
        return RedirectResponse(url=f"/retries?error={urllib.parse.quote(str(e))}",
                                status_code=303)
    except Exception as e:
        log.error("retry run failed: %s", e)
        return RedirectResponse(url=f"/retries?error={urllib.parse.quote(str(e))}",
                                status_code=303)


@app.post("/retries/stop")
async def retries_stop(lead_id: int = Form(...)):
    """Manual override: take a lead OUT of the retry pipeline (Captain decides it's
    done / not worth another attempt). Marks funnel_stage='closed' so
    fetch_callable_leads never dials it again. voicebrain.call_leads only."""
    try:
        conn = psycopg2.connect(**DB)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE voicebrain.call_leads "
                    "SET funnel_stage = 'closed', call_status = 'closed_manual' "
                    "WHERE id = %s AND deleted_at IS NULL",
                    (lead_id,))
        finally:
            conn.close()
        return RedirectResponse(url="/retries?notice=" + urllib.parse.quote(
            f"Lead {lead_id} removed from the retry pipeline."), status_code=303)
    except Exception as e:
        log.error("retry stop failed: %s", e)
        return RedirectResponse(url=f"/retries?error={urllib.parse.quote(str(e))}",
                                status_code=303)


@app.post("/upload")
async def upload_leads(
    campaign_name: str = Form(...),
    consent_source: str = Form(...),
    csv_file: UploadFile = File(...),
):
    """Upload a CSV of ANY size. Reuses (never forks):
      clean_leads  -> normalize/dedupe/validate + write a Bolna-ready batch CSV,
      live_intake  -> register the leads in voicebrain.call_leads (fresh/queued),
      dnc_filter   -> scan-db to flag any do-not-contact numbers (compliance).
    """
    try:
        # Save raw CSV to data/inbound/. SANITISE the client filename: keep only
        # the basename and safe chars so a crafted name can't escape the folder
        # ('../') or silently clobber another campaign's raw CSV.
        import re
        raw_name = os.path.basename(csv_file.filename or "upload.csv")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name).lstrip(".") or "upload.csv"
        if not safe_name.lower().endswith(".csv"):
            safe_name += ".csv"
        inbound_dir = os.path.join(BASE_DIR, "data", "inbound")
        os.makedirs(inbound_dir, exist_ok=True)
        file_path = os.path.join(inbound_dir, safe_name)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(csv_file.file, f)
        log.info("CSV saved to %s", file_path)

        # Clean (dry-run): normalize + write the Bolna batch CSV to data/processed/.
        # Consume clean_leads' OWN processed-dir constant so we look for the batch
        # exactly where clean_leads writes it (no BASE_DIR-vs-relative mismatch).
        from intake.clean_leads import (
            process as clean_process, PROCESSED_DIR as CLEAN_PROCESSED_DIR,
        )
        clean_res = clean_process(
            file_path=file_path,
            campaign=campaign_name,
            dry_run=True,
            consent_source=consent_source,
        )
        log.info("Clean leads (dry-run) done: %s", clean_res)

        stem = os.path.splitext(os.path.basename(file_path))[0]
        processed_batch_path = os.path.join(
            CLEAN_PROCESSED_DIR, f"{stem}_batch.csv")
        if not os.path.exists(processed_batch_path):
            raise FileNotFoundError(
                f"Cleaned batch file not written to {processed_batch_path}")

        # Live intake: write the leads into the DB (fresh, awaiting a call).
        from intake.live_intake import run as live_run
        live_res = live_run(
            file_path=processed_batch_path,
            campaign=campaign_name,
            consent_source=consent_source,
            dry_run=False,
        )
        log.info("Live intake DB write done: %s", live_res)

        # DNC-check: flag any newly-stored lead whose phone is on the block list.
        # Isolated — a DNC hiccup must not fail the upload (the Dialer re-checks
        # DNC at call time anyway, so this is belt-and-suspenders).
        try:
            from intake.dnc_filter import cmd_scan_db
            cmd_scan_db(dry_run=False)
            log.info("DNC scan-db done after intake")
        except Exception as dnc_exc:
            log.error("DNC scan after intake failed (calls still re-check): %s",
                      dnc_exc)

        return RedirectResponse(url="/", status_code=303)

    except Exception as e:
        log.error("Ingestion failed: %s", e)
        return RedirectResponse(
            url=f"/?error={urllib.parse.quote(str(e))}", status_code=303)


@app.post("/start-calling")
async def start_calling(
    campaign_id: int = Form(...),
    mode: str = Form("dry"),
    confirm: str = Form(""),
):
    """Wire the 'Start Calling' button to the Dialer. SAFE BY DESIGN — the SAME
    gate as the old Release button:
      - mode='dry'  -> read-only preview (count + window); NO calls, NO writes.
      - mode='live' -> requires confirm='yes'; calls launch_campaign(dry_run=False)
                       which enforces the calling window, MAX_ATTEMPTS, and DNC,
                       hands the whole list to Bolna's batch API (Bolna paces it),
                       then moves released leads into the calling state.
    We IMPORT the Dialer (no fork). All SQL lives in the Dialer (voicebrain.*)."""
    try:
        from dialer.dialer import (
            launch_campaign, plan_campaign, launch_campaign_chunked, chunk_plan,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/?error={urllib.parse.quote('dialer import failed: ' + str(e))}",
            status_code=303)

    try:
        if mode == "live":
            if confirm != "yes":
                return RedirectResponse(
                    url="/?error=calling+not+confirmed", status_code=303)

            # Guard: never start a second chunked run on top of a live one.
            prog = read_chunk_progress()
            if prog.get("status") == "running":
                return RedirectResponse(
                    url="/?notice=" + urllib.parse.quote(
                        "A chunked run is already in progress: "
                        + str(prog.get("message", ""))), status_code=303)

            # Plan the paced run for the confirmation message.
            plan = chunk_plan(campaign_id)
            if not plan["count"]:
                return RedirectResponse(
                    url="/?error=" + urllib.parse.quote(
                        "No callable leads — nothing to dial."), status_code=303)

            # Fire it in a BACKGROUND daemon thread so the ~50-min paced run does
            # not block the dashboard and survives the Captain navigating away.
            # The thread writes progress to PROGRESS_PATH after every chunk; the
            # board polls that file. SAME dialer guards apply (window/DNC/attempts)
            # — we IMPORT launch_campaign_chunked (no fork).
            import threading
            threading.Thread(
                target=launch_campaign_chunked,
                kwargs={"campaign_id": campaign_id, "dry_run": False,
                        "progress_path": PROGRESS_PATH},
                daemon=True,
            ).start()

            msg = (f"CHUNKED CALLING STARTED in background — {plan['count']} lead(s) "
                   f"in {plan['n_chunks']} chunks of {plan['chunk_size']}, "
                   f"~{plan['eta_min']} min total (a {plan['delay_sec']}s pace between "
                   "chunks to beat the carrier spam-block). Results stream in as "
                   "each chunk lands — watch the progress bar.")
            return RedirectResponse(
                url=f"/?notice={urllib.parse.quote(msg)}", status_code=303)

        # default: dry-run preview (no calls, no writes). plan_campaign drives
        # the banner; launch_campaign(dry_run=True) prints the FULL two-step batch
        # payload (create + schedule) to the server log — read-only, fires nothing.
        plan = plan_campaign(campaign_id)
        try:
            launch_campaign(campaign_id, dry_run=True)
        except Exception as dry_exc:
            log.error("dry-run preview print failed (harmless): %s", dry_exc)
        win = "OPEN" if plan["window_open"] else "CLOSED"
        msg = (f"DRY RUN — would (1) create a Bolna batch for {plan['count']} "
               f"lead(s) then (2) schedule it to run; window {win}. Both steps "
               "printed to the server log. No calls placed.")
        return RedirectResponse(
            url=f"/?notice={urllib.parse.quote(msg)}", status_code=303)

    except SystemExit as e:
        # Dialer guards (closed window / missing env) raise SystemExit.
        return RedirectResponse(
            url=f"/?error={urllib.parse.quote(str(e))}", status_code=303)
    except Exception as e:
        log.error("Start calling failed: %s", e)
        return RedirectResponse(
            url=f"/?error={urllib.parse.quote(str(e))}", status_code=303)


@app.post("/rate")
async def rate_lead(
    lead_id: int = Form(...),
    stars: int = Form(...),
    note: str = Form(""),
):
    """Layer 3 — Captain's manual star rating. Writes manual_rating + rating_note
    to the LATEST call_result for this lead. voicebrain.* only; no other writes.
    A lead with no completed call yet has no result row to rate -> graceful error."""
    if stars < 1 or stars > 5:
        return RedirectResponse(url="/?error=stars+must+be+1-5", status_code=303)

    try:
        conn = psycopg2.connect(**DB)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM voicebrain.call_results
                    WHERE call_lead_id = %s
                    ORDER BY received_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (lead_id,),
                )
                row = cur.fetchone()
                if not row:
                    return RedirectResponse(
                        url="/?error=no+call+result+to+rate+yet", status_code=303)
                cur.execute(
                    "UPDATE voicebrain.call_results "
                    "SET manual_rating = %s, rating_note = %s WHERE id = %s",
                    (stars, note, row[0]),
                )
        finally:
            conn.close()
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        log.error("Rate failed: %s", e)
        return RedirectResponse(
            url=f"/?error={urllib.parse.quote(str(e))}", status_code=303)


@app.get("/export-verified")
def export_verified(campaign_id: int = None):
    """Download a clean CSV of VERIFIED leads for the sales team. Full (unmasked)
    phone so the team can dial. Same verified rule as the board (latest result per
    lead, verified=true). Read-only; voicebrain.* only. Returns the CSV inline as
    a file download.

    campaign_id (optional) scopes the export to ONE batch — the button on both the
    main board and a History drill-down passes the batch's id so you get exactly
    that batch's verified leads. Omitted -> all verified across every batch."""
    import csv
    import io
    headers = ["name", "phone", "location", "budget", "configuration",
               "possession", "property_type", "interested", "outcome",
               "duration_sec", "cost", "recording_url"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    n = 0
    # Scope to one batch when asked. Parameterised — never string-built.
    camp_clause = " AND cl.campaign_id = %s" if campaign_id is not None else ""
    params = (campaign_id,) if campaign_id is not None else ()
    try:
        conn = psycopg2.connect(**DB)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cl.first_name, cl.phone, cr.location, cr.budget,
                           cr.configuration, cr.possession, cr.property_type,
                           cr.interested, cr.call_outcome, cr.duration_sec,
                           cr.cost, cr.recording_url
                    FROM voicebrain.call_leads cl
                    JOIN LATERAL (
                        SELECT * FROM voicebrain.call_results
                        WHERE call_lead_id = cl.id
                        ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 1
                    ) cr ON true
                    WHERE cl.deleted_at IS NULL AND cr.verified = true
                    """ + camp_clause + """
                    ORDER BY cl.id DESC
                    """,
                    params,
                )
                for r in cur.fetchall():
                    w.writerow([(x if x is not None else "") for x in r])
                    n += 1
        finally:
            conn.close()
    except Exception as e:
        log.error("export-verified failed: %s", e)
        # still return a (header-only) CSV rather than a 500 to the browser
    fname = (f"dhyaan_verified_batch_{campaign_id}.csv"
             if campaign_id is not None else "dhyaan_verified_leads.csv")
    log.info("export-verified: %d verified lead(s) [campaign=%s]", n, campaign_id)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=5006, reload=True)
