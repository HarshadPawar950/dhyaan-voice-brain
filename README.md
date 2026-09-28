# Dhyaan Voice Brain

An AI outbound-calling pipeline that verifies real estate leads at scale. Built during my internship at Dhyaan Enterprises, Vashi (Navi Mumbai), as a standalone system that sits next to the Dhyaan Command Center ERP/CRM.

## What it does
Sales staff upload a lead file (up to ~500 numbers). The system cleans it, filters out opted-out numbers, dials the leads in paced batches through the Bolna voice-AI platform, captures each call result by webhook, scores how interested each lead is, and hands qualified leads to the sales team.

## The funnel
```
WhatsApp message to all leads (AiSensy)
  -> Lead replies      -> goes straight to a human caller
  -> Lead stays silent -> AI voice agent calls to qualify
        -> Interested     -> verified lead, handed to a human
        -> Not interested -> archived / added to Do-Not-Call
```
The AI only calls the leads a human would otherwise never have reached, which protects sales-team time.

## Pipeline
| Module | Role |
|---|---|
| `intake/` | Cleans uploaded leads: dedupe, E.164 phone validation, Do-Not-Call filter |
| `brain/` | The voice agent's conversation design and system prompts |
| `dialer/` | Places calls through the Bolna API in paced chunks, with retry handling |
| `aisensy/` | WhatsApp outreach through the AiSensy API |
| `catcher/` | FastAPI webhook receiver for call results (transcript, extracted data, cost) |
| `dashboard/` | Live run dashboard: connected vs not connected, spend, verified leads, retry queue, CSV export |
| `tools/` | Maintenance scripts: webhook replay, re-scoring, audit checks |

## Lead scoring (3 layers)
1. **Call quality:** was the call usable (good / partial / junk)?
2. **Heat score:** a rules-based function reads the extracted budget, location and possession timeline and classifies intent as hot, warm or cold (`intake/heat_score.py`).
3. **Manual rating:** sales staff can override with a 1-5 star rating from the dashboard.

A lead counts as **verified** only when interest is explicitly stated *and* at least one concrete requirement is captured. An explicit "no" always wins (`intake/verify.py`). The scoring logic is written as pure functions, so it can be tested without a database or live calls.

## Design decisions
- **Isolated by design:** it reads and writes only its own `voicebrain` schema and never touches the production CRM database. Moving leads into the CRM is a manual, reviewed step.
- **Consent and compliance first:** it calls only leads who already enquired, keeps a Do-Not-Call list, and dials within set calling hours.
- **Paced dialing:** calls go out in chunks with delays so carriers do not flag the traffic as spam. Runs can pause and resume.

## Tech stack
Python, FastAPI, Uvicorn, PostgreSQL (psycopg2), pandas, phonenumbers, Bolna voice-AI API, AiSensy WhatsApp API, cloudflared/ngrok tunnels for webhooks.

## Setup
```
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then fill in your own keys
```
The repo contains no credentials, call logs or lead data. See `.env.example` for the variables needed.

## How it was built
I designed the architecture, the funnel, the scoring rules and the compliance approach, and directed Claude Code as the implementation partner across the build sessions (see `CLAUDE.md`). I also tested and iterated on the result.
