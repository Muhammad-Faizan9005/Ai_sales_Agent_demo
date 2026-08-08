# n8n Setup — AI Sales Agent

Three workflows. Import in this order; the first two reference the third by ID.

| File | Trigger | Purpose |
|---|---|---|
| `sales-agent-error-handler.json` | Error trigger | Writes failures to `agent_runs.error`, alerts ops |
| `sales-agent-events.json` | Webhook `POST /webhook/sales-agent` | The §9 fan-out: AutoCRM, Sheets, WhatsApp, email, write-back |
| `faq-clustering-nightly.json` | Cron 02:17 | Clusters recent transcripts into `faq_summary` |

Import order: error handler first, copy its workflow ID, then paste that ID into the other two (`settings.errorWorkflow`, currently `REPLACE_ERROR_WORKFLOW_ID`).

---

## 1. Verified against the AutoCRM source (2026-08-09)

The three unknowns this section used to list have been checked directly against
`backend/` rather than inferred from the handover doc. Two of the three answers
were **not** what the handover implied, and the workflows have been corrected.

**1.1 — Does `POST /api/leads/ingest` accept `X-AI-Service-Token`? No.**

`app/routers/leads.py` declares it `Depends(require_auth)` — cookie only. The
service token (`require_ai_agent_auth`) is wired to `/api/agent/*` routes and
nothing else, so it 401s here. The cookie path stays.

Do not "simplify" this by swapping the dependency to
`require_human_or_ai_agent_auth`. That looks like a one-word change and isn't:
`require_ai_agent_auth` returns `id` = the `ai_agent_credentials` row id and no
`role`, so `ingest` falls into its non-admin branch and sets
`owner_id` to a credential UUID — which violates
`leads.owner_id REFERENCES agents(id)`. It needs an owner_id guard too.

**1.2 — `LeadCreate` fields: `name`, not `full_name` — but use `/ingest`.**

`POST /api/leads/` requires `name` (min 2 chars) and silently drops
`full_name`, so the old body 422'd. Rather than rename the field, the workflow
now posts to **`/api/leads/ingest`**, which is strictly better here:

| `/api/leads/` | `/api/leads/ingest` |
|---|---|
| `name` only | accepts `full_name`, `lead_name`, or `name` |
| always inserts | **upserts by email** |
| org must be passed | auto-creates the organization from `company` |
| note = second call | inline via a `notes` key |

That collapsed `Create AutoCRM Lead` + `Attach Transcript Note` into a single
`Upsert AutoCRM Lead` node, and the note insert dedupes on exact content — so a
replayed webhook writes nothing even before the `Already Synced` guard.

Two traps on this path: `score` is `int 0–100`, **not** `"hot"`/`"warm"`/
`"cold"` (the backend recalculates it anyway, so the workflow does not send
it), and `/ingest` takes **no trailing slash** while `/api/leads/` requires one.

**1.3 — `TaskCreate` / `NoteCreate` fields are as sent.** `title`,
`description`, `entity_type`, `entity_id`, `status`, `priority` for tasks;
`entity_type`, `entity_id`, `content` for notes. Both correct as shipped.

**1.4 — Lead status is NOT free text.** The handover said it was; it isn't.
`normalize_status()` in `app/utils/statuses.py` restricts leads to
`{new, contacted, nurture, qualified, unqualified, junk}` and rejects anything
else with a 422. The `meeting_booked` branch's old `meeting_scheduled` was
therefore a guaranteed failure and is now `qualified`. Task status is a
different set — `{backlog, todo, in_progress, done, canceled}` — and the
workflow's `open` is fine, aliased to `todo`.

---


## 2. AutoCRM service account

n8n needs a real AutoCRM user, because there is no bearer path for user auth.

**Create it as an admin**, not via `/api/auth/register` — §8.2 notes that self-registration always assigns `sales_rep`:

```
POST /api/admin/users
{ "email": "n8n-agent@yourdomain.com", "full_name": "AI Sales Agent (n8n)", "role": "admin" }
```

**Use `admin`, not `sales_manager`.** Two source-level reasons, both found after
this file first recommended `sales_manager`:

