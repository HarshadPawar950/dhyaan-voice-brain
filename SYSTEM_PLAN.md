# ==========================================================================
# DHYAAN VOICE BRAIN — MASTER SYSTEM PLAN (read this to understand EVERYTHING)
# Save in repo root. Paste to Claude Code as a full-context brief, or keep
# alongside CLAUDE.md so any session knows the complete vision.
# ==========================================================================

CAPTAIN here. This document is the WHOLE PLAN for the system we're building.
Read it fully before reasoning about any single module — it explains how the
pieces connect and WHY. Nothing here is a build order; it's the map.

## ── WHAT THIS SYSTEM IS ──────────────────────────────────────────────
A self-running LEAD FILTER + AI CALLING machine for Dhyaan Enterprises
(premium real estate, Navi Mumbai / Vashi). It takes a raw pile of leads,
filters them PURE, and routes each one to the right channel automatically —
so nobody wastes time on dead numbers, and humans only ever talk to warm leads.

Standalone project. Runs on the same machine as the Command Center portal but
NEVER touches `realestate_db.private`. Own isolated Postgres schema `voicebrain`.
The portal stays frozen-safe at all times.

This is NOT cold-blasting. It's verification + qualification of leads who came
through Meta ads / website forms / WhatsApp enquiry. Registered under Inovant
Solutions for Websites Pvt Ltd. DNC honoured permanently. Calling hours only.

## ── THE FUNNEL LOGIC (the heart — get this exactly right) ────────────
Stage flow, in Harshad's exact words:

    WhatsApp BLAST goes to ALL leads (via AiSensy)
              │
        ┌─────┴─────┐
        │           │
     REPLIED?     NO REPLY?
        │           │
        ▼           ▼
   HUMAN CALLER   AI CALLER (Dhyan) ── this is the AI's whole job:
   (hot — skip          │              RESCUE the non-responders
    the AI)        ┌────┴────┐
                   │         │
              INTERESTED  not interested
                   │         │
                   ▼         ▼
              HUMAN CALLER  archive / DNC

KEY RULE: people who REPLY on WhatsApp are hot → straight to human, they do NOT
get AI-called. The AI (Dhyan) only calls the SILENT ones — the non-responders.
If Dhyan gets a non-responder interested, THEN they go to a human.

## ── THE PURE FILTER (6 gates, in order) ─────────────────────────────
Raw messy CSV in → clean sorted buckets out. Every lead graded + routed.

  GATE 1 · FORMAT      bad/empty/short numbers      → JUNK bucket
  GATE 2 · NORMALIZE   917… → +917…  (E.164)         [DONE in clean_leads.py]
  GATE 3 · DEDUPE      repeat in-file / already in DB → DUPLICATE bucket
  GATE 4 · DNC         ever opted out → permanent block→ DNC bucket   [TO BUILD]
  GATE 5 · CONSENT     proof opted in / --consent-source stamp [DONE]
  GATE 6 · REPLY-SPLIT WhatsApp replied? Y→human  N→AI  [TO BUILD]

