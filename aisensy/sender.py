"""
DHYAAN VOICE BRAIN — THE WHATSAPP SENDER (AiSensy)
==================================================
Stage 0 of the funnel: the WhatsApp BLAST. We message ALL fresh leads first;
the ones who REPLY go straight to a human, the SILENT ones get the AI caller.
This module sends that first WhatsApp via AiSensy's template campaign API.

SAFE BY DEFAULT — exactly like the Dialer's --go-live discipline. Every run is
a DRY RUN unless you pass --go-live. A dry run pulls the lead list read-only and
prints EXACTLY what it WOULD POST to AiSensy (destination + payload shape) — it
never calls the API and never writes a single row.

Two entry points:
  send_one(phone, first_name)         POST one template message to AiSensy
  send_campaign(campaign_id, ...)     blast every un-sent Stage-0 lead, rate-safe

WHO WE SEND TO (the "to send" set, read-only filter):
  voicebrain.call_leads WHERE
      funnel_stage   = 'whatsapp'      (Stage 0 — fresh intake)
    AND deleted_at     IS NULL         (live rows only)
    AND whatsapp_sent_at IS NULL       (never WhatsApp'd before — no double-send)
    AND whatsapp_failed_at IS NULL     (never failed before — a prior failure makes
                                        the lead AI-only; no WhatsApp retry)
    AND COALESCE(dnc, false) = false   (Gate 4: do-not-contact, column)
  ...then we ALSO drop anything on the dnc_master block list. We IMPORT
  intake.dnc_filter.dnc_phones(); we never fork it.

On a successful send we stamp that lead's whatsapp_sent_at = now() and COMMIT it
immediately — so a crash mid-blast can never re-send an already-delivered number.

Guards:
  - ENV: AISENSY_API_KEY / AISENSY_API_URL / AISENSY_CAMPAIGN_NAME must all be
         set for a LIVE send; missing/empty -> fail loud naming the var.
  - SAVEPOINT-safe: each lead's UPDATE runs inside its own savepoint; one bad
         row rolls back to the savepoint and the blast continues.

Isolation: only ever reads/writes voicebrain.* — never realestate_db.private.

CLI:
  python -m aisensy.sender --campaign-id 2                    # dry run (default)
  python -m aisensy.sender --campaign-id 2 --limit 5          # dry run, first 5
  python -m aisensy.sender --campaign-id 2 --limit 1 --go-live  # real send (1)
  python -m aisensy.sender --phone +919876543210 --name Harshad           # dry
  python -m aisensy.sender --phone +919876543210 --name Harshad --go-live  # real
"""
import os
import sys
import time
import argparse
from datetime import datetime

# Load .env BEFORE importing config — config reads os.getenv at import time, so
# the .env values must already be in os.environ when that runs.
from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import requests  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (  # noqa: E402
    DB, AISENSY_API_KEY, AISENSY_API_URL, AISENSY_CAMPAIGN_NAME,
)

WHATSAPP_STAGE = "whatsapp"     # Stage 0 — the blast target stage
NAME_FALLBACK  = "there"        # used when a lead has no first_name
SEND_DELAY_SEC = 1.0            # rate-limit-safe pause between live sends
HTTP_TIMEOUT   = 30

# --- SQL (constants so the isolation guard below can scan them) --------------
# PRIMARY "to send" set: Stage-0 leads never delivered AND never failed before.
# A lead that already FAILED a send is AI-only — it stays at the whatsapp stage
# (whatsapp_failed_at set) so the dialer calls it; we do NOT WhatsApp-retry it.
# whatsapp_failed_at needs migration 008; if absent we fall back to RETRY below.
SQL_SELECT_LEADS = (
    "SELECT id, phone, first_name "
    "FROM voicebrain.call_leads "
    "WHERE campaign_id = %s AND funnel_stage = %s "
    "  AND deleted_at IS NULL AND whatsapp_sent_at IS NULL "
    "  AND whatsapp_failed_at IS NULL "
    "  AND COALESCE(dnc, false) = false "
    "ORDER BY id"
)
# FALLBACK for DBs WITHOUT migration 008 (no whatsapp_failed_at column): the old
# behaviour where a failed lead is eligible for a WhatsApp retry. Used only if the
# primary query errors on the missing column.
SQL_SELECT_LEADS_RETRY = (
    "SELECT id, phone, first_name "
    "FROM voicebrain.call_leads "
    "WHERE campaign_id = %s AND funnel_stage = %s "
    "  AND deleted_at IS NULL AND whatsapp_sent_at IS NULL "
    "  AND COALESCE(dnc, false) = false "
    "ORDER BY id"
)
SQL_MARK_SENT = (  # (migration 006) success: stamp delivered time
    "UPDATE voicebrain.call_leads SET whatsapp_sent_at = now() WHERE id = %s"
)
# (migration 008) failure: record it DISTINCTLY — never stamp whatsapp_sent_at on
# a failure, or we'd wrongly mark the lead as messaged. A failed lead stays
# whatsapp_sent_at IS NULL + funnel_stage='whatsapp' so the dialer AI-calls them.
SQL_MARK_FAILED = (
    "UPDATE voicebrain.call_leads "
    "SET whatsapp_failed_at = now(), whatsapp_error = %s WHERE id = %s"
)
# (migration 008) a successful (re)send clears any prior failure — latest wins.
SQL_CLEAR_FAILED = (
    "UPDATE voicebrain.call_leads "
    "SET whatsapp_failed_at = NULL, whatsapp_error = NULL WHERE id = %s"
)