- `notes.py` gates lead notes on `role == "admin"` exactly
  (`_can_manage_lead_notes`). A manager falls through to a per-lead access
  check. It happens to pass for leads this account created, and fails the first
  time you attach a note to a lead it didn't — a bug that hides until the
  workflow touches an existing lead.
- `leads.py` forces `owner_id = current_user.id` for any non-admin creator. As
  a manager, every website lead lands owned by the bot account, which defeats
  the rep-assignment step. Admins can leave it unset for real routing.

Task creation accepts either (`_has_task_write_role` allows `admin`,
`sales_manager`), so `admin` costs nothing.

Do not create it via `/api/auth/register` — self-registration always assigns
`sales_rep`, which is scoped to owned leads only.


Then set two n8n environment variables to those credentials (§4 below).

**CORS is not your problem here.** §2 warns that new frontend domains must be allow-listed in `app/main.py` because `allow_credentials=True` — that applies to browsers. n8n is a server-side client; the browser CORS preflight never happens.

---

## 3. n8n credentials to create

Create these in **Credentials**, then swap the placeholder IDs in the JSON (or just re-select each credential in the UI after import — faster and less error-prone).

| Placeholder in JSON | Credential type | Notes |
|---|---|---|
| `REPLACE_HEADER_AUTH_CRED_ID` | Header Auth | Name: `X-Webhook-Secret`. Value: a long random string — this is your app's `N8N_WEBHOOK_SECRET` |
| `REPLACE_PG_CRED_ID` | Postgres | Supabase connection. Use the **direct** connection (port 5432), not the pooler — n8n holds connections across nodes |
| `REPLACE_SHEETS_CRED_ID` | Google Sheets OAuth2 | Scope `spreadsheets`. Share the sheet with the OAuth account |
| `REPLACE_WHATSAPP_CRED_ID` | WhatsApp | Meta Cloud API access token + app secret |
| `REPLACE_SMTP_CRED_ID` | SMTP | Or swap the `emailSend` nodes for Gmail / SendGrid / Mailjet |

AutoCRM deliberately has **no** n8n credential — it authenticates by logging in per execution with env-var credentials, since n8n has no built-in cookie-jar credential type.

---

## 4. n8n environment variables

Set on the n8n instance (Settings → Variables on Cloud, or the container env on self-hosted):

```
AUTOCRM_BASE_URL=https://your-autocrm-deployment
AUTOCRM_SERVICE_EMAIL=n8n-agent@yourdomain.com
AUTOCRM_SERVICE_PASSWORD=...

ANTHROPIC_API_KEY=sk-ant-...

SALES_TEAM_EMAIL=sales@systematicitsolutions.com
OPS_ALERT_EMAIL=you@yourdomain.com
NOTIFY_FROM_EMAIL=agent@systematicitsolutions.com

WHATSAPP_PHONE_NUMBER_ID=...
SALES_LEAD_WHATSAPP=+92...
```

Also replace `REPLACE_SHEET_ID` in the `Append to Sheets` node with your spreadsheet ID, and create a `Leads` tab with a header row matching the mapped columns: `captured_at, session_id, name, email, phone, company, service, score, crm_lead_id`.

> **Self-hosted n8n:** `$env` access in expressions is blocked unless you set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. Without it every `{{ $env.X }}` resolves empty and you'll chase phantom auth failures.

---

## 5. The payload your app must send

The workflow reads exactly this shape. Your `emit_event` tool builds it:

```json
{
  "event_type": "lead_created",
  "session_id": "sess_01H...",
  "score": "hot",
  "fields": {
    "visitor_name": "Ali Raza",
    "visitor_email": "ali@acme.com",
    "visitor_phone": "+92...",
    "company_name": "Acme Corp",
    "service_recommended": "local-seo"
  },
  "pages_visited": ["/seo/local-seo/", "/contact/"],
  "transcript_text": "user: ...\nassistant: ..."
}
```

`meeting_booked` adds a `meeting` object:

```json
"meeting": {
  "start_time": "2026-08-14T10:00:00Z",
  "reschedule_url": "https://cal.com/reschedule/...",
  "cancel_url": "https://cal.com/booking/..."
}
```

