# AI Sales Agent — Technical Build Plan

Reference document for implementation. Covers what runs in n8n, what's built custom, how the pieces talk to each other, and the run-logging schema that powers the admin dashboard.

---

## 1. Architecture Overview

```
Chat Widget (custom, embedded on client site)
        |
        v
n8n Orchestration Workflow (webhook-triggered)
        |
        +--> AI Agent node (Claude/GPT) + RAG vector store
        +--> External services (CRM, Sheets, Calendar, WhatsApp, Email)
        +--> Run Logger node --> Postgres/Supabase (agent_runs table)
                                        |
                                        v
                              Admin Dashboard (custom React app)
```

Principle: **n8n handles orchestration and integrations. Custom code handles anything that touches the browser (widget, page actions) or needs a real UI (dashboard).**

---

## 2. What Runs in n8n

### Core conversation workflow (webhook-triggered)
- **Webhook node** — receives `{session_id, message, visitor_meta}` from the widget
- **Memory node** (Postgres/Redis) — pulls conversation history for that session
- **AI Agent node** (Claude/GPT) — system prompt = sales persona; has access to tools below
- **Vector Store node** (Supabase/Qdrant) — RAG over scraped website content (services, pricing, blog, portfolio)

### Tools the agent can call (n8n sub-workflows)
| Tool | What it does |
|---|---|
| `get_service_info` | Query vector store for service/pricing details |
| `recommend_service` | Rule-based Switch node — industry → service mapping |
| `qualify_lead` | Structured-output LLM call — extracts name, email, phone, company, website, industry |
| `score_lead` | IF/Switch logic → Hot / Warm / Cold |
| `create_crm_lead` | HTTP Request node → client's CRM API |
| `log_to_sheets` | Google Sheets node |
| `book_meeting` | Calendly or Google Calendar node |
| `send_notification` | WhatsApp Cloud API / email — fires on new lead, meeting booked, high-value lead |
| `trigger_navigation` | Returns a structured action (e.g. `{action: "open_page", url}`) — does NOT execute anything itself |

### End-of-run logging (always fires, regardless of outcome)
- **Run Logger node** — writes a structured record to Postgres (see schema in Section 5)

**Why n8n fits this part well**: every item above is "call an API, branch on a condition, transform data" — n8n's core strength.

---

## 3. What's Built Custom (outside n8n)

n8n has no frontend capability at all — these pieces are unavoidable regardless of backend choice.

| Component | Why it can't be n8n |
|---|---|
| **Chat widget** (React → compiled to vanilla JS bundle, embedded via `<script>`) | Needs to render in the client's website, in their DOM |
| **Widget's action executor** — JS that interprets `{action: "open_page", url}` etc. returned by n8n and manipulates the DOM (open page, scroll, prefill form, open modal) | Client-side browser logic — n8n can only return instructions, never execute them in someone else's browser |
| **Admin dashboard** (React app) | n8n is not a BI/analytics tool |
| **Thin API/query layer between dashboard and Postgres** | Can be Supabase's auto-generated REST API (fastest) or a small FastAPI layer if more control over auth/filtering is needed |

---

## 4. Interaction Flow

1. Visitor types a message in the widget.
2. Widget POSTs `{session_id, message}` to the n8n webhook URL.
3. n8n workflow runs: pulls memory → agent reasons → calls tools as needed (RAG lookup, lead qualification, CRM/Sheets sync, booking, notifications).
4. n8n returns a JSON response: `{reply_text, action?: {type, params}}`.
5. Widget renders `reply_text` in the chat UI, and if `action` is present, executes it (e.g. navigates to a page, opens a form, prefills fields).
6. **Regardless of what happened in steps 3–5**, the Run Logger node writes one row to `agent_runs` in Postgres.
7. Admin dashboard queries `agent_runs` directly (via Supabase REST or FastAPI) — never touches n8n's internal execution log for client-facing display. n8n's native execution log stays available for your own debugging only.

