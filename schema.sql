-- AI Sales Agent — own tables.
--
-- These do NOT exist in AutoCRM. AutoCRM's schema has 23 tables and none of
-- them is agent_runs or faq_summary; its `ai_agent_runs` is the AI_service
-- microservice's run log and is unrelated. Every Postgres node in the three
-- n8n workflows reads or writes these two tables, so they must exist before
-- the first run or every execution fails at the first Postgres node.
--
-- Run against the AI Sales Agent's own Supabase project (see n8n/README.md
-- §2.1 for why it is a separate project, not AutoCRM's). Nothing here touches
-- AutoCRM's own tables — the CRM is reached over HTTP, never by direct SQL.
--
-- Paste the whole file into the Supabase SQL editor and run once. It is
-- idempotent: re-running is safe.

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

-- Deny-all RLS. Not optional.
--
-- Supabase publishes every table in `public` through PostgREST, so without this
-- the project's anon key -- which ships in any browser bundle -- could read every
-- chat transcript, name, email and phone number in agent_runs.
--
-- Enabling RLS with ZERO policies is exactly what we want: the anon and
-- authenticated roles get nothing, while n8n is unaffected because it connects as
-- `postgres`, and a table owner bypasses RLS.
--
-- Do NOT add FORCE ROW LEVEL SECURITY. That applies policies to the owner too,
-- and since there are no policies, it would lock n8n out of its own tables.
--
-- The admin dashboard must therefore read this DB server-side (service role key
-- or a direct connection), never with the anon key from the browser.
ALTER TABLE agent_runs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE faq_summary ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- RPC surface for n8n
--
-- n8n reaches this database over PostgREST using the Supabase credential, not
-- raw SQL. The Supabase node offers Get / Get Many / Create / Update / Delete
-- and nothing else -- no upsert operation, and no way to express
-- `frequency = frequency + 1` atomically. So the writes that need either live
-- here as functions, and n8n calls them with POST /rest/v1/rpc/<name>.
--
-- SECURITY INVOKER (the default) is deliberate. n8n authenticates as
-- service_role, which carries BYPASSRLS, so these already run with full access
-- and SECURITY DEFINER would only widen the blast radius. `SET search_path`
-- pins name resolution so nothing earlier in the caller's path can shadow
-- agent_runs.
-- ---------------------------------------------------------------------------

-- One function for all three "this branch finished, record what happened"
-- writes. Every parameter but the session is optional and COALESCEd, so each
-- caller sends only its own columns and leaves the rest untouched.
--
-- COALESCE also sidesteps a real n8n problem: the Supabase node declares field
-- values as strings, so an expression returning null arrives as '' and would
-- write an empty string into `error`, making every successful run look failed.
-- Omitting the key entirely just leaves the column alone.
--
-- Note `error` is COALESCEd like everything else, so a success never erases an
-- error an earlier attempt recorded. Clearing it is not something any caller
-- needs, and keeping the history is the safer default.
CREATE OR REPLACE FUNCTION mark_run_outcome(
  p_session_id         TEXT,
  p_crm_lead_id        TEXT    DEFAULT NULL,
  p_crm_synced         BOOLEAN DEFAULT NULL,
  p_sheets_synced      BOOLEAN DEFAULT NULL,
  p_notification_sent  BOOLEAN DEFAULT NULL,
  p_meeting_booked     BOOLEAN DEFAULT NULL,
  p_proposal_requested BOOLEAN DEFAULT NULL,
  p_error              TEXT    DEFAULT NULL
) RETURNS INTEGER
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  v_rows INTEGER;
BEGIN
  UPDATE agent_runs
     SET crm_lead_id        = COALESCE(p_crm_lead_id,        crm_lead_id),
         crm_synced         = COALESCE(p_crm_synced,         crm_synced),
         sheets_synced      = COALESCE(p_sheets_synced,      sheets_synced),
         notification_sent  = COALESCE(p_notification_sent,  notification_sent),
         meeting_booked     = COALESCE(p_meeting_booked,     meeting_booked),
         proposal_requested = COALESCE(p_proposal_requested, proposal_requested),
         error              = COALESCE(p_error,              error)
   WHERE session_id = p_session_id;

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows;   -- 0 = no such session_id; the caller can branch on it
END;
$$;

-- Shared by "Record Failure" (events workflow) and "Record Error on Run"
-- (error handler). INSERT .. ON CONFLICT rather than UPDATE, so the error is
-- still captured when no agent_runs row exists yet -- a plain UPDATE would
-- match zero rows and lose the failure silently.
CREATE OR REPLACE FUNCTION record_run_error(
  p_session_id TEXT,
  p_error      TEXT
) RETURNS UUID
LANGUAGE sql
SET search_path = public
AS $$
  INSERT INTO agent_runs (session_id, error)
  VALUES (p_session_id, p_error)
  ON CONFLICT (session_id)
  DO UPDATE SET error = EXCLUDED.error
  RETURNING run_id;
$$;

-- Nightly FAQ clustering. The increment must happen inside the statement:
-- emulating it as Get -> IF -> Update/Create would let two concurrent runs both
-- read the old count and lose one of the increments.
CREATE OR REPLACE FUNCTION bump_faq(
  p_question  TEXT,
  p_frequency INTEGER DEFAULT 1
) RETURNS INTEGER
LANGUAGE sql
SET search_path = public
AS $$
  INSERT INTO faq_summary (question, frequency, last_seen_at)
  VALUES (p_question, GREATEST(p_frequency, 1), now())
  ON CONFLICT (question)
  DO UPDATE SET frequency    = faq_summary.frequency + EXCLUDED.frequency,
                last_seen_at = now()
  RETURNING frequency;
$$;

-- Postgres grants EXECUTE on new functions to PUBLIC by default, which would
-- hand the anon key -- the one that ships in browser bundles -- the ability to
-- overwrite any run's error text or inflate FAQ counts. That would undo the
-- deny-all RLS above, so revoke it and grant only service_role.
REVOKE ALL ON FUNCTION mark_run_outcome(TEXT, TEXT, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION record_run_error(TEXT, TEXT)                                                    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION bump_faq(TEXT, INTEGER)                                                         FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION mark_run_outcome(TEXT, TEXT, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION record_run_error(TEXT, TEXT)                                                    TO service_role;
GRANT EXECUTE ON FUNCTION bump_faq(TEXT, INTEGER)                                                         TO service_role;

-- PostgREST caches the schema; without this the new functions 404 until the
-- next restart.
NOTIFY pgrst, 'reload schema';
