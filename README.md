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

Useful endpoints are `GET /api/health`, `POST /api/chat`, and `POST /api/end`.

## Tests

```powershell
pytest api
```

Some integration tests require local or external services and credentials.

## Git Policy

PDF documents and Markdown documents other than files named `README.md` are excluded from this repository. Generated environments, secrets, Python caches, and the built knowledge-base index are also ignored.

