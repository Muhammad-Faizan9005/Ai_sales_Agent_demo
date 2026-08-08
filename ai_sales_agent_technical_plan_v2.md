# AI Sales Agent — Technical Build Plan v2

Supersedes the architecture in `ai_sales_agent_technical_plan.md` (v1). v1 is kept for reference; the delta is documented in §6.

Scope note: this is a capability/test task, not a production deployment. Cost figures are carried through for completeness but are not a selection criterion. Decisions below optimise for correctness and defensibility under review.

---

## 1. Verdict

**Hybrid — with the code / no-code seam at the lead event, not at the chat turn.**

v1 routed every chat turn through n8n. That breaks the two hardest requirements in the brief: real-time conversation and autonomous page control. n8n moves *behind* the lead event, where it is genuinely the best available tool.

| Layer | Built with | Why |
|---|---|---|
| Chat widget + page actions | Code (vanilla JS / Preact, Shadow DOM) | Must run in the visitor's browser and mutate the host DOM |
| Conversation + tool loop | Code (Vercel AI SDK `streamText`) | Needs token streaming and a client-side tool round trip |
| Knowledge base | Prompt-cached text, no vector DB | KB is ~40–45k tokens and stable (§6.1) |
| Post-lead fan-out | n8n | Settled API-to-API sequencing, ops-editable without a deploy |
| Booking | Cal.com API v2 | Public unauthenticated booking endpoint (§8) |
| Human handover | Existing Tawk.to install | Already on the site (§6.3) |
| Admin dashboard | Code (one page, reads Postgres) | n8n is not a BI tool |

---

## 2. Site audit (verified 2026-08-07)

Direct inspection of `https://systematicitsolutions.com`. These facts drive several decisions.

| Finding | Impact |
|---|---|
| WordPress 7.0.3 + Elementor 4.1.1, LiteSpeed cache | Full page loads, not SPA routing → §7 |
| Gravity Forms on `/contact/` | Form prefill becomes a URL param, not DOM manipulation → §6.2 |
| **Tawk.to already installed** | Requirement 12's human handover is already solved → §6.3 |
| 39 pages + 13 `service` CPT entries ≈ 52 URLs | Small, closed knowledge base |
| **0 blog posts** (no post sitemap exists) | Brief references blogs/portfolios/case studies/testimonials that do not exist → §11 |
| **No pricing page anywhere on the site** | Highest hallucination risk in the build → §11 |
| Junk URLs `/12745178-2/`, `/sdfjhsdui/` | Exclude from KB ingest and from the `navigate_to` allowlist |
| ~57k words of real text ≈ 76k tokens raw, ~40–45k deduped | Below the RAG threshold → §6.1 |

---

## 3. Why not pure no-code

Requirements 1, 3, 4, 6, 7, 9 and 11 are commodity — Chatbase, Voiceflow, Wonderchat and Intercom Fin all cover them out of the box.

Two requirements eliminate that path:

- **Requirement 2 — autonomous website navigation.** The model must call functions *inside the visitor's browser* to open pages, scroll, prefill and operate elements on their behalf. No off-the-shelf sales-bot platform exposes client-side tool registration. Telnyx AI Assistant, Crow and LiveKit do ship this pattern — but as SDKs you build against, i.e. code.
- **Requirement 8 — custom CRM.** An arbitrary in-house API, not a listed integration.

Requirement 10 (admin dashboard) and 12 (widget/API integration, secure storage, scalable architecture) reinforce it.

---

## 4. Why not n8n in the chat loop

n8n is the right tool for a large part of this build. It is the wrong tool for the turn loop:

1. **Latency.** ~20s AI Agent latency on n8n Cloud is a commonly reported figure. A sales chat needs sub-second first token.
2. **Streaming is fragile.** It only works when Chat Trigger, AI Agent and the model sub-node each opt in independently. A `Respond to Chat` node anywhere mid-chain finalises the turn and kills the stream.
3. **No client-side tool round trip.** The loop is: model emits tool call → browser executes → result returns → model continues. n8n's structured stream is not built to accept a tool result back from the client mid-turn.
4. **The agent is the product.** Every "when to graduate from n8n" source converges on the same signal: a customer-facing, differentiating agent belongs in code. n8n also has no prompt versioning, no eval gate, and JSON exports whose diffs are not reviewable.