Sent with header `X-Webhook-Secret: <your secret>`, fire-and-forget — never awaited in the chat turn.

Note `transcript_text` is a pre-flattened string, not the raw JSONB array. Flatten it app-side. The workflow truncates to 4500 characters: on the `/ingest` path the note is written straight to a `TEXT` column (bypassing `NoteCreate`'s 5000-char cap), so the real ceiling is AutoCRM's 1 MB request limit — 4500 is a readability choice, and you can raise it.

---

## 6. WhatsApp will not work out of the box

Meta's Cloud API only permits free-form text inside a 24-hour customer-service window opened by the *recipient* messaging you first. Your sales lead has not messaged your business number, so a free-form hot-lead alert gets rejected.

Two ways through:

- **Approved message template** — submit one with variables for name / company / service, then switch the WhatsApp node from `text` to `template`. This is the correct production answer and takes a day or two for Meta approval.
- **Telegram or Slack instead** — swap the node, zero approval, works immediately. For a capability demo this is the honest shortcut, and worth saying out loud in your writeup rather than shipping a WhatsApp node that silently errors.

The node ships with `onError: continueRegularOutput`, so a WhatsApp failure won't block the CRM write-back either way.

---

## 7. One design decision left open

The `meeting_booked` branch does `PATCH /api/leads/{id}` with
`status: "qualified"` (see §1.4 — the original `meeting_scheduled` was invalid).

AutoCRM's fuller qualification step is `POST /api/leads/{lead_id}/convert-to-deal`,
which sets the lead to `qualified` *and* creates a deal. Arguably a booked
meeting *is* qualification. The problem is that conversion wants a deal `value`,
and a website chat doesn't produce one — you'd be inventing a number, and
inventing numbers is exactly what plan §11.1 warns about on pricing.

Recommendation: leave it as the status update and let a human convert to a deal
after the call, once there's a real number. If you want auto-conversion instead
it's a one-node swap — but pick deliberately rather than by default.


---

## 8. Before the first real run

- [ ] **Run `../schema.sql`.** `agent_runs` and `faq_summary` do not exist
      anywhere — not in AutoCRM (whose `ai_agent_runs` is the separate AI_service
      microservice's log), not in your Supabase project. Every Postgres node in
      all three workflows hits them, so without this the first node of every
      execution fails. The file includes the `UNIQUE` on `faq_summary.question`
      that `ON CONFLICT` needs and the `pages_visited` / `handoff_requested`
      columns from plan §10 — no follow-up ALTERs required.
- [ ] Create the AutoCRM service account as **`admin`** (§2) and set
      `AUTOCRM_SERVICE_EMAIL` / `_PASSWORD` to it
- [ ] If AutoCRM runs with `DEBUG=False` (i.e. production), `AUTOCRM_BASE_URL`
      **must be https** — cookies are then issued `Secure`, and over plain http
      the browser-equivalent client gets no `access_token`, so `Extract Cookies`
      throws its "login returned no access_token" error and you chase a
      credential bug that isn't one
- [ ] Import error handler → copy its ID → paste into the other two workflows' settings
- [ ] Fire a test `lead_created` with curl and confirm: lead appears in AutoCRM, note attached, Sheets row added, email received, `agent_runs.crm_lead_id` populated
- [ ] Fire the **same** payload again — it must stop at `Already Synced` and create nothing. Then confirm the layer beneath it: temporarily bypass that guard and re-fire, and `/ingest` should update the existing lead rather than duplicate it, with no second note. The widget rehydrating on every WordPress page load makes duplicate emits routine, not hypothetical
- [ ] Break something on purpose (wrong AutoCRM password) and confirm `agent_runs.error` gets written

---

## Note on the exported JSON

These were hand-authored against n8n's export format, not round-tripped through a live instance. Node `typeVersion` values track recent n8n releases; if your instance is older, a node may import with a version warning and need re-selecting in the UI. Credentials and the Sheets document ID are placeholders by design — n8n never exports secrets. Expect to re-pick credentials on each node after import.
