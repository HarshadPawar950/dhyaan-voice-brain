# DHYAAN VOICE BRAIN

Standalone AI cold-calling **verification** system for Dhyaan Enterprises.
Sits NEXT TO the Command Center — never inside it. The portal stays frozen-safe.

> Compliance footing: outbound to **consented Meta/form leads only**, registered
> under Inovant Solutions for Websites Pvt Ltd. Honour DNC immediately. Calling
> hours only. This is NOT cold-blasting — it is verification of warm leads.

## The machine (6 modules)
```
INTAKE → BRAIN → DIALER → [Bolna places call] → CATCHER → LEDGER → DASHBOARD
```
| Module    | Job                                                        | Status |
|-----------|------------------------------------------------------------|--------|
| brain/    | The one mind: prompt spec + extraction schema              | ✅ v1  |
| ledger/   | Own Postgres schema `voicebrain` (campaigns/leads/results) | ✅ v1  |
| catcher/  | FastAPI webhook receiver → parses Bolna → Ledger           | ✅ v1  |
| intake/   | CSV clean → dedupe → E.164 validate → consent filter       | ⬜ next |
| dialer/   | Wrapper over Bolna /call and /batches + retry windows      | ⬜      |
| dashboard/| See campaigns, outcomes, verified leads, promote button    | ⬜      |

## Bolna API facts (confirmed)
- Trigger one: `POST https://api.bolna.ai/call`  {agent_id, recipient_phone_number}
- Batch: `POST https://api.bolna.ai/batches` (CSV; phone col header = `contact_number`, E.164)
- Webhook delivers: call status, transcript, **extracted_data**, cost
- Fallback: List Batch Executions API (poll if webhook missed)
- Native Auto-Retry exists — lean on it instead of rebuilding.
- Webhook needs a PUBLIC url → use cloudflared/ngrok in test, small host in prod.

## Setup (first run)
```bash
cd dhyaan-voice-brain
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env                                # then fill in real values
psql -U postgres -d realestate_db -f ledger/schema.sql
```

## Test the Catcher with ONE real call (do this first)
```bash
uvicorn catcher.server:app --host 0.0.0.0 --port 5005
cloudflared tunnel --url http://localhost:5005        # gives a public https URL
# paste <public-url>/webhook/bolna into Bolna agent's Webhook URL field
# make one call from Bolna playground → watch logs/raw_webhooks.jsonl fill
```
Once you see a real payload, open `catcher/server.py` → `parse_bolna()` and
confirm the key names match (extracted_data, transcript, duration, cost).
Tighten them, then results flow into `voicebrain.call_results`.

## Conventions (carried from Command Center)
- Backup before edits (`*.bak_[tag]`), evidence over "it works", one goal per session.
- Soft-delete via `deleted_at`. Secrets in `.env` only — never committed.
- Promotion into the Command Center portal is a MANUAL, reviewed step. Never auto-write
  into `realestate_db.private` from here.

## Build order
1. ✅ Brain + Ledger + Catcher (this session)
2. ⬜ Test Catcher with one real call; lock the payload keys
3. ⬜ Intake (CSV → call-ready batch)
4. ⬜ Dialer (trigger batches via Bolna)
5. ⬜ Dashboard (see + promote)