Conversely, n8n's stated sweet spot — settled sequences between existing APIs, editable by non-engineers without a deploy — describes §9 exactly. That is where it goes.

---

## 5. Architecture

```
┌─ Visitor's browser (systematicitsolutions.com, WordPress) ──────────┐
│                                                                     │
│   Widget bundle (Shadow DOM)                                        │
│     • chat UI, streams tokens                                       │
│     • executes CLIENT-SIDE tools:                                   │
│         navigate_to · scroll_to · highlight · read_current_page     │
│         open_contact_form · open_booking · handoff_to_human         │
│     • sessionStorage: session_id + last N turns (survives reload)   │
└───────────────┬─────────────────────────────────────────────────────┘
                │  SSE stream (AI SDK protocol)
                ▼
┌─ /api/chat  (Next.js route, Vercel) ────────────────────────────────┐
│   streamText()                                                      │
│     • system prompt = sales persona + FULL site KB (prompt-cached)  │
│     • server-side tools:                                            │
│         get_available_slots · book_meeting · save_lead              │
│         request_proposal · emit_event                               │
└───────┬──────────────────────────────────┬──────────────────────────┘
        │ upsert on session_id             │ fire-and-forget POST
        ▼                                  ▼
┌─ Postgres (Supabase) ──────┐   ┌─ n8n (off the latency path) ───────┐
│   agent_runs               │   │   • create lead in custom CRM      │
│   faq_summary              │   │   • append row to Google Sheets    │
└───────┬────────────────────┘   │   • WhatsApp + email notify        │
        │                        │   • assign sales rep               │
        ▼                        │   • follow-up reminders            │
┌─ Admin dashboard ──────────┐   │   • nightly FAQ clustering job     │
│   reads Postgres directly  │   │   • writes sync status back        │
└────────────────────────────┘   └────────────────────────────────────┘
```

**Principle:** the visitor never waits on an integration. The turn loop touches only the model and Postgres. Everything with an external dependency — CRM, Sheets, WhatsApp, reminders — happens after the response has already streamed, in n8n, where a failure is retryable and visible instead of a dead chat window.

---

## 6. What changed from v1, and why

### 6.1 Cut the vector store

v1 specified a Supabase/Qdrant vector store with RAG over scraped site content.

Measured KB: **~40–45k tokens** (76k raw, minus repeated nav/footer boilerplate) across 52 stable URLs that change perhaps monthly.

That is well under the ~100–200k-token threshold at which retrieval starts paying for itself. Anthropic's own guidance is to use full context with caching below 200k tokens before building retrieval. With prompt caching, cache reads run at roughly 10% of base input price, so the whole KB rides along on every turn for very little.

Deleting the vector store removes: the embedding pipeline, the vector DB, the chunker, the re-index job, retrieval threshold tuning — and the entire *"the answer was in the corpus but top-k didn't surface it"* failure class. Recall becomes 100% by construction, and cross-page reasoning ("compare Local SEO with Technical SEO") works, which naive top-k retrieval is bad at.

KB build: a script hits `/wp-json/wp/v2/pages` and `/wp-json/wp/v2/service`, strips markup, drops the two junk URLs, and emits one markdown file with a `## <title> — <url>` header per page. Re-run on content change. That file is the cached prefix of the system prompt.

**Revisit when:** you can point at a real logged query that failed because content did not fit. Not before. The migration path is unglamorous and always available — chunk, embed, swap the KB block for retrieved chunks, leave the rest of the prompt untouched.

### 6.2 Cut the DOM manipulation for form prefill

v1 had the widget prefilling forms by manipulating the DOM. Against React-controlled inputs that requires native property setters plus synthetic `input` events; against Elementor + Gravity Forms it is simply fragile, and it breaks on any theme update.

Gravity Forms supports **dynamic population** natively. Enable it per field, and the agent navigates to:

```
/contact/?gf_name=Ali&gf_email=ali@acme.com&gf_company=Acme&gf_service=local-seo
```

One WordPress setting replaces a JS module and its whole maintenance surface. The agent's `open_contact_form` tool builds a URL; the browser does the rest.

Real DOM control is still available for `scroll_to` and `highlight`, where it is trivial and low-risk.

### 6.3 Cut the human-handover build

