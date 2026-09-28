"""
DHYAAN VOICE BRAIN — THE FUNNEL ORCHESTRATOR
============================================
Chains the WHOLE machine for one campaign, in the correct funnel order, with
every stage gated and logged. This is the conductor — it owns NO business logic
of its own; it IMPORTS and calls the proven modules (the sender, the dialer, the
DNC filter). It NEVER forks them.

THE FUNNEL (WhatsApp is FIRST):
  STAGE 1  confirm leads loaded     count Stage-0 ('whatsapp') leads
  STAGE 2  WhatsApp blast           aisensy.sender.send_campaign  (all unsent)
  STAGE 3  reply checkpoint         print funnel state: sent / replied / silent
                                    (the reply-webhook feeds this later)
  STAGE 4  AI-call the SILENT       dialer.launch_campaign  (non-responders)

SAFE BY DEFAULT — the whole run is a DRY RUN unless you pass --go-live. Dry runs
read the DB read-only and print exactly what each stage WOULD do; no WhatsApp,
no call, no write. --go-live flows through to each module, which still enforce
its OWN guards (env, DNC, calling window, MAX_ATTEMPTS) — this orchestrator
weakens NONE of them.

THE KEY RULE (enforced): WhatsApp repliers go STRAIGHT to a human and are NEVER
AI-called. Repliers leave Stage 0 (the reply-webhook moves them funnel_stage->
'human_call'), so the dialer — which only dials 'whatsapp'-stage leads — never
touches them. Stage 4 ADDS a safety assert: if it ever finds a lead that replied
but is STILL at the whatsapp stage, it REFUSES to go live and tells you, because
that means the stage transition didn't happen and a replier could get AI-called.

Isolation: only ever reads voicebrain.* — never realestate_db.private.

CLI:
  python run_funnel.py --campaign-id 2                 # dry run, all 4 stages
  python run_funnel.py --campaign-id 2 --limit 5       # dry, WhatsApp capped at 5
  python run_funnel.py --campaign-id 2 --go-live       # FIRE (each guard applies)
"""
import os
import sys
import argparse

# Load .env BEFORE importing config / the modules (they read os.getenv at import).
from dotenv import load_dotenv  # noqa: E402
load_dotenv()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import DB  # noqa: E402

# IMPORT the proven modules — never fork them.
from aisensy.sender import send_campaign            # noqa: E402  (Stage 2)
from dialer.dialer import launch_campaign           # noqa: E402  (Stage 4)

WHATSAPP_STAGE = "whatsapp"

# --- SQL (constants so the isolation guard below can scan them) --------------
# Full breakdown — needs migration 008 (whatsapp_failed_at). Columns, in order:
#   stage0_total, delivered, replied_total, silent, failed, pending,
#   replied_at_stage0, dnc_at_stage0, human_call, ai_call, closed
SQL_FUNNEL_STATE = """
    SELECT
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp')                        AS stage0_total,
        COUNT(*) FILTER (WHERE whatsapp_sent_at IS NOT NULL)                      AS delivered,
        COUNT(*) FILTER (WHERE whatsapp_replied = true)                          AS replied_total,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND whatsapp_sent_at IS NOT NULL
                         AND whatsapp_replied = false)                           AS silent,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND whatsapp_failed_at IS NOT NULL
                         AND whatsapp_sent_at IS NULL
                         AND whatsapp_replied = false)                           AS failed,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND whatsapp_sent_at IS NULL
                         AND whatsapp_failed_at IS NULL
                         AND whatsapp_replied = false)                           AS pending,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND whatsapp_replied = true)                            AS replied_at_stage0,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND COALESCE(dnc, false) = true)                        AS dnc_at_stage0,
        COUNT(*) FILTER (WHERE funnel_stage = 'human_call')                      AS human_call,
        COUNT(*) FILTER (WHERE funnel_stage = 'ai_call')                         AS ai_call,
        COUNT(*) FILTER (WHERE funnel_stage = 'closed')                          AS closed
    FROM voicebrain.call_leads
    WHERE campaign_id = %s AND deleted_at IS NULL
"""
# Fallback for DBs WITHOUT migration 008: failed=0, and the would-be-failed leads
# fall into 'pending' (we can't distinguish them without the column). Same column
# order/count as SQL_FUNNEL_STATE so one key list maps either result.
SQL_FUNNEL_STATE_NOFAIL = """
    SELECT
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp')                        AS stage0_total,
        COUNT(*) FILTER (WHERE whatsapp_sent_at IS NOT NULL)                      AS delivered,
        COUNT(*) FILTER (WHERE whatsapp_replied = true)                          AS replied_total,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND whatsapp_sent_at IS NOT NULL
                         AND whatsapp_replied = false)                           AS silent,
        0                                                                        AS failed,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND whatsapp_sent_at IS NULL
                         AND whatsapp_replied = false)                           AS pending,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND whatsapp_replied = true)                            AS replied_at_stage0,
        COUNT(*) FILTER (WHERE funnel_stage = 'whatsapp'
                         AND COALESCE(dnc, false) = true)                        AS dnc_at_stage0,
        COUNT(*) FILTER (WHERE funnel_stage = 'human_call')                      AS human_call,
        COUNT(*) FILTER (WHERE funnel_stage = 'ai_call')                         AS ai_call,
        COUNT(*) FILTER (WHERE funnel_stage = 'closed')                          AS closed
    FROM voicebrain.call_leads
    WHERE campaign_id = %s AND deleted_at IS NULL
"""
SQL_CAMPAIGN_NAME = (
    "SELECT name FROM voicebrain.campaigns WHERE id = %s AND deleted_at IS NULL"
)