# ISOLATION GUARD — this module must NEVER reference the portal's schema.
for _sql in (SQL_SELECT_LEADS, SQL_SELECT_LEADS_RETRY,
             SQL_MARK_SENT, SQL_MARK_FAILED, SQL_CLEAR_FAILED):
    _low = _sql.lower()
    assert "private" not in _low and "realestate_db" not in _low, (
        "[AISENSY] ISOLATION VIOLATION — SQL references a forbidden schema/db"
    )
    assert "voicebrain." in _low, "[AISENSY] SQL must be voicebrain-qualified"


# --- guards -----------------------------------------------------------------
def require_env(live: bool):
    """For a LIVE send all three AiSensy vars must be set. For a dry run we only
    WARN so the payload can still be previewed; the message names what to set."""
    missing = [name for name, val in (
        ("AISENSY_API_KEY", AISENSY_API_KEY),
        ("AISENSY_API_URL", AISENSY_API_URL),
        ("AISENSY_CAMPAIGN_NAME", AISENSY_CAMPAIGN_NAME),
    ) if not (val or "").strip()]
    if not missing:
        return
    msg = ("[AISENSY] missing/empty env var(s): " + ", ".join(missing)
           + "\n  Set them in .env (gitignored): "
           + ", ".join(f"{m}=<value>" for m in missing))
    if live:
        raise SystemExit("[AISENSY] FATAL — cannot send a LIVE WhatsApp.\n" + msg)
    print(msg + "\n  (dry run continues so you can eyeball the payload shape.)")


def print_target(dry_run: bool, mode: str):
    print("=" * 56)
    print(f"  AISENSY SENDER — {mode}" + ("   [DRY RUN]" if dry_run else "   [LIVE]"))
    print("=" * 56)
    print(f"  host     : {DB['host']}")
    print(f"  port     : {DB['port']}")
    print(f"  dbname   : {DB['dbname']}")
    print(f"  user     : {DB['user']}")
    print(f"  schema   : voicebrain   (all I/O voicebrain.* — never private)")
    print(f"  endpoint : POST {AISENSY_API_URL or '<AISENSY_API_URL not set>'}")
    print(f"  campaign : {AISENSY_CAMPAIGN_NAME or '<AISENSY_CAMPAIGN_NAME not set>'}")
    print(f"  apiKey   : {'<set>' if (AISENSY_API_KEY or '').strip() else '<AISENSY_API_KEY not set>'}")
    print(f"  mode     : {'DRY RUN — no API call, no DB writes' if dry_run else 'LIVE SEND'}")
    print("=" * 56)


# --- payload helpers --------------------------------------------------------
def to_destination(phone: str) -> str:
    """AiSensy wants the number WITH country code but WITHOUT the leading '+'
    (e.g. +919876543210 -> 919876543210). Strip a leading '+' and any spaces."""
    return (phone or "").strip().lstrip("+").replace(" ", "")


def resolve_name(first_name: str) -> str:
    """A non-blank greeting name; falls back to 'there' so {{1}} is never empty."""
    name = (first_name or "").strip()
    return name if name else NAME_FALLBACK


def build_payload(phone: str, first_name: str) -> dict:
    """The exact JSON body AiSensy's v2 campaign API expects. templateParams maps
    positionally to the template — our template uses {{1}} = first name only."""
    name = resolve_name(first_name)
    return {
        "apiKey": AISENSY_API_KEY,
        "campaignName": AISENSY_CAMPAIGN_NAME,
        "destination": to_destination(phone),
        "userName": name,
        "templateParams": [name],
    }


def _redacted(payload: dict) -> dict:
    """A copy safe to print — never leak the API key to the console/logs."""
    safe = dict(payload)
    safe["apiKey"] = "<set>" if (payload.get("apiKey") or "").strip() else "<not set>"
    return safe