Requirement 12 asks for live human agent handover. **Tawk.to is already installed on the site.**

```js
Tawk_API.setAttributes({ name, email, phone, summary, transcriptUrl });
Tawk_API.maximize();
```

That is the entire `handoff_to_human` tool. Roughly 20 lines including error handling, versus building presence, routing and an agent console.

### 6.4 Moved n8n behind the lead event

Covered in §4 and §9.

---

## 7. The WordPress constraint

WordPress + Elementor means **full page loads, not client-side routing**. Every `navigate_to` destroys the document and remounts the widget. This is the single most likely thing to be got wrong, and there is a public n8n bug report from someone hitting exactly this on WordPress: the bot loses all context on every page change.

Requirements:

- **Bundle:** vanilla JS or Preact, mounted in a **Shadow DOM** so Elementor's CSS cannot leak in and the widget's cannot leak out.
- **Load:** one `<script defer>` via `wp_enqueue_script` in a child theme (not a plugin — nothing here needs the plugin lifecycle). `defer` keeps LiteSpeed from pulling it into the critical path.
- **Continuity:** `session_id` plus the last N turns live in `sessionStorage`. On mount, the widget rehydrates the thread and **reopens itself** if it was open before navigation. To the visitor it is one conversation that walked across five pages.
- **Post-navigation context:** after remount, the widget sends the new page's visible text as context on the next turn, so the agent answers about the page the visitor is actually on.
- **Exclusions:** `/wp-admin`, `/wp-login.php`, and both junk URLs.

**Navigation allowlist.** `navigate_to` accepts only paths present in the ingested sitemap. A model that invents `/pricing/` (a page that does not exist — §11) would otherwise walk the visitor into a 404 mid-sale.

---

## 8. Tool split

**Client-side** — model emits the call, browser executes it, result returns via `addToolOutput`, turn auto-resubmits (`sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithToolCalls`):

| Tool | Action |
|---|---|
| `navigate_to(path)` | Allowlisted path only; full page load, widget rehydrates |
| `scroll_to(selector)` | `scrollIntoView` |
| `highlight(selector)` | Temporary outline — draws the eye to what the agent is describing |
| `read_current_page()` | Visible text of the current page, so the agent answers from what is on screen |
| `open_contact_form(prefill)` | Navigates to `/contact/` with Gravity Forms dynamic-population params |
| `open_booking(prefill)` | Opens the Cal.com embed with name/email prefilled |
| `handoff_to_human(reason)` | `Tawk_API.setAttributes` + `maximize` |

**Server-side:**

| Tool | Action |
|---|---|
| `get_available_slots(from, to)` | `GET /v2/slots` |
| `book_meeting(...)` | `POST /v2/bookings` |
| `save_lead(fields, score)` | Upsert `agent_runs` on `session_id` |
| `request_proposal(...)` | Flags the run, fires `emit_event` |
| `emit_event(type, payload)` | Fire-and-forget POST to the n8n webhook |

**Booking: Cal.com over Calendly.** `POST /v2/bookings` is **public and requires no authentication**, which is exactly the shape needed for an agent booking on behalf of an anonymous visitor. Calendly's Scheduling API needs an OAuth app or a PAT and is gated behind paid tiers. Cal.com also exposes `GET /v2/slots` cleanly, and the flow is: fetch slots → offer 2–3 → confirm → book → return the reschedule/cancel links. Validate the slot is still open immediately before booking.

Requirement 5's two paths: *request a callback* → `save_lead` with a callback flag + `emit_event`; *direct jump to call* → a `tel:` link plus an instant-booking event type.

**Lead scoring** stays deterministic — plain code over the extracted fields (has phone + budget signal + timeline → hot, etc.), not an LLM judgement call. Same inputs must always give the same score, or the dashboard's Hot/Warm/Cold columns mean nothing.

---

## 9. The n8n layer

One webhook, switched on `event_type`. Everything here is off the latency path.

