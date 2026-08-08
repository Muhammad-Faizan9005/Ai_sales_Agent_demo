-- AI Sales Agent — own tables.
--
-- These do NOT exist in AutoCRM. AutoCRM's schema has 23 tables and none of
-- them is agent_runs or faq_summary; its `ai_agent_runs` is the AI_service
-- microservice's run log and is unrelated. Every Postgres node in the three
-- n8n workflows reads or writes these two tables, so they must exist before
-- the first run or every execution fails at the first Postgres node.
--
-- Run against whichever Postgres the n8n Postgres credential points at.
-- Nothing here touches AutoCRM's own tables — the CRM is reached over HTTP,
-- never by direct SQL.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS agent_runs (
  run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- One row per conversation, upserted as it progresses. UNIQUE is load-bearing:
  -- every workflow looks a run up by session_id and expects exactly one row.
  session_id          TEXT NOT NULL UNIQUE,
  started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at            TIMESTAMPTZ,

  -- Visitor / lead info, filled progressively as the agent qualifies them
  visitor_name        TEXT,
  visitor_email       TEXT,
  visitor_phone       TEXT,
  company_name        TEXT,
  website_url         TEXT,
  industry            TEXT,

  -- Sales outcome
  lead_status         TEXT CHECK (lead_status IN ('hot', 'warm', 'cold')),
  service_recommended TEXT,
  meeting_booked      BOOLEAN NOT NULL DEFAULT false,
  proposal_requested  BOOLEAN NOT NULL DEFAULT false,
  handoff_requested   BOOLEAN NOT NULL DEFAULT false,

  -- Conversation content
  messages_count      INTEGER NOT NULL DEFAULT 0,
  transcript          JSONB,          -- [{role, content, timestamp}]
  actions_taken       JSONB,          -- ["navigate_to:/seo/local-seo/", "open_contact_form", ...]
  pages_visited       JSONB,          -- ["/seo/local-seo/", "/contact/"]

  -- Integration status
  crm_synced          BOOLEAN NOT NULL DEFAULT false,
  crm_lead_id         TEXT,           -- AutoCRM leads.id (UUID), kept as TEXT for cross-CRM portability
  sheets_synced       BOOLEAN NOT NULL DEFAULT false,
  notification_sent   BOOLEAN NOT NULL DEFAULT false,

  -- Ops
  duration_ms         INTEGER,
  error               TEXT,           -- written by sales-agent-error-handler
  language            TEXT DEFAULT 'en',

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at   ON agent_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_lead_status  ON agent_runs (lead_status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_meeting      ON agent_runs (meeting_booked) WHERE meeting_booked = true;
-- session_id needs no index: UNIQUE already builds one.

CREATE TABLE IF NOT EXISTS faq_summary (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- UNIQUE is required, not cosmetic: the nightly clustering job upserts with
  -- ON CONFLICT (question), which errors without a matching unique constraint.
  question     TEXT NOT NULL UNIQUE,
  frequency    INTEGER NOT NULL DEFAULT 1,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
