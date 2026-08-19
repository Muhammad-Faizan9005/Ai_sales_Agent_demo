"""Settings for the AI Sales Agent."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Knowledge base / crawl -------------------------------------------
    firecrawl_api_key: str = Field(
        default="", validation_alias=AliasChoices("FIRECRAWL_API_KEY")
    )
    site_base_url: str = Field(
        default="https://systematicitsolutions.com",
        validation_alias=AliasChoices("SITE_BASE_URL"),
    )

    # ---- LLM ---------------------------------------------------------------
    # Provider is ollama; the model must support tool calling for phase 4.
    llm_provider: str = Field(
        default="ollama", validation_alias=AliasChoices("LLM_PROVIDER")
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        validation_alias=AliasChoices("OLLAMA_BASE_URL"),
    )
    # minimax-m3:cloud is Ollama-hosted, so it needs no local pull. Verified to
    # emit well-formed tool_calls, which the server tools in phase 5 depend on.
    llm_model_small: str = Field(
        default="minimax-m3:cloud", validation_alias=AliasChoices("LLM_MODEL_SMALL")
    )
    llm_model_large: str = Field(
        default="minimax-m3:cloud", validation_alias=AliasChoices("LLM_MODEL_LARGE")
    )
    llm_timeout_seconds: int = Field(
        default=120, validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS")
    )
    llm_num_ctx: int = Field(
        default=16384, validation_alias=AliasChoices("LLM_NUM_CTX")
    )

    # ---- Retrieval (FAISS + local embeddings) ------------------------------
    # The KB no longer rides in the system prompt. Ollama serves minimax-m3
    # with no prompt cache, so all ~38k KB tokens were being paid for on every
    # turn; retrieval cuts that to a few hundred.
    #
    # Local weights, so retrieval survives an Ollama outage -- chat and
    # retrieval now fail independently. 384 dimensions; the model truncates at
    # 256 word pieces (see kb/chunk.py).
    embed_model: str = Field(
        default="all-MiniLM-L6-v2", validation_alias=AliasChoices("EMBED_MODEL")
    )
    # How many chunks ride along on each turn. 6 is ~1.2k tokens against the
    # 38k the whole KB cost.
    retrieval_top_k: int = Field(
        default=6, validation_alias=AliasChoices("RETRIEVAL_TOP_K")
    )
    # Cosine floor. Normalised vectors on IndexFlatIP means the score IS the
    # cosine, so this is directly interpretable: below ~0.25 the chunk is
    # matching on stopwords and only dilutes the context.
    retrieval_min_score: float = Field(
        default=0.25, validation_alias=AliasChoices("RETRIEVAL_MIN_SCORE")
    )
    # Only kb/raw/ is committed -- site_kb.md, sitemap.json and kb/index/ are
    # gitignored build artifacts, so a fresh clone or a move to another machine
    # has the scraped pages and nothing derived from them. Build them on
    # startup instead of serving an ungrounded agent behind a log warning.
    # Set KB_AUTOBUILD=false to require an explicit `python kb/embed.py`.
    kb_autobuild: bool = Field(
        default=True, validation_alias=AliasChoices("KB_AUTOBUILD")
    )

    # ---- Booking (Cal.com) -------------------------------------------------
    cal_api_key: str = Field(default="", validation_alias=AliasChoices("CAL_API_KEY"))
    cal_username: str = Field(default="", validation_alias=AliasChoices("CAL_USERNAME"))
    cal_event_type_slug: str = Field(
        default="30min", validation_alias=AliasChoices("CAL_EVENT_TYPE_SLUG")
    )
    # The timezone the visitor's stated time is interpreted in, and the one the
    # n8n Validate Request node falls back to. Keep both in step.
    meeting_timezone: str = Field(
        default="Asia/Karachi", validation_alias=AliasChoices("MEETING_TIMEZONE")
    )

    # ---- n8n fan-out -------------------------------------------------------
    # emit_event is fire-and-forget: a chat turn never waits on n8n.
    # Both webhooks are guarded by the same n8n Header Auth credential.
    n8n_events_url: str = Field(
        default="http://localhost:5678/webhook/sales-agent",
        validation_alias=AliasChoices("N8N_EVENTS_URL", "N8N_WEBHOOK_URL"),
    )
    n8n_cal_action_url: str = Field(
        default="http://localhost:5678/webhook/cal-action",
        validation_alias=AliasChoices("N8N_CAL_ACTION_URL"),
    )
    n8n_webhook_secret: str = Field(
        default="", validation_alias=AliasChoices("N8N_WEBHOOK_SECRET")
    )
    # Covers only the *fire* POST, which cal-booking-actions answers immediately
    # (responseMode onReceived). It is NOT how long we wait for a booking: the
    # outcome arrives separately on /api/cal-callback. Awaiting the workflow's
    # own response is what produced four real meetings the agent believed had
    # failed -- see cal_ack_timeout_seconds.
    n8n_timeout_seconds: float = Field(
        default=8.0, validation_alias=AliasChoices("N8N_TIMEOUT_SECONDS")
    )
    # How long a booking turn waits for n8n to call back with the real outcome.
    # Generous on purpose: the visitor sees a "Booking your meeting..." status
    # for the whole wait, and the alternative -- giving up early -- is what told
    # visitors their slot was taken while Cal.com was emailing them an invite.
    # Must exceed the workflow's realistic worst case (Cal.com create plus the
    # Notify Agent hop), or bookings land as BOOKING_UNCONFIRMED needlessly.
    cal_ack_timeout_seconds: float = Field(
        default=45.0, validation_alias=AliasChoices("CAL_ACK_TIMEOUT_SECONDS")
    )

    # ---- Supabase (agent_runs) ---------------------------------------------
    # Direct Postgres, not PostgREST: the API writes its own rows, and RLS is
    # deny-all so an anon key would see nothing anyway.
    db_url: str = Field(default="", validation_alias=AliasChoices("DB_URL"))

    # ---- Admin dashboard ---------------------------------------------------
    # The dashboard exposes names, emails, phone numbers and full transcripts,
    # so it cannot be open. One shared token, not a login system -- but unset
    # means the routes refuse to serve rather than defaulting to open.
    dashboard_token: str = Field(
        default="", validation_alias=AliasChoices("DASHBOARD_TOKEN")
    )

    # ---- Widget / CORS -----------------------------------------------------
    # The API listens on 8002; the mock site on 8080. These are the origins the
    # widget is served FROM, not the API's own port.
    allowed_origins: str = Field(
        default="http://localhost:8080,http://127.0.0.1:8080",
        validation_alias=AliasChoices("ALLOWED_ORIGINS"),
    )
    # Where uvicorn binds. Kept here so main.py's docstring, the widget default
    # and the run command cannot drift apart again.
    api_port: int = Field(default=8002, validation_alias=AliasChoices("API_PORT"))
    session_idle_minutes: int = Field(
        default=30, validation_alias=AliasChoices("SESSION_IDLE_MINUTES")
    )

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    # ---- Paths -------------------------------------------------------------
    @property
    def kb_dir(self) -> Path:
        return PROJECT_ROOT / "kb"

    @property
    def kb_file(self) -> Path:
        return self.kb_dir / "site_kb.md"

    @property
    def sitemap_file(self) -> Path:
        return self.kb_dir / "sitemap.json"

    @property
    def raw_dir(self) -> Path:
        return self.kb_dir / "raw"

    # kb/index/ is a build artifact, not source: rebuilt by kb/embed.py from
    # site_kb.md, gitignored, never committed.
    @property
    def index_dir(self) -> Path:
        return self.kb_dir / "index"

    @property
    def faiss_file(self) -> Path:
        return self.index_dir / "site.faiss"

    @property
    def chunks_file(self) -> Path:
        return self.index_dir / "chunks.jsonl"

    @property
    def mocksite_dir(self) -> Path:
        return PROJECT_ROOT / "mocksite"

    @property
    def dashboard_dir(self) -> Path:
        return PROJECT_ROOT / "dashboard"

    @property
    def console_dir(self) -> Path:
        return PROJECT_ROOT / "console"


@lru_cache
def get_settings() -> Settings:
    return Settings()
