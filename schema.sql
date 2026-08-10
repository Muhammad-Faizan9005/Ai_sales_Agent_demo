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
  crm_task_id         TEXT,           -- AutoCRM tasks.id for the prep task, so a reschedule can PATCH its due_at
  sheets_synced       BOOLEAN NOT NULL DEFAULT false,
  notification_sent   BOOLEAN NOT NULL DEFAULT false,

  -- Cal.com booking state.
  -- cal_booking_uid is the join key for inbound Cal.com webhooks. It cannot be
  -- session_id: Cal.com omits booking metadata from the BOOKING_CANCELLED
  -- payload (calcom/cal.com#27783), so a cancel event carries the uid but not
  -- our session. Rescheduling ALSO mints a new uid and cancels the old one, so
  -- this column is rewritten on every reschedule and must stay unique-indexed.
  cal_booking_uid     TEXT,
  cal_booking_status  TEXT,           -- accepted | pending | cancelled | rejected
  cal_meeting_url     TEXT,
  meeting_start_at    TIMESTAMPTZ,    -- authoritative start; survives reschedules

  -- Ops
  duration_ms         INTEGER,
  error               TEXT,           -- written by sales-agent-error-handler
  language            TEXT DEFAULT 'en',

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The CREATE TABLE above is IF NOT EXISTS, so on a database where agent_runs
-- already exists it is a no-op and new columns in its body would never appear.
-- These ALTERs are what actually migrate an existing table, and they are how
-- every future column must be added.
ALTER TABLE agent_runs
  ADD COLUMN IF NOT EXISTS crm_task_id        TEXT,
  ADD COLUMN IF NOT EXISTS cal_booking_uid    TEXT,
  ADD COLUMN IF NOT EXISTS cal_booking_status TEXT,
  ADD COLUMN IF NOT EXISTS cal_meeting_url    TEXT,
  ADD COLUMN IF NOT EXISTS meeting_start_at   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at   ON agent_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_lead_status  ON agent_runs (lead_status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_meeting      ON agent_runs (meeting_booked) WHERE meeting_booked = true;
-- session_id needs no index: UNIQUE already builds one.

-- UNIQUE, and partial so the many rows with a NULL uid do not collide. This is
-- the lookup key for every inbound Cal.com webhook, and uniqueness is what
-- guarantees a redelivered BOOKING_CANCELLED cannot match two runs.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_cal_uid
  ON agent_runs (cal_booking_uid) WHERE cal_booking_uid IS NOT NULL;

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

-- Drop the original 8-argument signature before redefining. Postgres keys
-- functions by name AND argument types, so CREATE OR REPLACE with the wider
-- signature below would ADD an overload instead of replacing it. Two candidates
-- whose trailing params all have defaults are ambiguous to PostgREST, which
-- then refuses the call with PGRST203 "could not choose the best candidate
-- function". Safe on a fresh database thanks to IF EXISTS.
DROP FUNCTION IF EXISTS mark_run_outcome(
  TEXT, TEXT, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, TEXT
);

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
  p_error              TEXT    DEFAULT NULL,
  -- Conversation payload. Without these the row exists but is empty, and the
  -- nightly FAQ job -- which filters on `transcript IS NOT NULL` -- matches
  -- nothing, so faq_summary stays permanently empty.
  p_visitor_name       TEXT    DEFAULT NULL,
  p_visitor_email      TEXT    DEFAULT NULL,
  p_visitor_phone      TEXT    DEFAULT NULL,
  p_company_name       TEXT    DEFAULT NULL,
  p_service_recommended TEXT   DEFAULT NULL,
  p_lead_status        TEXT    DEFAULT NULL,
  p_transcript         JSONB   DEFAULT NULL,
  p_pages_visited      JSONB   DEFAULT NULL,
  -- Cal.com booking state + the prep task id, so a later reschedule can move
  -- the task deadline instead of creating a second task.
  p_crm_task_id        TEXT    DEFAULT NULL,
  p_cal_booking_uid    TEXT    DEFAULT NULL,
  p_cal_booking_status TEXT    DEFAULT NULL,
  p_cal_meeting_url    TEXT    DEFAULT NULL,
  p_meeting_start_at   TIMESTAMPTZ DEFAULT NULL
) RETURNS INTEGER
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  v_rows INTEGER;
BEGIN
  -- INSERT .. ON CONFLICT, not a bare UPDATE. n8n is currently the only writer
  -- of agent_runs: the chat app that was meant to create the row on turn 1 does
  -- not exist yet. A plain UPDATE matched zero rows and returned 0 while still
  -- reporting HTTP success, so every outcome was silently dropped and the table
  -- stayed empty -- which also starved the nightly FAQ job of transcripts.
  -- Upserting means the row is created by whichever writer arrives first.
  INSERT INTO agent_runs (
    session_id, crm_lead_id, crm_synced, sheets_synced,
    notification_sent, meeting_booked, proposal_requested, error,
    visitor_name, visitor_email, visitor_phone, company_name,
    service_recommended, lead_status, transcript, pages_visited,
    crm_task_id, cal_booking_uid, cal_booking_status, cal_meeting_url,
    meeting_start_at
  )
  VALUES (
    p_session_id, p_crm_lead_id,
    -- The five booleans are NOT NULL DEFAULT false, and a column DEFAULT only
    -- applies when the column is OMITTED from the INSERT. Listing it and passing
    -- an explicit NULL defeats the default and raises 23502 ("null value in
    -- column meeting_booked violates not-null constraint"), which is exactly
    -- what killed every lead_created write-back. COALESCE restores the default.
    COALESCE(p_crm_synced, false),
    COALESCE(p_sheets_synced, false),
    COALESCE(p_notification_sent, false),
    COALESCE(p_meeting_booked, false),
    COALESCE(p_proposal_requested, false),
    p_error,
    p_visitor_name, p_visitor_email, p_visitor_phone, p_company_name,
    p_service_recommended, p_lead_status, p_transcript, p_pages_visited,
    p_crm_task_id, p_cal_booking_uid, p_cal_booking_status, p_cal_meeting_url,
    p_meeting_start_at
  )
  -- Deliberately COALESCE against the PARAMETERS here, not EXCLUDED. Because the
  -- VALUES clause above turns an unsupplied boolean into false, EXCLUDED can no
  -- longer distinguish "caller said false" from "caller said nothing" -- and
  -- COALESCE(EXCLUDED.meeting_booked, ...) would then stamp false over a true
  -- that the meeting branch had already recorded. The parameter is still NULL
  -- when omitted, so it preserves "omitted means leave this column alone".
  ON CONFLICT (session_id) DO UPDATE
     SET crm_lead_id        = COALESCE(p_crm_lead_id,        agent_runs.crm_lead_id),
         crm_synced         = COALESCE(p_crm_synced,         agent_runs.crm_synced),
         sheets_synced      = COALESCE(p_sheets_synced,      agent_runs.sheets_synced),
         notification_sent  = COALESCE(p_notification_sent,  agent_runs.notification_sent),
         meeting_booked     = COALESCE(p_meeting_booked,     agent_runs.meeting_booked),
         proposal_requested = COALESCE(p_proposal_requested, agent_runs.proposal_requested),
         error              = COALESCE(p_error,              agent_runs.error),
         visitor_name       = COALESCE(p_visitor_name,       agent_runs.visitor_name),
         visitor_email      = COALESCE(p_visitor_email,      agent_runs.visitor_email),
         visitor_phone      = COALESCE(p_visitor_phone,      agent_runs.visitor_phone),
         company_name       = COALESCE(p_company_name,       agent_runs.company_name),
         service_recommended= COALESCE(p_service_recommended,agent_runs.service_recommended),
         lead_status        = COALESCE(p_lead_status,        agent_runs.lead_status),
         transcript         = COALESCE(p_transcript,         agent_runs.transcript),
         pages_visited      = COALESCE(p_pages_visited,      agent_runs.pages_visited),
         crm_task_id        = COALESCE(p_crm_task_id,        agent_runs.crm_task_id),
         cal_booking_uid    = COALESCE(p_cal_booking_uid,    agent_runs.cal_booking_uid),
         cal_booking_status = COALESCE(p_cal_booking_status, agent_runs.cal_booking_status),
         cal_meeting_url    = COALESCE(p_cal_meeting_url,    agent_runs.cal_meeting_url),
         meeting_start_at   = COALESCE(p_meeting_start_at,   agent_runs.meeting_start_at);

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows;   -- 1 = inserted or updated; 0 should no longer happen
END;
$$;

-- Inbound Cal.com webhooks join on cal_booking_uid, not session_id: Cal.com
-- omits booking metadata from the BOOKING_CANCELLED payload
-- (calcom/cal.com#27783), so a cancel event knows the uid but not our session.
--
-- Rescheduling mints a NEW uid and cancels the old one, so p_new_cal_booking_uid
-- lets one call move the row onto the new uid while matching on the old.
--
-- Returns the row so the caller can reach crm_lead_id / crm_task_id / session_id
-- without a second round trip. Zero rows means the booking was made outside the
-- agent (someone used the public Cal.com page) -- normal, and the caller should
-- treat it as a no-op rather than an error.
CREATE OR REPLACE FUNCTION apply_cal_booking_event(
  p_cal_booking_uid     TEXT        DEFAULT NULL,
  -- Second join path. BOOKING_CREATED and BOOKING_RESCHEDULED do carry our
  -- metadata, so session_id is available and is the more reliable match on the
  -- first event for a booking -- before any uid has been stored.
  p_session_id          TEXT        DEFAULT NULL,
  p_cal_booking_status  TEXT        DEFAULT NULL,
  p_new_cal_booking_uid TEXT        DEFAULT NULL,
  p_meeting_start_at    TIMESTAMPTZ DEFAULT NULL,
  p_cal_meeting_url     TEXT        DEFAULT NULL,
  p_meeting_booked      BOOLEAN     DEFAULT NULL
) RETURNS TABLE (
  session_id  TEXT,
  crm_lead_id TEXT,
  crm_task_id TEXT,
  visitor_name  TEXT,
  visitor_email TEXT
)
LANGUAGE sql
SET search_path = public
AS $$
  UPDATE agent_runs SET
    cal_booking_uid    = COALESCE(p_new_cal_booking_uid, p_cal_booking_uid, agent_runs.cal_booking_uid),
    cal_booking_status = COALESCE(p_cal_booking_status,  agent_runs.cal_booking_status),
    meeting_start_at   = COALESCE(p_meeting_start_at,    agent_runs.meeting_start_at),
    cal_meeting_url    = COALESCE(p_cal_meeting_url,     agent_runs.cal_meeting_url),
    meeting_booked     = COALESCE(p_meeting_booked,      agent_runs.meeting_booked)
  -- Match on EITHER key. The uid is checked first because it is the only thing
  -- a BOOKING_CANCELLED payload carries; session_id covers the first event for
  -- a booking, before any uid has been stored on the row.
  WHERE (p_cal_booking_uid IS NOT NULL AND agent_runs.cal_booking_uid = p_cal_booking_uid)
     OR (p_session_id      IS NOT NULL AND agent_runs.session_id      = p_session_id)
  RETURNING agent_runs.session_id, agent_runs.crm_lead_id, agent_runs.crm_task_id,
            agent_runs.visitor_name, agent_runs.visitor_email;
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
-- Adding parameters to mark_run_outcome does NOT replace the old function:
-- Postgres keys functions by argument list, so CREATE OR REPLACE leaves the
-- previous 16-arg version in place as an overload. PostgREST then sees two
-- candidates for /rpc/mark_run_outcome and answers 300 ("Could not choose the
-- best candidate function"), which breaks every write-back at once. Drop the
-- superseded signature explicitly.
DROP FUNCTION IF EXISTS mark_run_outcome(TEXT, TEXT, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB);

REVOKE ALL ON FUNCTION mark_run_outcome(TEXT, TEXT, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION record_run_error(TEXT, TEXT)                                                    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION bump_faq(TEXT, INTEGER)                                                         FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION apply_cal_booking_event(TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ, TEXT, BOOLEAN)     FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION mark_run_outcome(TEXT, TEXT, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, BOOLEAN, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION record_run_error(TEXT, TEXT)                                                    TO service_role;
GRANT EXECUTE ON FUNCTION bump_faq(TEXT, INTEGER)                                                         TO service_role;
GRANT EXECUTE ON FUNCTION apply_cal_booking_event(TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ, TEXT, BOOLEAN)     TO service_role;

-- PostgREST caches the schema; without this the new functions 404 until the
-- next restart.
NOTIFY pgrst, 'reload schema';