---

## 5. Run Logger Table Schema

```sql
CREATE TABLE agent_runs (
  run_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id        TEXT NOT NULL,                 -- ties multiple messages in one conversation together
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at          TIMESTAMPTZ,                     -- null while conversation is still active

  -- Visitor / lead info (filled progressively as the agent qualifies them)
  visitor_name      TEXT,
  visitor_email     TEXT,
  visitor_phone     TEXT,
  company_name      TEXT,
  website_url       TEXT,
  industry          TEXT,

  -- Sales outcome
  lead_status       TEXT CHECK (lead_status IN ('hot', 'warm', 'cold', NULL)),
  service_recommended TEXT,
  meeting_booked    BOOLEAN NOT NULL DEFAULT false,
  proposal_requested BOOLEAN NOT NULL DEFAULT false,

  -- Conversation content
  messages_count    INTEGER NOT NULL DEFAULT 0,
  transcript        JSONB,                           -- full array of {role, content, timestamp}
  actions_taken     JSONB,                            -- e.g. ["opened_pricing_page", "prefilled_contact_form", "booked_meeting"]

  -- Integration status (for debugging / support)
  crm_synced        BOOLEAN NOT NULL DEFAULT false,
  crm_lead_id       TEXT,                              -- ID returned by client's CRM, for cross-referencing
  sheets_synced     BOOLEAN NOT NULL DEFAULT false,
  notification_sent BOOLEAN NOT NULL DEFAULT false,

  -- Ops / reliability
  duration_ms       INTEGER,
  error             TEXT,                               -- non-null if something failed mid-run
  language          TEXT DEFAULT 'en',

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for the dashboard's common queries
CREATE INDEX idx_agent_runs_started_at ON agent_runs (started_at DESC);
CREATE INDEX idx_agent_runs_lead_status ON agent_runs (lead_status);
CREATE INDEX idx_agent_runs_session_id ON agent_runs (session_id);
CREATE INDEX idx_agent_runs_meeting_booked ON agent_runs (meeting_booked) WHERE meeting_booked = true;
```

### Design notes
- **One row per conversation, not per message.** Update the row as the conversation progresses (upsert on `session_id`), rather than inserting a new row per turn. Keeps aggregate queries (conversion rate, leads/day) simple.
- **`transcript` as JSONB**, not a separate messages table, unless you expect to query individual messages often. For a sales-lead dashboard, the whole transcript is usually read as a unit (viewed on a detail page), so JSONB keeps it simple.
- **`error` column matters more than it looks.** This is your signal for "the agent silently failed to sync a lead to the CRM" — surface it in the dashboard so nothing gets lost quietly.
- **`crm_lead_id`** lets support staff cross-reference a dashboard row with the actual CRM record without guessing.

### Optional companion table — FAQ clustering
If you want the "Frequently Asked Questions" dashboard section to be real data rather than a static list, add a periodic n8n job (e.g. nightly) that clusters/summarizes recent transcripts via an LLM call and writes here:

```sql
CREATE TABLE faq_summary (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  question     TEXT NOT NULL,
  frequency    INTEGER NOT NULL DEFAULT 1,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 6. Dashboard Views This Schema Supports

- **Runs table**: filterable by date range, `lead_status`, `service_recommended`
- **Run detail page**: full `transcript`, `actions_taken`, integration sync status
- **Aggregate stats**: total visitors (count of `session_id`), conversion rate (`meeting_booked` / total), popular services (`GROUP BY service_recommended`)
- **Lead status tracking**: `WHERE lead_status = 'hot' AND meeting_booked = false` → who needs follow-up
- **AI performance**: average `duration_ms`, error rate (`WHERE error IS NOT NULL`)

---

## 7. Key Principle to Hand to the Coding Agent

Keep n8n's native execution log for internal debugging only. All client-facing dashboard data comes from `agent_runs`, written explicitly at the end of every run — never inferred from n8n's internal workflow state.