Output buckets (separate clean CSVs, each ready for its channel):
  - ai_caller_leads.csv     (no reply → Dhyan's call list)
  - human_caller_leads.csv  (replied → hot, skip AI)
  - rejects.csv             (junk + dupes + no-consent, WITH reason per row)
  - dnc_master              (permanent do-not-contact)

## ── THE AUTOMATION (95% hands-off, 2 human clicks) ──────────────────
Two webhook receivers run always-listening (same pattern, the Catcher is one):

  1. AISENSY WEBHOOK  → fires when a lead REPLIES to WhatsApp
                        → marks whatsapp_replied=Y, stage=HUMAN, into human queue
                        (AiSensy supports real-time webhooks + auto-tagging;
                         non-responders land on AiSensy "History Page" = inactive)

  2. BOLNA WEBHOOK    → fires when a call ENDS  [THIS IS THE CATCHER, BUILT ✅]
                        → parses extracted_data, saves result, routes interested→human

Full auto chain:
  AiSensy blast → (reply?) → repliers auto-routed to human
                → non-responders auto-pulled → FILTER auto-cleans
                → ready batch → 🖐️ CLICK 1 "release calls" (compliance gate)
                → DIALER auto-uploads to Bolna → calls within calling hours
                → CATCHER auto-saves → interested auto-routed to human
                → DASHBOARD updates live
                → verified lead → 🖐️ CLICK 2 "promote to Command Center" (portal gate)

THE 2 HUMAN GATES (deliberate, do NOT auto-remove):
  CLICK 1 — release the call batch. Auto-dialing the instant leads land is how
            the AiSensy quality rating dropped before. Prepare auto, release by hand.
  CLICK 2 — promote verified lead into the live portal. The one cross into the
            frozen ERP. Auto-fill the queue, human approves the push.

## ── THE CALLING ENGINE: BOLNA (do not switch) ───────────────────────
Bolna is locked. Agent "Dhyan", male, ElevenLabs Viraj voice (Stability 0.6,
Speed 0.95-1.0, Similarity 0.75, Temp 0.2, azure/gpt-4o-mini, Deepgram nova-3).
Bolna bundles telephony + the ElevenLabs voice together — that's why it's chosen.
We do NOT build telephony. We build the brain, filter, pipeline, capture, dashboard.
Architecture is engine-agnostic: only the DIALER module talks to Bolna, so the
engine could be swapped later by changing ONE module. Don't switch now.

Bolna API facts (confirmed):
  - one call:  POST https://api.bolna.ai/call  {agent_id, recipient_phone_number}
  - batch:     POST https://api.bolna.ai/batches (CSV, phone col = contact_number, E.164)
  - results:   webhook per call → status, transcript, extracted_data, cost
  - fallback:  List Batch Executions API
  - native Auto-Retry exists — prefer it. Webhook needs a public URL (cloudflared/ngrok).

## ── THE DATABASE (voicebrain schema) — tracks the whole funnel ──────
  campaigns      one row per blast/batch (name, source, bolna_batch_id, totals)
  call_leads     one row per lead — THE FUNNEL TRACKER
                   phone(E.164), stage(whatsapp|ai_calling|human|done),
                   whatsapp_replied(Y/N) ← drives the split,
                   consent_source, dnc(Y/N), call_status, bolna_execution_id
  call_results   one row per completed call — THE GOLD
                   branch, location, budget, configuration, possession,
                   property_type, carpet_area, call_outcome, interested,
                   call_quality, raw_transcript, raw_payload(JSONB),
                   promoted_to_portal(Y/N)
  dnc_master     permanent do-not-contact (phone, reason, added_at)

## ── MODULE / BUILD STATUS ───────────────────────────────────────────
  brain/agent_brain.md      Dhyan prompt + extraction schema        ✅ DONE
  intake/clean_leads.py     gates 1-3,5 + --consent-source flag     ✅ DONE (40 proven)
  intake/dnc_filter.py      gate 4 — permanent block                ⬜ TO BUILD
  intake/reply_split.py     gate 6 — WhatsApp replied → route        ⬜ TO BUILD
  intake/live_intake.py     write filtered leads to DB (staged)     ⬜ NEXT
  ledger/schema.sql         voicebrain campaigns/leads/results       ✅ DONE
  ledger/002_funnel_cols.sql stage/whatsapp_replied/dnc columns      ⬜ TO BUILD
  catcher/server.py         Bolna webhook → parse → ledger           ✅ DONE (hardened)
  aisensy_catcher/          AiSensy webhook → reply routing          ⬜ TO BUILD
  dialer/dialer.py          upload to Bolna, calling-window guard    ⬜ TO BUILD
  dashboard/app.py          track ALL process on one screen          ⬜ TO BUILD

## ── THE DASHBOARD (the "track everything" screen) ───────────────────
One screen, live, brand-styled (navy #0A1628 / gold #D4AF37, Playfair + Inter):
  - funnel view: WhatsApp → AI → Human, each lead's stage visible
  - live + recent call activity, status, duration
  - extracted data per lead (location/budget/possession/verified)
  - campaign scoreboard: leads / called / verified / callbacks / not-interested
  - verified-lead list + "promote to Command Center" button (CLICK 2)

## ── NON-NEGOTIABLE CONVENTIONS ──────────────────────────────────────
  - Never write to realestate_db.private. Promotion = manual, click-gated.
  - Soft-delete via deleted_at. Secrets via os.getenv → .env (gitignored).
  - Catcher/AiSensy webhooks: persist raw payload FIRST, then parse; never 5xx back.
  - Parse defensively (vendor key names vary), keep raw_payload JSONB.
  - clean_leads.py is DO-NOT-MODIFY (proven). New filters import it, never fork it.
  - Backup before edits (*.bak_tag), diff-by-diff, py_compile, evidence over "it works".
  - One focused task per session. English only. Call me CAPTAIN.

## ── SUGGESTED BUILD ORDER (when CAPTAIN says go) ────────────────────
  1. live_intake.py + ledger funnel columns  → register the 40 (staged: 5 then full)
  2. dnc_filter.py (gate 4)                   → permanent block list
  3. reply_split.py (gate 6) + aisensy_catcher→ the WhatsApp split + auto-feed
  4. dialer.py                                → make Bolna actually call
  5. dashboard/app.py                         → see the whole machine
  6. scheduler                                → run it daily, hands-off

Do not build from this doc directly — it's the MAP. Wait for CAPTAIN to name
the task, then build that one piece with full discipline.
