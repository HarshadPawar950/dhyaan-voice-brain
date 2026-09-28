"""
DHYAAN VOICE BRAIN — central config.
Secrets come from environment variables. NEVER hardcode the API key.
Set them in a .env file (gitignored) or Windows env vars.
"""
import os

# Load .env automatically so EVERY entry point (catcher, dashboard, dialer, and
# one-off scripts) gets secrets without each having to call load_dotenv itself.
# This was the bug that silently dropped a whole batch of webhooks: the catcher
# was launched in a shell without .env, so VB_DB_PASS was empty and every DB
# write failed with "no password supplied" while raw payloads piled up on disk.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    # python-dotenv is optional in prod if real env vars are set another way.
    pass

# --- Bolna ---
BOLNA_API_KEY   = os.getenv("BOLNA_API_KEY", "")          # Bearer token
BOLNA_AGENT_ID  = os.getenv("BOLNA_AGENT_ID", "")         # Dhyan agent UUID (default agent)
BOLNA_BASE_URL  = "https://api.bolna.ai"
FROM_PHONE      = os.getenv("BOLNA_FROM_PHONE", "")       # your purchased +91 number

# --- Named agents (multi-caller). Dhyan defaults to BOLNA_AGENT_ID so nothing
#     breaks if the named slot is unset. Priya is the 2nd caller — set its UUID
#     in .env as BOLNA_AGENT_PRIYA once created in Bolna. Add more the same way. ---
BOLNA_AGENT_DHYAN = os.getenv("BOLNA_AGENT_DHYAN", "") or BOLNA_AGENT_ID
BOLNA_AGENT_PRIYA = os.getenv("BOLNA_AGENT_PRIYA", "")
BOLNA_AGENTS = {name: uuid for name, uuid in {
    "dhyan": BOLNA_AGENT_DHYAN,
    "priya": BOLNA_AGENT_PRIYA,
}.items() if uuid}                                        # only configured agents

# --- Per-caller FROM phone numbers (for the BATCH path). ---
# POST /call takes NO from-number — a single call dials from whatever number is
# saved on the agent inside Bolna. But POST /batches sends from_phone_numbers
# EXPLICITLY, so a batch must know each caller's own number. Dhyan defaults to
# BOLNA_FROM_PHONE (unchanged); Priya uses her purchased 2nd number. A caller
# with NO from-phone here is HARD-STOPPED before a batch (never silently dials
# from the wrong number). Add more callers the same way.
BOLNA_FROM_PHONE_DHYAN = os.getenv("BOLNA_FROM_PHONE_DHYAN", "") or FROM_PHONE
BOLNA_FROM_PHONE_PRIYA = os.getenv("BOLNA_FROM_PHONE_PRIYA", "")
BOLNA_FROM_PHONES = {name: num for name, num in {
    "dhyan": BOLNA_FROM_PHONE_DHYAN,
    "priya": BOLNA_FROM_PHONE_PRIYA,
}.items() if num}                                        # only configured numbers

# --- AiSensy (WhatsApp blast + reply webhook — config slots only for now) ---
AISENSY_API_KEY       = os.getenv("AISENSY_API_KEY", "")        # API key from AiSensy dashboard
AISENSY_API_URL       = os.getenv("AISENSY_API_URL",
                                  "https://backend.aisensy.com/campaign/t1/api/v2")
AISENSY_CAMPAIGN_NAME = os.getenv("AISENSY_CAMPAIGN_NAME", "")  # exact Live API Campaign name

# --- Database (reuse Command Center's Postgres server, isolated schema) ---
DB = {
    "host":     os.getenv("VB_DB_HOST", "localhost"),
    "port":     int(os.getenv("VB_DB_PORT", "5432")),
    "dbname":   os.getenv("VB_DB_NAME", "realestate_db"),
    "user":     os.getenv("VB_DB_USER", "postgres"),
    "password": os.getenv("VB_DB_PASS", ""),
}

# --- Catcher (webhook server) ---
CATCHER_HOST = "0.0.0.0"
CATCHER_PORT = int(os.getenv("VB_CATCHER_PORT", "5005"))   # avoid portal's 3000

# --- Dashboard (control panel) ---
DASHBOARD_PORT = int(os.getenv("VB_DASHBOARD_PORT", "5006"))   # portal owns 3000

