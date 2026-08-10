# n8n Setup — AI Sales Agent

> **Node-level reference:** [`docs_n8n/`](docs_n8n/) documents all 58 nodes across
> the five live `*.local.json` workflows — every parameter, expression, payload and
> schema. Start at [`docs_n8n/contracts.md`](docs_n8n/contracts.md) when wiring the
> agent's tools. This file stays the *setup* guide: credentials, env vars, and the
> decisions behind the design.

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

## 2.1 Which database — a separate Supabase project

`agent_runs` and `faq_summary` go in **their own new Supabase project**, not
AutoCRM's. Free tier covers it, so this costs nothing.

The deciding reason is credential blast radius. n8n holds a **Supabase
`service_role` key**, which bypasses RLS on every table in whatever project it
belongs to and also reaches the Auth and Storage admin endpoints. Pointed at
AutoCRM's project that is total access — `agents` (your users), `customers`,
`deals`, `leads`, `revoked_tokens`. In its own project it reaches exactly two
tables. n8n is third-party-hosted and runs community nodes; it should not hold a
key like that for the CRM.

The architecture already assumes this split — every AutoCRM touch in these
workflows goes over HTTP with a scoped service account, never SQL. The database
boundary should match the one the workflows already respect.

Two supporting reasons:

- AutoCRM already has a table called **`ai_agent_runs`** (the AI\_service
  microservice's run log). Putting `agent_runs` beside it in the same database
  is a 3am confusion generator — two similar names, two unrelated systems.
- Different clients and lifecycles: this agent is for systematicitsolutions.com
  and is a capability demo; AutoCRM is your product.

One thing that is *not* a reason: AutoCRM's Alembic setup has
`target_metadata = None`, so autogenerate is a no-op and would never try to drop
unknown tables. Hand-written migrations only.

**No connection string needed.** The workflows talk to this project over HTTPS
through PostgREST, using only the project URL and the `service_role` key — so
the whole IPv4/IPv6, pooler-vs-direct question does not arise. (For reference,
if you ever do connect a SQL client: the direct connection
`db.[ref].supabase.co:5432` is IPv6-only without the paid IPv4 add-on, so use the
Session pooler `aws-[region].pooler.supabase.com:5432` instead.)

Run [`../schema.sql`](../schema.sql) against this project — paste the whole file
into the SQL editor. It creates both tables, then:

- **Deny-all RLS.** Supabase publishes `public` through PostgREST, so without it
  the anon key — which ships in browser bundles — would read every chat
  transcript, name, email and phone. Zero policies blocks `anon` and
  `authenticated`; n8n is unaffected because `service_role` carries `BYPASSRLS`.
- **Three functions** — `mark_run_outcome`, `record_run_error`, `bump_faq` —
  which is how the writes happen (§3.1).
- **`REVOKE … FROM PUBLIC`** on all three. Postgres grants `EXECUTE` to `PUBLIC`
  by default, which would have let the anon key overwrite any run's error text
  and inflate FAQ counts, quietly undoing the RLS above.

---

## 3. n8n credentials to create

Create these in **Credentials**, then swap the placeholder IDs in the JSON (or just re-select each credential in the UI after import — faster and less error-prone).

| Placeholder in JSON | Credential type | Notes |
|---|---|---|
| `REPLACE_HEADER_AUTH_CRED_ID` | Header Auth | Name: `X-Webhook-Secret`. Value: a long random string — this is your app's `N8N_WEBHOOK_SECRET` |
| `REPLACE_SUPABASE_CRED_ID` | Supabase API | **Its own project, not AutoCRM's** (see §2.1). Host = the project URL (`https://[ref].supabase.co`), Service Role Secret = Settings → API Keys → `service_role`. Used by both the Supabase nodes and the RPC HTTP nodes |
| `REPLACE_SHEETS_CRED_ID` | Google Sheets OAuth2 | Scope `spreadsheets`. Share the sheet with the OAuth account |
| `REPLACE_WHATSAPP_CRED_ID` | WhatsApp | Meta Cloud API access token + app secret |
| `REPLACE_SMTP_CRED_ID` | SMTP | Or swap the `emailSend` nodes for Gmail / SendGrid / Mailjet |

AutoCRM deliberately has **no** n8n credential — it authenticates by logging in per execution with env-var credentials, since n8n has no built-in cookie-jar credential type.

---

## 3.1 How the Supabase access is split — reads native, writes by RPC

Everything reaches Supabase over HTTPS on the one `supabaseApi` credential. But
it lands in two different node types, and the split is not stylistic:

| Nodes | Type | Why |
|---|---|---|
| `Check Existing Sync`, `Lookup Lead (meeting)`, `Lookup Lead (proposal)`, `Fetch Recent Transcripts` | **Supabase** node, Get Many | Plain filtered reads. The node does these cleanly |
| `Write Back (lead_created)`, `Write Back (meeting)`, `Write Back (proposal)`, `Record Failure`, `Record Error on Run`, `Upsert faq_summary` | **HTTP Request** → `POST /rest/v1/rpc/<fn>` | See below |

The Supabase node has exactly five operations — Create, Delete, Get, Get Many,
Update — and **no upsert**. Three of those writes are `INSERT … ON CONFLICT`, and
two problems make emulating them wrong rather than merely verbose:

- **Lost updates.** `bump_faq` does
  `frequency = faq_summary.frequency + EXCLUDED.frequency` in one statement. As
  Get → IF → Update, two concurrent runs both read the old count and one
  increment vanishes.
- **Lost failures.** `record_run_error` must capture an error even when no
  `agent_runs` row exists yet. A plain Update matches zero rows and drops the
  failure silently — the exact opposite of what an error sink is for.

There is a third, subtler reason the write-backs are RPC too. The Supabase node
types field values as **strings**, so an expression returning `null` arrives as
`""`. `Write Back (meeting)` and `(proposal)` set `error` to null on success —
writing `""` there would make every successful run look failed to any dashboard
checking `error IS NOT NULL`. `mark_run_outcome` sidesteps it by `COALESCE`-ing
every optional argument, so a caller simply omits the keys it does not set.

Two details worth knowing if you edit these nodes:

- The RPC URL uses **`{{ $env.SUPABASE_URL }}`**, not the credential. n8n does not
  expose credentials to the expression layer, so `{{ $credentials.host }}` does
  not resolve. The credential still supplies the `apikey` and `Authorization`
  headers via `predefinedCredentialType`.
- The Supabase node's **Get Many has no "select" option**, so the column list is
  appended to the filter string (`…&select=crm_lead_id`). `Order By` *is* a real
  parameter, so `Fetch Recent Transcripts` uses it rather than an inline `order=`.

If you add a function to `schema.sql` later, remember the `REVOKE … FROM PUBLIC`
and the `NOTIFY pgrst, 'reload schema'` — without the latter PostgREST 404s the
new function until it next restarts.

---

## 4. n8n environment variables

Set on the n8n instance (Settings → Variables on Cloud, or the container env on self-hosted):

```
AUTOCRM_BASE_URL=https://your-autocrm-deployment
AUTOCRM_SERVICE_EMAIL=n8n-agent@yourdomain.com
AUTOCRM_SERVICE_PASSWORD=...

# The AI Sales Agent's own Supabase project (§2.1) — no trailing slash.
# The six RPC nodes build their URL from this; the apikey and Authorization
# headers come from the Supabase credential, not from here.
SUPABASE_URL=https://[ref].supabase.co

ANTHROPIC_API_KEY=sk-ant-...

SALES_TEAM_EMAIL=sales@systematicitsolutions.com
OPS_ALERT_EMAIL=you@yourdomain.com
NOTIFY_FROM_EMAIL=agent@systematicitsolutions.com

WHATSAPP_PHONE_NUMBER_ID=...
SALES_LEAD_WHATSAPP=+92...
```

Also replace `REPLACE_SHEET_ID` in the `Append to Sheets` node with your spreadsheet ID, and create a `Leads` tab with a header row matching the mapped columns: `captured_at, session_id, name, email, phone, company, service, score, crm_lead_id`.

> **Self-hosted n8n:** `$env` access in expressions is blocked unless you set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. Without it every `{{ $env.X }}` resolves empty and you'll chase phantom auth failures.

### Cal.com variables

The two Cal workflows add three more. They already exist in `Ai Sales Agent/.env`; copy the values across:

```
CAL_API_KEY=cal_live_...
CAL_USERNAME=muhammad-faizan-haider-ali-nb2t6u
CAL_EVENT_TYPE_SLUG=30min
```

Verified live on 2026-08-09: the key authenticates against `GET /v2/me`, and `30min`
resolves to event type **6597690** (the account also has `15min` = 6597691 and
`secret` = 6597692). Booking by slug + username avoids hard-coding the numeric id,
but you can pass `event_type_id` per request to override it.

---

## 4.1 Cal.com — book, cancel and reschedule (API v2)

**n8n has no Cal.com action node.** It ships only a *Cal Trigger*, and that trigger
is currently broken: it still calls the decommissioned v1 API and fails with
*"API v1 has been decommissioned. Please migrate to API v2"*
([n8n community](https://community.n8n.io/t/cal-com-connector-node-doesnt-work-after-v2-api/295058)).
There is an [open feature request](https://community.n8n.io/t/need-your-help-upvoting-a-feature-request/300025).
So both Cal workflows use plain HTTP Request nodes against `api.cal.com/v2`.

### The trap: `cal-api-version` is per-endpoint

Not one global value. Send the wrong date and you get `404 Cannot GET /v2/slots`,
which reads like a bad URL rather than a bad header. Verified by direct probe:

| Endpoint | Version |
|---|---|
| `GET /v2/slots` | `2024-09-04` |
| `GET /v2/event-types` | `2024-06-14` |
| `POST /v2/bookings` and everything under `/v2/bookings/{uid}/...` | `2024-08-13` |

Every response wraps in `{ status, data }`, so expressions read `$json.data.uid` —
never `$json.uid`.

### `cal-booking-actions` — outbound

`POST /webhook/cal-action`, guarded by the same `X-Webhook-Secret` credential as
the main events workflow. Unlike that one it responds with the **last node**, so
the caller gets the booking uid back for a confirmation message.

```json
{ "action": "book",        "session_id": "test-003",
  "start": "2026-08-20T09:00:00Z",
  "attendee": { "name": "Shafi", "email": "shafi@example.com", "timeZone": "Asia/Karachi" } }

{ "action": "cancel",      "session_id": "test-002",
  "booking_uid": "bk_abc123", "reason": "Client unavailable" }

{ "action": "reschedule",  "session_id": "test-002",
  "booking_uid": "bk_abc123", "start": "2026-08-21T14:00:00Z" }
```

Replies `{ ok, action, booking_uid, status, start, meeting_url }`. On failure
`ok:false` with an `error` string — n8n cannot set a 4xx from a Set node, so
**callers must check `ok`, not the HTTP status**.

Two behaviours worth knowing:

- **`POST /v2/bookings` is public.** Verified: an unauthenticated call reached
  event-type validation rather than 401. The Bearer key is still sent so hidden
  event types resolve.
- **Cal.com rejects a `start` carrying a UTC offset.** `Validate Request`
  normalises it — `2026-08-20T09:00:00+05:00` is converted to `2026-08-20T04:00:00Z`
  before the call.

### `cal-booking-events` — inbound

Register the webhook URL at **Cal.com → Settings → Developer → Webhooks** for
`BOOKING_CREATED`, `BOOKING_RESCHEDULED`, `BOOKING_CANCELLED`. This catches what
the outbound path cannot see: the visitor moving or cancelling from the Cal.com
page or the email link.

On a change it updates `agent_runs`, **PATCHes the existing prep task** (moving
`due_at`, or setting `status: canceled`) and emails the rep. It never creates a
second task.

**Auth is an unguessable path, not a header.** Cal.com cannot send a custom auth
header — it signs the raw body with HMAC-SHA256 in `X-Cal-Signature-256`, which
n8n's Header Auth cannot verify. So the path carries a random segment
(`/webhook/cal-booking/<token>`) and the whole URL should be treated as a
credential. To harden it, set a `secret` on the Cal.com webhook and verify the
signature in a Code node before trusting the payload.

### Why `agent_runs` gained `cal_booking_uid`

Two Cal.com behaviours force it:

1. **`BOOKING_CANCELLED` omits booking metadata**
   ([calcom/cal.com#27783](https://github.com/calcom/cal.com/issues/27783)), so a
   cancel event carries the uid but *not* our `session_id`. Matching on session
   alone would silently drop every cancellation.
2. **Rescheduling mints a NEW uid** and cancels the old one. `apply_cal_booking_event`
   matches on the old uid and writes the new one in a single statement — otherwise
   the next cancel would 400 against a uid Cal.com already retired.

`crm_task_id` is stored for the same reason: it's what lets a reschedule move the
existing task instead of stacking a second "Discovery call" on the lead.

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

## 8. Error handling

Every node that touches the network retries **3× with 5s backoff** (n8n's
ceiling on both). Transient 429s and upstream blips are absorbed before anything
reaches an error path, so a recorded failure means a real failure.

Beyond retry, each node is classified by one question: **if this fails, is the
downstream work still meaningful?**

| | Nodes | `onError` | On failure |
|---|---|---|---|
| **Critical** | Login, Extract Cookies, Check Existing Sync, Upsert Lead, all 3 Write Backs, both Lookups, Update Lead Status | `continueErrorOutput` | Branch stops, `Record Failure` writes `agent_runs.error` |
| **Non-fatal** | Sheets, WhatsApp, all 3 Notify Rep, both task creations | `continueRegularOutput` | Skipped, chain continues to the write-back |

The split matters most at `Upsert AutoCRM Lead`. Making it non-fatal would look
more "graceful" and would be much worse: every downstream node reads
`$('Upsert AutoCRM Lead').item.json.id`, so the chain would write a blank
`crm_lead_id` and then set `crm_synced = true` — the dashboard reporting a
successful sync that never happened.

**`Record Failure`** is the single shared error sink. It stores the failing node
name (`$prevNode.name`) plus the n8n execution id, not the raw error text — the
Query Parameters field is comma-separated, so a message containing a comma would
shift every parameter after it. The stack trace stays in n8n's execution log,
and the DB gets a pointer to it. It uses `INSERT … ON CONFLICT` rather than
`UPDATE` so the failure is still recorded when no `agent_runs` row exists;
an `UPDATE` would match zero rows and lose it silently.

### The write-backs no longer lie

They previously set `sheets_synced = true` and `notification_sent = true`
unconditionally, while the Sheets and email nodes were already
`continueRegularOutput`. A failed email therefore wrote "notified" to the
dashboard. Those flags are now driven by whether the node actually errored:

```
notification_sent = {{ !$('Notify Rep (Email)').item.json.error }}
```

The meeting and proposal branches additionally record `'prep task not created'` /
`'follow-up task not created'` into `error`, since a lost rep reminder would
otherwise be invisible outside n8n's execution log.

### Zero-row lookups

Both `Lookup Lead` nodes have `alwaysOutputData: true`, so a `meeting_booked`
event arriving with no matching `agent_runs` row still emits an item. The
undefined `crm_lead_id` then fails `Update Lead Status` against AutoCRM, which
routes to `Record Failure`. Out-of-order events get recorded, not silently
dropped — but the message names `Update Lead Status`, not the real cause.

### The error handler protects itself

`sales-agent-error-handler.json` had `Record Error on Run → Alert Ops`
hard-chained. A transient DB blip meant the write failed *and* the human alert
never fired — total silence about the original failure. `Record Error on Run` is
now `continueRegularOutput`, so the alert always sends.

That workflow deliberately has **no** `errorWorkflow` of its own (it would
recurse). `Alert Ops` keeps the default stop-on-error so that a genuinely
undeliverable alert shows the execution red in n8n — the signal of last resort.

---

## 9. Before the first real run

- [ ] **Run `../schema.sql` in full.** `agent_runs` and `faq_summary` do not exist
      anywhere — not in AutoCRM (whose `ai_agent_runs` is the separate AI_service
      microservice's log), not in a fresh Supabase project. All ten Supabase-facing
      nodes across the three workflows hit them, so without this the first one in
      every execution fails. Run the **whole file**, not just the `CREATE TABLE`s:
      the six RPC nodes call functions defined in its second half, and each would
      return `404 Not Found` for a missing function. It also includes the `UNIQUE`
      on `faq_summary.question` that `ON CONFLICT` needs and the `pages_visited` /
      `handoff_requested` columns from plan §10 — no follow-up ALTERs required.
- [ ] **Set `SUPABASE_URL`** to the project URL (`https://[ref].supabase.co`, no
      trailing slash). The six RPC nodes build their URL from it; unset, they POST
      to `/rest/v1/rpc/...` on no host and fail with an invalid-URL error that does
      not obviously point at a missing variable.
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
- [ ] Break something on purpose (wrong AutoCRM password) and confirm `agent_runs.error` gets written. Expect it to take ~15s first — the login node retries 3× at 5s before giving up, which is correct behaviour, not a hang
- [ ] Break the **notification** instead (bad SMTP credential) and confirm the opposite: the lead still reaches AutoCRM, and `agent_runs.notification_sent` is `false` rather than `true`. That's the §8 write-back fix doing its job

---

## Note on the exported JSON

These were hand-authored against n8n's export format, not round-tripped through a live instance. Node `typeVersion` values track recent n8n releases; if your instance is older, a node may import with a version warning and need re-selecting in the UI. Credentials and the Sheets document ID are placeholders by design — n8n never exports secrets. Expect to re-pick credentials on each node after import.