| Event | Fan-out |
|---|---|
| `lead_created` | Create in custom CRM → append to Google Sheets → assign rep → notify (WhatsApp + email) → write `crm_lead_id` and sync flags back to `agent_runs` |
| `meeting_booked` | Notify rep, update CRM lead status |
| `proposal_requested` | Notify + create follow-up reminder |
| `high_value_lead` | Immediate WhatsApp to the sales lead |
| nightly cron | Cluster recent transcripts via one LLM call → write `faq_summary` (powers the dashboard's FAQ panel with real data) |

Covers requirements 8 and 9 in full. This is legitimately where n8n is the best tool available: ops can change a notification channel or a routing rule without a deploy, retries and error branches are built in, and none of it can stall a visitor's chat.

**Sync status is written back**, so the dashboard can surface "lead captured but CRM sync failed" instead of losing it silently.

---

## 10. Data model

The v1 schema is sound and carries forward unchanged — `agent_runs` (one row per conversation, upsert on `session_id`) and `faq_summary`. See v1 §5 for the DDL.

Two additions:

```sql
ALTER TABLE agent_runs ADD COLUMN pages_visited JSONB;   -- navigation path the agent walked
ALTER TABLE agent_runs ADD COLUMN handoff_requested BOOLEAN NOT NULL DEFAULT false;
```

`actions_taken` now records client-side tool calls (`navigate_to:/seo/local-seo/`, `open_contact_form`, …), which is what makes requirement 2 auditable — you can prove the agent actually drove the site rather than just talked about it.

v1's principle holds: **the dashboard never reads n8n's execution log.** All client-facing data comes from `agent_runs`, written explicitly. n8n's log is for debugging only.

Dashboard (requirement 10) is one page over this schema — totals, conversion rate, popular services, lead status, FAQ panel, error rate, plus a run-detail view with the full transcript and the navigation path.

---

## 11. Open items — need the client, not code

Ordered by risk.

1. **Pricing does not exist on the site.** Requirement 6 lists pricing as a knowledge-base topic. There is no pricing page and no figures anywhere in the 52 URLs. An unconstrained sales agent *will* invent numbers — this is the highest-severity hallucination risk in the build. Either supply ranges for the KB, or hard-instruct: *"we scope per project — let me book you 15 minutes with a specialist."* **Blocking for launch, not for the build.** Default to the deflection instruction until told otherwise.
2. **Blogs, portfolios, case studies, testimonials do not exist as pages.** Requirements 2 and 6 assume them. Zero posts, no portfolio or case-study URLs. The agent cannot navigate to what is not there. Either they get built, or those capabilities are cut from scope explicitly.
3. **Custom CRM API** — auth method, lead payload schema, and whether it accepts an inbound webhook. Requirement 8 cannot be finished without it. Build against a stub in the meantime.
4. **Multi-language (requirement 11).** The model handles any language for free, but every page it navigates to is English. Decide: non-English visitors get chat-only answers, or key pages get translated. Chat-only is the sane default; state it rather than leaving it ambiguous.
5. **Two junk sitemap URLs** — worth flagging to the client regardless; they are an SEO liability for an SEO agency.
6. **Consent + retention.** Transcripts contain names, emails and phone numbers. Needs a disclosure line in the widget and a retention policy. Requirement 12 says "secure data storage" — this is the part of it that is a decision, not a config.

---

## 12. Build order

1. KB extraction script → single markdown file → verify token count
2. `/api/chat` with streaming, persona prompt, cached KB — text only, no tools
3. Widget shell in Shadow DOM + `sessionStorage` continuity across page loads *(hardest part; do it early — see §7)*
4. Client-side tools: `navigate_to`, `scroll_to`, `highlight`, `read_current_page`
5. Lead extraction + deterministic scoring + `agent_runs` upsert
6. Gravity Forms dynamic population + `open_contact_form`
7. Cal.com slots + booking
8. n8n webhook + fan-out (CRM against a stub until §11.3 lands)
9. Tawk.to handover
10. Dashboard
11. Nightly FAQ clustering

Steps 1–4 are the demonstrable core: an agent that knows the business and drives the site. Everything after is integration.

---

## 13. Cost (carried for completeness, not a selection criterion)

Roughly **$150–200/month** at moderate volume: LLM ~$100 (prompt-cached KB, ~$0.02/turn), Vercel ~$20, Supabase ~$25, n8n ~$24, Cal.com free tier, Tawk.to free.

The cached-KB decision (§6.1) is what keeps the LLM line small — uncached, the same design runs roughly 10× that. Noted because it is the one architectural choice where the cheap option is also the simpler and more accurate one.
