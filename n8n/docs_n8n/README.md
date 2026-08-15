# docs_n8n — node-level reference for the live workflows

Documents the **`*.local.json`** exports only — the five workflows shown as published
in the n8n UI. The non-`.local` siblings are the same graphs with credentials blanked
to `REPLACE_*` placeholders; see [§ local vs base](#local-vs-base) for the exact delta.

Read order:

| Doc | What it answers |
|---|---|
| **[contracts.md](contracts.md)** | The payload every workflow expects, the Postgres RPC signatures, the `agent_runs` columns. **Start here if you are wiring the agent's tools.** |
| [01-sales-agent-events.md](01-sales-agent-events.md) | 26 nodes. `POST /webhook/sales-agent` → AutoCRM + Sheets + WhatsApp + email + write-back |
| [02-cal-booking-actions.md](02-cal-booking-actions.md) | 10 nodes. `POST /webhook/cal-action` → Cal.com book / cancel / reschedule, replies synchronously |
| [03-cal-booking-events.md](03-cal-booking-events.md) | 11 nodes. Inbound Cal.com webhook → reconcile `agent_runs` + prep task |
| [04-faq-clustering-nightly.md](04-faq-clustering-nightly.md) | 6 nodes. Cron 02:17 → cluster transcripts → `faq_summary` |
| [05-sales-agent-error-handler.md](05-sales-agent-error-handler.md) | 5 nodes. Error trigger → `agent_runs.error` + ops email |

58 nodes total.

---

## Topology

```
 api/tools.py                                    Cal.com
      │                                             │
      │ emit_event()          call_cal_action()     │ BOOKING_CREATED
      │ fire-and-forget       awaited               │ BOOKING_RESCHEDULED
      │                            │                │ BOOKING_CANCELLED
      ▼                            ▼                ▼
 ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
 │ sales-agent-     │   │ cal-booking-     │   │ cal-booking-     │
 │ events           │   │ actions          │   │ events           │
 │ /webhook/        │   │ /webhook/        │   │ /webhook/        │
 │   sales-agent    │   │   cal-action     │   │   cal-booking/…  │
 └───┬──────────┬───┘   └────────┬─────────┘   └────────┬─────────┘
     │          │                │                      │
     │      AutoCRM          api.cal.com/v2          AutoCRM
     │      Sheets                │                   (task PATCH)
     │      Mailjet               │                      │
     │      WhatsApp              │                      │
     ▼                            ▼                      ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  Supabase (own project) — agent_runs · faq_summary           │
 │  writes via RPC:  mark_run_outcome · record_run_error        │
 │                   apply_cal_booking_event · bump_faq         │
 └──────────────────────────────────────────────────────────────┘
     ▲                                              ▲
     │ record_run_error                             │ bump_faq
 ┌───┴──────────────────────┐              ┌────────┴─────────────┐
 │ sales-agent-error-handler│◄─errorWorkflow│ faq-clustering-      │
 │ (error trigger)          │   for all 4   │ nightly (cron 02:17) │
 └──────────────────────────┘              └──────────────────────┘
```

Rule the whole design rests on: **the visitor never waits on an integration.**
`sales-agent-events` answers `onReceived` (200 before any work runs).
`cal-booking-actions` is the one exception — it answers `lastNode`, because the
agent cannot confirm a booking it has not yet made.

---

## Live credentials (as embedded in the `.local.json` files)

| Credential type | ID | Display name | Used by |
|---|---|---|---|
| `httpHeaderAuth` | `Dbye85qyr3DiF3jG` | Sales Agent Webhook Secret | `Webhook`, `Actions Webhook` |
| `supabaseApi` | `szox4UWsZWBiuv2V` | Supabase account | all 10 Supabase-facing nodes |
| `googleSheetsOAuth2Api` | `WjcvKmC4XjMNT0jE` | Google Sheets account | `Append to Sheets` |
| `mailjetEmailApi` | `6ApVgdiLsjekE01f` | Mailjet Email account | all 6 email nodes |
| `whatsAppApi` | **`REPLACE_WHATSAPP_CRED_ID`** | WhatsApp Cloud API | `WhatsApp Hot Alert` — **still a placeholder, see [contracts.md §10.2](contracts.md#102-whatsapp-hot-alert-still-carries-replace_whatsapp_cred_id)** |

Error workflow ID wired into the other four: **`15HLl7LZ0x9ORtMb`**.

The header-auth credential's **header name must be `X-Webhook-Secret`** — that is
the header `api/n8n_client.py::_headers()` sends. The value is the app's
`N8N_WEBHOOK_SECRET`.

`cal-booking-events` has **no credential at all**: Cal.com cannot send a custom
auth header (it HMAC-signs the body into `X-Cal-Signature-256`), so the secret is
the unguessable path segment. Treat that whole URL as a credential.

---

## n8n environment variables

Every `{{ $env.X }}` referenced across the five workflows:

| Variable | Read by | Notes |
|---|---|---|
| `AUTOCRM_BASE_URL` | events, cal-events | No trailing slash. Must be **https** if AutoCRM runs `DEBUG=False`, or the login cookie is `Secure` and never comes back |
| `AUTOCRM_SERVICE_EMAIL` | events, cal-events | Service account, role **`admin`** |
| `AUTOCRM_SERVICE_PASSWORD` | events, cal-events | |
| `SUPABASE_URL` | all except error-handler's trigger | `https://[ref].supabase.co`, no trailing slash. The RPC nodes build their URL from this; the credential only supplies `apikey` / `Authorization` |
| `SALES_TEAM_EMAIL` | events, cal-events | `toEmail` on every rep notification |
| `NOTIFY_FROM_EMAIL` | events, cal-events, error-handler | `fromEmail`; must be a verified Mailjet sender |
| `NOTIFY_FROM_NAME` | same | Optional, falls back to `Systematic IT Solutions` |
| `OPS_ALERT_EMAIL` | error-handler | Where failures land |
| `MEETING_TIMEZONE` | events, cal-actions, cal-events | Default `Asia/Karachi`. **Keep in step with the app's `MEETING_TIMEZONE`** — `api/config.py` uses the same default |
| `WHATSAPP_PHONE_NUMBER_ID` | events | Meta Cloud API sender id |
| `SALES_LEAD_WHATSAPP` | events | Recipient for hot-lead alerts |
| `CAL_API_KEY` | cal-actions | `cal_live_…`, sent as `Bearer` |
| `CAL_USERNAME` | cal-actions | Fallback when no `event_type_id` is passed |
| `CAL_EVENT_TYPE_SLUG` | cal-actions | Fallback, e.g. `30min` |
| `ANTHROPIC_API_KEY` | faq-clustering | `x-api-key` header |

> **Self-hosted n8n:** set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` or every `$env`
> expression silently resolves to empty and you chase phantom auth failures.

---

## Google Sheet binding

`Append to Sheets` is bound by **URL**, not id:

```
https://docs.google.com/spreadsheets/d/1r0qOPJDRONAvYXJhwTBHNtfsuF1x_eagCLyG-DObpqA/edit
tab: Sheet1
```

The tab needs a header row matching the nine mapped columns, in any order:

```
captured_at · session_id · name · email · phone · company · service · score · crm_lead_id
```

Operation is `appendOrUpdate` matching on `session_id` — one row per conversation
regardless of redelivery.

---

## local vs base

`diff` of each `X.json` against `X.local.json` — the graphs are byte-identical
apart from:

| | `X.json` | `X.local.json` |
|---|---|---|
| `name` | `sales-agent-events` | `sales-agent-events (local)` |
| header-auth cred | `REPLACE_HEADER_AUTH_CRED_ID` | `Dbye85qyr3DiF3jG` |
| supabase cred | `REPLACE_SUPABASE_CRED_ID` | `szox4UWsZWBiuv2V` |
| sheets cred | `REPLACE_SHEETS_CRED_ID` | `WjcvKmC4XjMNT0jE` |
| mailjet cred | `REPLACE_MAILJET_CRED_ID` | `6ApVgdiLsjekE01f` |
| sheet binding | `mode: id` / `REPLACE_SHEET_ID`, tab `Leads` | `mode: url` / real URL, tab `Sheet1` |
| `settings.errorWorkflow` | `REPLACE_ERROR_WORKFLOW_ID` | `15HLl7LZ0x9ORtMb` |
| workflow `id` | absent | `calActions000001`, `calEvents0000001` (cal pair only) |

No node, parameter, expression or connection differs. Anything you change in a
`.local` file must be mirrored back into its base sibling by hand.

---

## Retry and error policy (uniform across all five)

Every node that touches the network: **`retryOnFail: true`, `maxTries: 3`,
`waitBetweenTries: 5000`** — n8n's ceiling on both. A recorded failure therefore
means a real failure, not a transient 429.

Beyond retry, one question decides `onError`: *if this fails, is the downstream
work still meaningful?*

| `onError` | Meaning | Examples |
|---|---|---|
| `continueErrorOutput` | Branch stops, output 1 goes to the error sink | logins, cookie extraction, lookups, the lead upsert, every write-back, every Cal HTTP call |
| `continueRegularOutput` | Node is skipped, chain carries on | Sheets, WhatsApp, all rep/lead emails, both task creations, `Upsert faq_summary`, `Record Failure` itself |
| *(default, stop)* | Execution goes red | `Alert Ops` only — the signal of last resort |

The split matters most at `Upsert AutoCRM Lead`. Making it non-fatal would look
graceful and be worse: every node after it reads
`$('Upsert AutoCRM Lead').item.json.id`, so the chain would write a blank
`crm_lead_id` and still set `crm_synced = true`.