# ISOLATION GUARD — this orchestrator must NEVER reference the portal's schema.
for _sql in (SQL_FUNNEL_STATE, SQL_FUNNEL_STATE_NOFAIL, SQL_CAMPAIGN_NAME):
    _low = _sql.lower()
    assert "private" not in _low and "realestate_db" not in _low, (
        "[FUNNEL] ISOLATION VIOLATION — SQL references a forbidden schema/db"
    )
    assert "voicebrain." in _low, "[FUNNEL] SQL must be voicebrain-qualified"


# --- read-only state --------------------------------------------------------
def funnel_state(campaign_id):
    """Read-only snapshot of where this campaign's leads sit in the funnel.
    Tries the full breakdown (needs migration 008); if whatsapp_failed_at isn't
    there yet it falls back to the no-fail query and flags failed_tracked=False."""
    import psycopg2
    conn = psycopg2.connect(**DB)
    failed_tracked = True
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_CAMPAIGN_NAME, (campaign_id,))
            row = cur.fetchone()
            name = row[0] if row else None
            try:
                cur.execute("SAVEPOINT sp_fs")
                cur.execute(SQL_FUNNEL_STATE, (campaign_id,))
                c = cur.fetchone()
                cur.execute("RELEASE SAVEPOINT sp_fs")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_fs")
                failed_tracked = False
                cur.execute(SQL_FUNNEL_STATE_NOFAIL, (campaign_id,))
                c = cur.fetchone()
    finally:
        conn.close()
    keys = ["stage0_total", "delivered", "replied_total", "silent", "failed",
            "pending", "replied_at_stage0", "dnc_at_stage0", "human_call",
            "ai_call", "closed"]
    state = dict(zip(keys, c)) if c else {k: 0 for k in keys}
    state["ai_pool"] = state["silent"] + state["failed"]   # the AI-call batch
    state["unsent"] = state["pending"] + state["failed"]   # not yet delivered
    state["failed_tracked"] = failed_tracked
    state["name"] = name
    return state


def banner(title, dry_run):
    print("\n" + "#" * 60)
    print(f"#  {title}" + ("   [DRY RUN]" if dry_run else "   [LIVE]"))
    print("#" * 60)


def print_state(state):
    fail_disp = (str(state['failed']) if state['failed_tracked']
                 else "n/a — run 008 (counted under 'not attempted')")
    print("  funnel state (voicebrain.call_leads, live rows):")
    print(f"    Stage-0 total (whatsapp)     : {state['stage0_total']}")
    print(f"      ├─ delivered (sent-ok)     : {state['delivered']}")
    print(f"      │    ├─ replied (HOT→human): {state['replied_total']}")
    print(f"      │    └─ silent (→ AI call) : {state['silent']}")
    print(f"      ├─ failed send (→ AI call) : {fail_disp}")
    print(f"      ├─ not yet attempted       : {state['pending']}")
    print(f"      └─ DNC at stage0           : {state['dnc_at_stage0']}")
    print(f"    >> AI-CALL POOL (silent+failed): {state['ai_pool']}")
    print(f"    moved → human_call           : {state['human_call']}")
    print(f"    moved → ai_call              : {state['ai_call']}")
    print(f"    closed                       : {state['closed']}")


