# ==========================================================================
# DHYAAN VOICE BRAIN — MASTER CLAUDE CODE PROMPT
# Paste this whole block as your first message to Claude Code in the project root.
# (Or save it as CLAUDE.md in the repo so it auto-loads every session.)
# ==========================================================================

You are my pair-programmer on a NEW, STANDALONE project called the
**Dhyaan Voice Brain** — an AI outbound *verification* calling system for
Dhyaan Enterprises (premium real estate, Navi Mumbai / Vashi). It runs on the
same Windows machine as my existing Command Center portal but is a SEPARATE
repo. You must NEVER edit, migrate, or write into the Command Center's database
schema `private` in `realestate_db`. This project uses its own isolated schema
`voicebrain`. The portal stays frozen-safe at all times.

## WHO I AM (address me as CAPTAIN or MY G — English only)
Harshad, solo developer + marketing/ops at Dhyaan. High-energy, direct. I work
one focused goal per session. I want evidence over "it works": show diffs, run
syntax checks, and verify before claiming done. Back up any file before editing
it (`*.bak_[tag]`). Prefer PowerShell one-liners on Windows. Secrets live in
`.env` only — never hardcode, never commit them.

## WHAT THIS SYSTEM IS (and is NOT)
- IS: outbound calls to leads who ALREADY consented via Meta ads / website forms,
  to verify their requirement (location, budget, possession/type) so a human
  advisor can follow up well.
- IS NOT: cold-blasting random numbers. Compliance footing is registered under
  Inovant Solutions for Websites Pvt Ltd. Honour DNC instantly. Calling hours only.
- The voice layer is **Bolna** (already live). Agent "Dhyan", male, ElevenLabs
  Viraj voice. We do NOT build telephony — Bolna places calls. We build the
  brain, the pipeline, and the data capture around it.

## THE FUNNEL (the heart — get this exactly right; full map in SYSTEM_PLAN.md)
WhatsApp is FIRST, not last. The AI's whole job is to RESCUE the silent leads.
```
Stage 0 whatsapp   → WhatsApp BLAST to ALL leads (via AiSensy)
                       ├─ REPLIED?  → straight to HUMAN caller (hot, skip the AI)
                       └─ NO REPLY? → AI caller (Dhyan), Stage 1
Stage 1 ai_call    → Dhyan calls ONLY the non-responders
                       ├─ INTERESTED     → HUMAN caller, Stage 2
                       └─ not interested → archive / DNC
Stage 2 human_call → humans work the warm list (repliers + AI-rescued-interested)
Stage 3 closed     → archived with an outcome reason
```
KEY RULE: WhatsApp repliers go STRAIGHT to a human and are NEVER AI-called. Dhyan
only calls the SILENT non-responders; if he warms one up, THEN a human takes over.
Fresh intake enters at `funnel_stage='whatsapp'`.

## CONFIRMED BOLNA API FACTS (don't re-research unless something breaks)
- Trigger one call:  POST https://api.bolna.ai/call
  body: { "agent_id": "<uuid>", "recipient_phone_number": "+91…" }
  header: Authorization: Bearer <BOLNA_API_KEY>
- Batch calling:     POST https://api.bolna.ai/batches  (multipart)
  fields: agent_id, file=@batch.csv, from_phone_numbers="+91…"
  CSV rule: phone column header MUST be `contact_number`, numbers E.164,
  extra columns (first_name etc.) pass to the agent as context.
- Schedule a batch:  Schedule Batch API using the returned batch_id.
- Results:           Bolna POSTs a webhook per call with status, transcript,
  extracted_data, and cost. Same shape as the Get Execution API response.
- Fallback:          List Batch Executions API (poll) if a webhook is missed.
- Native Auto-Retry exists in Bolna — prefer it over rebuilding retry logic.
- The webhook needs a PUBLIC https URL. In dev use `cloudflared tunnel --url
  http://localhost:5005` or `ngrok http 5005`. In prod, a small always-on host.

## ARCHITECTURE — 6 modules
INTAKE → BRAIN → DIALER → [Bolna calls] → CATCHER → LEDGER → DASHBOARD

| Module     | Job                                                          | Status |
|------------|--------------------------------------------------------------|--------|
| brain/     | The ONE MIND: prompt spec + extraction schema (agent_brain.md)| DONE  |
| ledger/    | Postgres schema `voicebrain` (campaigns, call_leads, results)| DONE  |
| catcher/   | FastAPI webhook → parse Bolna payload → write Ledger          | DONE  |
| intake/    | CSV clean → dedupe → E.164 validate → consent filter → batch  | TODO  |
| dialer/    | Wrapper over Bolna /call + /batches, calling-window guard     | TODO  |
| dashboard/ | View campaigns/outcomes/verified leads + manual promote button| TODO  |

