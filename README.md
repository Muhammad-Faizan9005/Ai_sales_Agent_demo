# AI Sales Agent

An AI-powered website sales assistant that answers service questions from a local knowledge base, qualifies leads, helps visitors book meetings, and records conversation activity for follow-up workflows.

The service uses FastAPI for its streaming chat API, Ollama for tool-capable language models, FAISS with Sentence Transformers for retrieval, PostgreSQL for run persistence, Cal.com for booking, and n8n for asynchronous sales automation.

## Features

- Streaming website chat over Server-Sent Events
- Retrieval-augmented answers from a locally built FAISS knowledge base
- Lead qualification and scoring
- Cal.com meeting availability and booking tools
- n8n event and booking workflow integration
- PostgreSQL conversation and agent-run persistence
- Embeddable JavaScript widget
- Token-protected conversation dashboard

## Project Structure

| Path | Purpose |
| --- | --- |
| `api/` | FastAPI application, chat orchestration, tools, retrieval, scoring, and persistence |
| `kb/` | Website extraction, chunking, embedding, and knowledge-base source data |
| `n8n/` | Importable n8n workflow definitions |
| `widget/` | Embeddable website chat widget |
| `dashboard/` | Sales-agent activity dashboard |
| `console/` | Local operator console |
| `schema.sql` | PostgreSQL schema |
| `requirements.txt` | Pinned Python runtime dependencies |

## Prerequisites

- Python 3.11+
- Ollama with a tool-capable model
- PostgreSQL for persistent agent runs
- Optional Cal.com, n8n, and Firecrawl credentials

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local `.env` file and configure integrations such as `OLLAMA_BASE_URL`, `LLM_MODEL_SMALL`, `LLM_MODEL_LARGE`, `DB_URL`, `DASHBOARD_TOKEN`, `ALLOWED_ORIGINS`, and `API_PORT`.

Never commit `.env` or service credentials.

### Booking acknowledgements

Booking is a two-way handshake with n8n. The API fires `cal-action` and waits for
n8n to call back on `POST /api/cal-callback` with what Cal.com actually did; it
never treats the webhook's own HTTP response as the outcome. Two settings govern
it:

| Setting | Where | Purpose |
| --- | --- | --- |
| `N8N_WEBHOOK_SECRET` | this `.env` | Sent outbound as `X-Webhook-Secret`, and required inbound on `/api/cal-callback`. Unset means the callback route refuses to serve rather than defaulting to open |
| `CAL_ACK_TIMEOUT_SECONDS` | this `.env` | How long a booking turn waits for the callback, default `45`. The visitor sees a "Booking your meeting…" status throughout |
| `AGENT_CALLBACK_URL` | **n8n's** environment | Where n8n posts the outcome, e.g. `http://localhost:8002/api/cal-callback`. If n8n runs in Docker, `localhost` is the container — use `http://host.docker.internal:8002/api/cal-callback` |

See `n8n/README.md` §4.1 for why the outcome travels this way rather than on the
webhook response.

## Build the Knowledge Base

```powershell
python kb\chunk.py
python kb\embed.py
```

The generated FAISS index is ignored by Git.

## Run the API

```powershell
uvicorn api.main:app --reload --port 8002
```

Useful endpoints are `GET /api/health`, `POST /api/chat`, `POST /api/end`, and `POST /api/cal-callback`.

## Tests

```powershell
python -m api.test_booking
python -m api.test_scoring
python -m api.test_retrieval
```

Each test module is a plain-assert script with no pytest dependency. Some
integration checks require local or external services and credentials.

## Git Policy

PDF documents and Markdown documents other than files named `README.md` are excluded from this repository. Generated environments, secrets, Python caches, and the built knowledge-base index are also ignored.