# --- send_one ---------------------------------------------------------------
def send_one(phone, first_name, dry_run=False):
    """POST one WhatsApp template message to AiSensy. Returns AiSensy's parsed
    JSON response (or {} if the body is empty). In dry_run it prints the payload
    shape and returns None WITHOUT calling the API."""
    require_env(live=not dry_run)
    payload = build_payload(phone, first_name)

    if dry_run:
        print(f"  [DRY] would POST -> {_redacted(payload)}")
        return None

    r = requests.post(
        AISENSY_API_URL,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    print(f"  [AISENSY] {to_destination(phone)} -> {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json() if r.content else {}


# --- lead list (read-only) --------------------------------------------------
def fetch_unsent_leads(campaign_id, limit=None):
    """Read-only pull of this campaign's Stage-0 leads still needing a WhatsApp:
    never delivered AND never failed before (a prior failure makes the lead
    AI-only — see SQL_SELECT_LEADS) and not do-not-contact. We IMPORT
    dnc_filter.dnc_phones() for the block list (Gate 4) and never fork it. limit
    caps the count AFTER the DNC drop. Falls back to the retry query on a DB
    without migration 008 (no whatsapp_failed_at column)."""
    import psycopg2
    # DNC block list. Defensive: if dnc_master doesn't exist yet, the column
    # filter in the SQL still applies and we fall back to an empty block set.
    blocked = set()
    try:
        from intake.dnc_filter import dnc_phones
        blocked = dnc_phones()
    except Exception:
        blocked = set()

    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SAVEPOINT sp_unsent")
                cur.execute(SQL_SELECT_LEADS, (campaign_id, WHATSAPP_STAGE))
                raw = cur.fetchall()
                cur.execute("RELEASE SAVEPOINT sp_unsent")
            except Exception:
                # No whatsapp_failed_at column (pre-008) — fall back to retry set.
                cur.execute("ROLLBACK TO SAVEPOINT sp_unsent")
                cur.execute(SQL_SELECT_LEADS_RETRY, (campaign_id, WHATSAPP_STAGE))
                raw = cur.fetchall()
            rows = [{"id": r[0], "phone": r[1], "first_name": r[2] or ""}
                    for r in raw]
    finally:
        conn.close()

    # belt-and-suspenders: drop anything on the dnc_master list too.
    rows = [r for r in rows if r["phone"] not in blocked]
    if limit is not None:
        rows = rows[:limit]
    return rows


# --- savepoint-safe single statement ----------------------------------------
def _try_stmt(conn, sql, args, sp):
    """Run one statement inside its own SAVEPOINT. Returns True on success; on any
    failure it rolls back to the savepoint (leaving the surrounding transaction
    usable) and returns False. Lets the failure-tracking columns be best-effort:
    if migration 008 hasn't run yet, the stamp simply no-ops instead of aborting
    the whole blast."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {sp}")
            cur.execute(sql, args)
            cur.execute(f"RELEASE SAVEPOINT {sp}")
        return True
    except Exception:
        try:
            with conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        except Exception:
            conn.rollback()
        return False


# --- send_campaign ----------------------------------------------------------
def send_campaign(campaign_id, dry_run=True, limit=None):
    """Blast the WhatsApp template to every un-sent Stage-0 lead of a campaign.
    Rate-limit-safe (small delay between live sends). On each success the lead's
    whatsapp_sent_at is stamped and COMMITTED immediately so a mid-blast crash
    never re-sends. SAVEPOINT-safe: a bad row rolls back to its savepoint and the
    blast continues."""
    require_env(live=not dry_run)
    leads = fetch_unsent_leads(campaign_id, limit=limit)

    print_target(dry_run, "send_campaign")
    print(f"  campaign_id : {campaign_id}")
    print(f"  limit       : {limit if limit is not None else 'none (all)'}")
    print(f"  to send     : {len(leads)}  (stage='{WHATSAPP_STAGE}', "
          f"not sent, not failed, not DNC)")
    print(f"  delay       : {SEND_DELAY_SEC}s between live sends")
    preview = [to_destination(l["phone"]) for l in leads[:5]]
    print(f"  first 5 #   : {preview}")
    print("=" * 56)

    if not leads:
        print("[AISENSY] Nothing to send (no un-sent Stage-0 leads, or all DNC).")
        return {"sent": 0, "failed": 0, "skipped": 0, "total": 0}

    if dry_run:
        print("[AISENSY] DRY RUN — payload preview for each lead below. "
              "No API call, no DB write, nothing stamped.\n")
        for ld in leads:
            print(f"  lead #{ld['id']}  name='{resolve_name(ld['first_name'])}'")
            send_one(ld["phone"], ld["first_name"], dry_run=True)
        print(f"\n[AISENSY] DRY RUN — would send {len(leads)} WhatsApp message(s). "
              "No API call, no DB write.")
        return {"sent": 0, "failed": 0, "skipped": len(leads), "total": len(leads)}

    # --- LIVE path ----------------------------------------------------------
    import psycopg2
    sent = failed = 0
    conn = psycopg2.connect(**DB)
    try:
        for idx, ld in enumerate(leads):
            try:
                send_one(ld["phone"], ld["first_name"], dry_run=False)
            except Exception as e:
                # FAILED send: the lead was NOT contacted. Record it DISTINCTLY —
                # do NOT stamp whatsapp_sent_at. They stay funnel_stage='whatsapp'
                # with whatsapp_sent_at NULL, so the dialer AI-calls them next to
                # the silent non-responders (same batch). Failure stamp needs 008;
                # if absent it no-ops (the lead is still correctly NOT marked sent).
                failed += 1
                err = f"{type(e).__name__}: {e}"[:500]
                print(f"  [AISENSY] SEND FAILED lead #{ld['id']} "
                      f"({to_destination(ld['phone'])}): {err}")
                if _try_stmt(conn, SQL_MARK_FAILED, (err, ld["id"]), "sp_fail"):
                    conn.commit()
                else:
                    conn.rollback()
                    print("    (whatsapp_failed_at not tracked — run migration 008 "
                          "to record failures; routing to AI-call still works.)")
                if idx < len(leads) - 1:
                    time.sleep(SEND_DELAY_SEC)
                continue

            # Send accepted -> stamp delivered + clear any prior failure, commit
            # immediately (no double-send ever). The clear is best-effort (008).
            if _try_stmt(conn, SQL_MARK_SENT, (ld["id"],), "sp_mark"):
                _try_stmt(conn, SQL_CLEAR_FAILED, (ld["id"],), "sp_clear")
                conn.commit()
                sent += 1
            else:
                conn.rollback()
                print(f"  [AISENSY] WARN — sent but FAILED to stamp lead "
                      f"#{ld['id']}  (may re-send on next run — verify!)")

            # rate-limit-safe pause (skip after the last one).
            if idx < len(leads) - 1:
                time.sleep(SEND_DELAY_SEC)
    finally:
        conn.close()

    print("\n" + "=" * 56)
    print("  AISENSY SEND SUMMARY")
    print("=" * 56)
    print(f"  campaign_id    : {campaign_id}")
    print(f"  attempted      : {len(leads)}")
    print(f"  SENT+stamped   : {sent}")
    print(f"  FAILED+stamped : {failed}  (-> AI-call pool, same batch as silent)")
    print(f"  finished       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 56)
    return {"sent": sent, "failed": failed, "skipped": 0, "total": len(leads)}


# --- send_one_test (single-shot, no DB) -------------------------------------
def send_one_test(phone, first_name, dry_run=True):
    """Fire ONE WhatsApp to an arbitrary number — for testing the AiSensy wiring
    against your own phone WITHOUT staging a lead. Same env guard + format + name
    fallback as the campaign path. Touches NO database (no read, no write)."""
    require_env(live=not dry_run)
    print("=" * 56)
    print("  AISENSY SENDER — single-shot" + ("   [DRY RUN]" if dry_run else "   [LIVE]"))
    print("=" * 56)
    print(f"  endpoint : POST {AISENSY_API_URL or '<AISENSY_API_URL not set>'}")
    print(f"  campaign : {AISENSY_CAMPAIGN_NAME or '<AISENSY_CAMPAIGN_NAME not set>'}")
    print(f"  apiKey   : {'<set>' if (AISENSY_API_KEY or '').strip() else '<not set>'}")
    print(f"  phone    : {phone}  ->  {to_destination(phone)}")
    print(f"  name     : {resolve_name(first_name)}")
    print(f"  DB       : (none — single-shot test touches no database)")
    print("=" * 56)
    resp = send_one(phone, first_name, dry_run=dry_run)
    if dry_run:
        print("[AISENSY] DRY RUN — no API call made, no DB write.")
    else:
        print(f"[AISENSY] LIVE — single-shot sent. response: {resp}")
    return resp


# --- CLI --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        prog="python -m aisensy.sender",
        description="Dhyaan Voice Brain — WhatsApp sender (AiSensy, Stage 0). "
                    "DRY-RUN by default; pass --go-live to actually send.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--campaign-id", type=int,
                      help="campaign whose Stage-0 leads to WhatsApp (blast mode)")
    mode.add_argument("--phone",
                      help="single-shot: send ONE WhatsApp to this E.164 number "
                           "(test mode, no DB)")
    ap.add_argument("--name", default="",
                    help="first name for --phone single-shot (default: 'there')")
    ap.add_argument("--limit", type=int, default=None,
                    help="campaign mode: cap how many leads to send (after DNC drop)")
    ap.add_argument("--go-live", action="store_true",
                    help="actually send real WhatsApp (default is dry-run)")
    args = ap.parse_args()

    if args.phone:
        send_one_test(args.phone, args.name, dry_run=not args.go_live)
    else:
        send_campaign(args.campaign_id, dry_run=not args.go_live, limit=args.limit)


if __name__ == "__main__":
    main()
