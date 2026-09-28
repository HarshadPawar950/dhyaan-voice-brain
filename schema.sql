-- DHYAAN VOICE BRAIN — LEDGER SCHEMA
-- Its OWN schema. Does NOT touch realestate_db's `private` schema.
-- Portal stays frozen-safe. We control promotion into Command Center manually.
--
-- Run once:  psql -U postgres -d realestate_db -f ledger/schema.sql
-- (We reuse the same DB server but an isolated schema: `voicebrain`)

CREATE SCHEMA IF NOT EXISTS voicebrain;

-- ============================================================
-- CAMPAIGNS — one batch of calls = one campaign
-- ============================================================
CREATE TABLE IF NOT EXISTS voicebrain.campaigns (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    source        TEXT,                       -- e.g. 'meta_form_jun26'
    bolna_batch_id TEXT,                       -- returned by Bolna /batches
    total_leads   INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT now(),
    deleted_at    TIMESTAMPTZ                  -- soft-delete (portal convention)
);

-- ============================================================
-- CALL_LEADS — one row per number we intend to / did call
-- ============================================================
CREATE TABLE IF NOT EXISTS voicebrain.call_leads (
    id             BIGSERIAL PRIMARY KEY,
    campaign_id    BIGINT REFERENCES voicebrain.campaigns(id),
    phone          TEXT NOT NULL,             -- E.164, +91…
    first_name     TEXT,
    consent_source TEXT,                       -- proof this lead opted in
    bolna_execution_id TEXT,                   -- maps webhook back to this row
    call_status    TEXT DEFAULT 'pending',    -- pending|queued|in-progress|completed|failed
    attempts       INTEGER DEFAULT 0,
    -- lead-filter funnel cascade: ai_call -> whatsapp -> human_call -> closed
    funnel_stage   TEXT NOT NULL DEFAULT 'ai_call'
                   CHECK (funnel_stage IN ('ai_call','whatsapp','human_call','closed')),
    outcome        TEXT                       -- NULL until a stage resolves it
                   CHECK (outcome IS NULL OR outcome IN
                          ('interested','no_interest','no_response','qualified','dead')),
    created_at     TIMESTAMPTZ DEFAULT now(),
    deleted_at     TIMESTAMPTZ,
    UNIQUE (campaign_id, phone)                -- dedupe within a campaign
);

-- ============================================================
-- CALL_RESULTS — the GOLD. One row per completed call.
-- ============================================================
CREATE TABLE IF NOT EXISTS voicebrain.call_results (
    id             BIGSERIAL PRIMARY KEY,
    call_lead_id   BIGINT REFERENCES voicebrain.call_leads(id),
    bolna_execution_id TEXT,
    -- extracted slots (mirror brain section 6)
    branch         TEXT,                       -- residential|commercial
    location       TEXT,
    budget         TEXT,
    configuration  TEXT,
    possession     TEXT,
    property_type  TEXT,
    carpet_area    TEXT,
    call_outcome   TEXT,                       -- verified|callback_requested|…
    interested     BOOLEAN,
    -- quality + raw
    call_quality   TEXT,                       -- good|partial|junk (our scoring)
    duration_sec   INTEGER,
    cost           NUMERIC(10,4),
    raw_transcript TEXT,
    raw_payload    JSONB,                      -- full Bolna webhook, for safety
    promoted_to_portal BOOLEAN DEFAULT FALSE,  -- did we push to Command Center?
    received_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_results_outcome ON voicebrain.call_results(call_outcome);
CREATE INDEX IF NOT EXISTS idx_results_exec    ON voicebrain.call_results(bolna_execution_id);
CREATE INDEX IF NOT EXISTS idx_leads_exec      ON voicebrain.call_leads(bolna_execution_id);
CREATE INDEX IF NOT EXISTS idx_leads_funnel    ON voicebrain.call_leads(funnel_stage);
CREATE INDEX IF NOT EXISTS idx_leads_outcome   ON voicebrain.call_leads(outcome);