# --- Tunnel (the PUBLIC https URL Bolna POSTs webhooks to) ---
# The Catcher only ever sees a webhook if Bolna can reach it on a public URL.
# Two modes, switched by VB_TUNNEL_MODE so you are unblocked today AND have a
# zero-churn upgrade path:
#   quick : free cloudflared quick tunnel. A RANDOM *.trycloudflare.com URL is
#           minted on EVERY launch, so start_voicebrain prints it for you to
#           paste into Bolna. Fine for testing; the URL dies on restart.
#   named : a cloudflared NAMED tunnel bound to your own subdomain (you own
#           your-domain.com). The URL is STABLE —
#           set VB_PUBLIC_URL to e.g. https://catcher.example.com, paste it into
#           Bolna ONCE, and it never changes again across reboots.
TUNNEL_MODE  = os.getenv("VB_TUNNEL_MODE", "quick").strip().lower()   # quick | named
TUNNEL_NAME  = os.getenv("VB_TUNNEL_NAME", "")    # cloudflared named-tunnel name (mode=named)
PUBLIC_URL   = os.getenv("VB_PUBLIC_URL", "").rstrip("/")  # stable https base (mode=named)
WEBHOOK_PATH = "/webhook/bolna"                   # the route Bolna must call

# --- Bolna balance warning (shown on the dashboard + Start Calling modal) ---
# Bolna exposes GET /user/me with a "wallet" float (₹). Below this many rupees
# the dashboard flashes a low-balance warning so a batch never dies mid-run for
# want of credit. Display only — it never blocks a launch.
LOW_BALANCE_THRESHOLD = float(os.getenv("VB_LOW_BALANCE_THRESHOLD", "200"))

# --- Calling rules (compliance) ---
CALL_WINDOW_START = 10   # 10:00 — never dial before
CALL_WINDOW_END   = 19   # 19:00 — never dial after
MAX_ATTEMPTS      = 2     # retry a no-answer at most this many times

# --- Failed-call retry / rescheduling ---
# A call that NEVER CONNECTED (no-answer / busy / network-failed / not-connected)
# is auto-requeued for another attempt — up to MAX_ATTEMPTS, and only after a
# cool-off gap so we don't hammer the same number back-to-back. We NEVER retry a
# lead who connected+talked, said not-interested, or is on DNC (see
# dialer.requeue_failed_leads — the single source of truth for eligibility).
# The gap is in MINUTES; the actual re-dial still obeys the calling window.
RETRY_DELAY_MIN = int(os.getenv("VB_RETRY_DELAY_MIN", "60"))   # cool-off before a retry

# --- Verification rule (what makes a lead "verified") ---
# A lead is verified ONLY when it carries a REAL requirement (clean budget /
# location / configuration) AND the call was a genuine exchange — not a 10-second
# blip where a stray number got mis-extracted. This is the minimum talk-time (sec)
# we accept as "genuine engagement" when the lead didn't explicitly say yes.
# Tune in .env if real verified leads are being missed (lower) or junk slips
# through (raise). See intake/verify.is_verified — the single source of truth.
VERIFY_MIN_DURATION_SEC = int(os.getenv("VB_VERIFY_MIN_DURATION_SEC", "30"))

# --- Auto-chunking (beat the carrier spam-block) ---
# Big batches (e.g. 57) get carrier-dropped wholesale; small bursts slip through.
# So launch_campaign_chunked splits the callable list into small sub-batches and
# paces them: fire CHUNK_SIZE calls, wait CHUNK_DELAY_SEC, fire the next, etc.
# Both are easily tunable here / in .env — raise the delay if blocks persist,
# shrink the size if a carrier is strict. 88 leads at size 5 / 180s ~= 51 min.
CHUNK_SIZE      = int(os.getenv("VB_CHUNK_SIZE", "5"))      # calls per sub-batch
CHUNK_DELAY_SEC = int(os.getenv("VB_CHUNK_DELAY_SEC", "180"))  # pause between sub-batches

# --- Cost estimate (display only — the "Start Calling" confirmation modal) ---
# Rough ₹ per completed call, used ONLY to show an estimate before you confirm.
# It never gates anything and is not billed; tune via .env if your Bolna rate
# differs. Real spend always comes from the Catcher's per-call cost webhook.
EST_COST_PER_CALL = float(os.getenv("VB_EST_COST_PER_CALL", "3.5"))