# --- the orchestration ------------------------------------------------------
def run_funnel(campaign_id, dry_run=True, limit=None):
    print("=" * 60)
    print("  DHYAAN VOICE BRAIN — FUNNEL ORCHESTRATOR")
    print("=" * 60)
    print(f"  DB target : {DB['user']}@{DB['host']}:{DB['port']}/{DB['dbname']} "
          f"(schema voicebrain — never private)")
    print(f"  campaign  : {campaign_id}")
    print(f"  mode      : {'DRY RUN — nothing fires' if dry_run else 'LIVE — guards still apply'}")
    print("=" * 60)

    state = funnel_state(campaign_id)
    if state["name"] is None:
        raise SystemExit(f"[FUNNEL] FATAL — no live campaign with id={campaign_id}.")
    print(f"  campaign name: '{state['name']}'")

    # ---- STAGE 1 — confirm leads loaded ------------------------------------
    banner("STAGE 1 — confirm leads loaded", dry_run)
    print_state(state)
    if state["stage0_total"] == 0:
        print("\n[FUNNEL] STOP — no Stage-0 ('whatsapp') leads for this campaign. "
              "Run intake first. Nothing to do.")
        return
    print(f"\n[FUNNEL] STAGE 1 OK — {state['stage0_total']} lead(s) at Stage 0.")

    # ---- STAGE 2 — WhatsApp blast (delegates to the sender) -----------------
    banner("STAGE 2 — WhatsApp blast (AiSensy)", dry_run)
    if state["unsent"] == 0:
        print("[FUNNEL] STAGE 2 SKIP — every Stage-0 lead already WhatsApp'd "
              "(whatsapp_sent_at set). Nothing to blast.")
    else:
        print(f"[FUNNEL] delegating {state['unsent']} not-yet-delivered lead(s) "
              f"({state['pending']} never tried + {state['failed']} prior-failed) "
              "to aisensy.sender.send_campaign (its env/DNC guards apply)...")
        send_campaign(campaign_id, dry_run=dry_run, limit=limit)

    # ---- STAGE 3 — reply checkpoint ----------------------------------------
    banner("STAGE 3 — reply checkpoint", dry_run)
    state = funnel_state(campaign_id)  # refresh after the blast
    print_state(state)
    print("\n[FUNNEL] STAGE 3 — the AiSensy reply-catcher marks repliers "
          "(whatsapp_replied=true) and moves them funnel_stage->'human_call'.")
    if state["replied_total"] > 0:
        print(f"  {state['replied_total']} replied lead(s) are HOT — route to a "
              "HUMAN. They must NOT be AI-called (the KEY RULE).")
    print(f"  AI-call pool for Stage 4 = {state['ai_pool']}  "
          f"({state['silent']} silent + {state['failed']} failed-send).")

    # ---- STAGE 4 — AI-call the SILENT non-responders (delegates to dialer) --
    banner("STAGE 4 — AI-call the silent (Bolna)", dry_run)

    # KEY-RULE safety: a replier still parked at the whatsapp stage would be
    # swept into the dialer's call list (it dials ALL whatsapp-stage leads).
    # Refuse a LIVE dial until the stage transition has happened.
    if not dry_run and state["replied_at_stage0"] > 0:
        raise SystemExit(
            f"[FUNNEL] BLOCKED (KEY RULE) — {state['replied_at_stage0']} lead(s) "
            "replied to WhatsApp but are STILL at funnel_stage='whatsapp'. The "
            "dialer dials every whatsapp-stage lead, so going live now could "
            "AI-call a replier. Move repliers to 'human_call' first (the reply-"
            "webhook does this), then re-run Stage 4."
        )

    if state["ai_pool"] == 0:
        print("[FUNNEL] STAGE 4 SKIP — AI-call pool is empty (no silent or "
              "failed-send leads at the whatsapp stage yet).")
    else:
        print(f"[FUNNEL] delegating {state['ai_pool']} lead(s) "
              f"({state['silent']} silent + {state['failed']} failed-send) to "
              "dialer.launch_campaign — it batch-calls callable whatsapp-stage "
              "leads, excluding repliers/DNC (its window/attempts guards apply)...")
        launch_campaign(campaign_id, dry_run=dry_run)

    # ---- final state -------------------------------------------------------
    banner("FUNNEL COMPLETE — final state", dry_run)
    print_state(funnel_state(campaign_id))
    if dry_run:
        print("\n[FUNNEL] DRY RUN complete — nothing was sent, called, or written.")
    else:
        print("\n[FUNNEL] LIVE run complete — see each stage's log above for what fired.")


# --- CLI --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        prog="python run_funnel.py",
        description="Dhyaan Voice Brain — full funnel orchestrator. DRY-RUN by "
                    "default; --go-live fires (each module's guards still apply).")
    ap.add_argument("--campaign-id", type=int, required=True,
                    help="campaign to run through the funnel")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stage 2: cap how many WhatsApp messages to send")
    ap.add_argument("--go-live", action="store_true",
                    help="actually fire each stage (default is dry-run)")
    args = ap.parse_args()
    run_funnel(args.campaign_id, dry_run=not args.go_live, limit=args.limit)


if __name__ == "__main__":
    main()