Stack: Python 3, FastAPI, psycopg2, pandas, phonenumbers, requests.
DB: reuse the local Postgres server, but ISOLATED schema `voicebrain`.
Catcher runs on port 5005 (portal owns 3000 — never collide).

## NON-NEGOTIABLE CONVENTIONS
1. Isolation: never write to `realestate_db.private`. Promotion of a verified
   lead into the Command Center is a SEPARATE, MANUAL, reviewed step later.
2. Soft-delete via `deleted_at`. No hard deletes.
3. Secrets only via `config.py` reading `os.getenv`, values in `.env` (gitignored).
4. The Catcher must NEVER return a 5xx to Bolna — always persist the raw payload
   to logs/raw_webhooks.jsonl FIRST, then parse. Raw is the source of truth.
5. Defensive parsing: Bolna key names vary by version. Read with fallbacks and
   keep raw_payload JSONB. Do not assume a key exists.
6. Every DB mutation wrapped in a transaction; log what changed.
7. Before editing any existing file, create `<file>.bak_<short-tag>` and show me
   the diff before applying.

## CURRENT STATE (already scaffolded — read these before doing anything)
- brain/agent_brain.md      → the agent spec + extraction schema (sections 1–7)
- ledger/schema.sql         → voicebrain.campaigns / call_leads / call_results
- catcher/server.py         → FastAPI app, /health + /webhook/bolna, parse_bolna()
- config.py, .env.example, requirements.txt, README.md

## ===== YOUR TASKS, IN ORDER =====

### TASK 0 — Orient (do first, no code changes)
Read README.md, brain/agent_brain.md, ledger/schema.sql, catcher/server.py and
config.py. Summarise back to me in 6 bullets what the system does and confirm the
schema/field names match between agent_brain.md §6, ledger/schema.sql, and
catcher/server.py's parse_bolna(). Flag ANY mismatch. STOP and wait for my go.

### TASK 1 — Intake module (build after I approve Task 0)
Create intake/clean_leads.py that:
- reads a raw CSV from data/inbound/ (columns may be messy: name, phone, source).
- normalises phone to E.164 +91 using the `phonenumbers` lib; drop invalid.
- dedupes on phone (within file AND against voicebrain.call_leads already stored).
- requires a consent_source value per row; rows without it go to a rejects file,
  not the batch (compliance).
- writes TWO outputs to data/processed/:
   (a) a Bolna-ready batch CSV with header `contact_number,first_name` (E.164),
   (b) inserts the leads into voicebrain.call_leads under a new campaign row.
- prints a summary: total in, valid, duplicates dropped, no-consent rejected, final.
- CLI: `python -m intake.clean_leads --file data/inbound/x.csv --campaign "Meta Jun26"`
Add a tiny sample messy CSV in data/inbound/sample_leads.csv to test with.
Show me the diff + a dry run on the sample before touching the DB.

### TASK 2 — Dialer module
Create dialer/dialer.py that:
- has trigger_one(phone) → POST /call for quick single tests.
- has launch_batch(campaign_id) → uploads the processed batch CSV to /batches,
  stores the returned bolna_batch_id on the campaign row, maps execution ids back
  to call_leads where possible.
- enforces the calling window (config CALL_WINDOW_START/END) — refuse to dial
  outside 10:00–19:00 and say why.
- never exceeds MAX_ATTEMPTS per lead; increments call_leads.attempts.
- dry-run flag that prints what it WOULD do without calling Bolna.
Show diffs; run the dry-run path first. Do NOT place a real call without me
explicitly saying "go live".

### TASK 3 — Dashboard (read-only first)
Minimal FastAPI + a single HTML page (navy #0A1628 / gold #D4AF37, Playfair +
Inter to match brand) showing: campaigns, call counts by outcome, and a table of
verified leads from voicebrain.call_results. Add a per-lead "promote" button that
ONLY marks promoted_to_portal=TRUE for now (no cross-DB write yet — we design that
together later). Read-only against the DB otherwise.

### TASK 4 — Tests + run docs
Add tests/ for parse_bolna() using a saved sample webhook payload, and a phone
normaliser test. Update README with exact Windows run commands for each module.

## HOW TO WORK WITH ME
- One task at a time. Finish, show evidence, wait for my "next".
- Always: back up → diff → syntax check (`python -m py_compile`) → summary.
- If a Bolna call/payload doesn't match our assumptions, STOP and show me the raw
  before adapting code.
- Keep it English, high-energy, concise. Call me CAPTAIN.

Start with TASK 0 now.
