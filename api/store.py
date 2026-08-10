"""Conversation state and agent_runs persistence.

Two layers:

  * In-memory sessions -- the live turn loop. Fast, and lost on restart, which
    is fine: a chat that outlives a deploy is not a requirement.
  * agent_runs in Supabase -- the durable record the dashboard and n8n read.

Writes are upserts on session_id so a conversation is one row that fills in as
qualification progresses, matching how the n8n workflows already read it.

Postgres is optional at runtime. If DB_URL is unset or unreachable the chat
still works and only persistence is skipped -- a database blip must not take
the visitor-facing product down with it.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from api.config import get_settings

SETTINGS = get_settings()

# Turns kept in the prompt. The system prompt is ~39k tokens, so history is
# where context is spent; 20 turns is far more than a sales chat needs.
MAX_HISTORY = 20


@dataclass
class Session:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    meeting_booked: bool = False
    proposal_requested: bool = False
    handoff_requested: bool = False
    cal_booking_uid: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Set by chat.end_conversation on widget unload. Until then the row is a
    # live conversation and ended_at stays NULL, which is how the dashboard
    # tells "in progress" from "finished".
    ended_at: datetime | None = None

    def duration_ms(self) -> int:
        """Wall-clock length of the conversation so far."""
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0, int((end - self.created_at).total_seconds() * 1000))

    def add(self, role: str, content: str, **extra: Any) -> None:
        self.messages.append({"role": role, "content": content, **extra})

    def history(self) -> list[dict[str, Any]]:
        """Recent turns, trimmed. Tool plumbing is stripped for the transcript."""
        return self.messages[-MAX_HISTORY * 2 :]

    def transcript_text(self) -> str:
        """Flat transcript for the KB/FAQ pipeline and the CRM note."""
        lines = []
        for m in self.messages:
            if m["role"] in ("user", "assistant") and m.get("content"):
                lines.append(f"{m['role']}: {m['content']}")
        return "\n".join(lines)

    def note_action(self, action: str) -> None:
        """Record a tool call -- this is what makes agent behaviour auditable."""
        self.actions.append(action)

    def user_turns(self) -> int:
        return sum(1 for m in self.messages if m["role"] == "user")


class SessionStore:
    """Thread-safe in-memory session map."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(session_id=session_id)
                self._sessions[session_id] = session
            return session

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


SESSIONS = SessionStore()


# --------------------------------------------------------------------------
# agent_runs persistence
# --------------------------------------------------------------------------

_UPSERT = """
INSERT INTO agent_runs (
    session_id, visitor_name, visitor_email, visitor_phone, company_name,
    website_url, industry, lead_status, service_recommended,
    meeting_booked, proposal_requested, handoff_requested,
    messages_count, transcript, actions_taken, pages_visited,
    cal_booking_uid, ended_at, duration_ms
) VALUES (
    %(session_id)s, %(visitor_name)s, %(visitor_email)s, %(visitor_phone)s,
    %(company_name)s, %(website_url)s, %(industry)s, %(lead_status)s,
    %(service_recommended)s, %(meeting_booked)s, %(proposal_requested)s,
    %(handoff_requested)s, %(messages_count)s, %(transcript)s,
    %(actions_taken)s, %(pages_visited)s, %(cal_booking_uid)s,
    %(ended_at)s, %(duration_ms)s
)
ON CONFLICT (session_id) DO UPDATE SET
    -- COALESCE so a later turn that has not re-extracted a field cannot blank
    -- a value an earlier turn captured.
    visitor_name        = COALESCE(EXCLUDED.visitor_name, agent_runs.visitor_name),
    visitor_email       = COALESCE(EXCLUDED.visitor_email, agent_runs.visitor_email),
    visitor_phone       = COALESCE(EXCLUDED.visitor_phone, agent_runs.visitor_phone),
    company_name        = COALESCE(EXCLUDED.company_name, agent_runs.company_name),
    website_url         = COALESCE(EXCLUDED.website_url, agent_runs.website_url),
    industry            = COALESCE(EXCLUDED.industry, agent_runs.industry),
    lead_status         = COALESCE(EXCLUDED.lead_status, agent_runs.lead_status),
    service_recommended = COALESCE(EXCLUDED.service_recommended, agent_runs.service_recommended),
    meeting_booked      = agent_runs.meeting_booked OR EXCLUDED.meeting_booked,
    proposal_requested  = agent_runs.proposal_requested OR EXCLUDED.proposal_requested,
    handoff_requested   = agent_runs.handoff_requested OR EXCLUDED.handoff_requested,
    messages_count      = EXCLUDED.messages_count,
    transcript          = EXCLUDED.transcript,
    actions_taken       = EXCLUDED.actions_taken,
    pages_visited       = EXCLUDED.pages_visited,
    cal_booking_uid     = COALESCE(EXCLUDED.cal_booking_uid, agent_runs.cal_booking_uid),
    -- COALESCE, not plain assignment: every turn persists, and only the final
    -- one carries an ended_at. Overwriting would blank the end time on any
    -- late write and make a finished conversation look live again.
    ended_at            = COALESCE(EXCLUDED.ended_at, agent_runs.ended_at),
    duration_ms         = EXCLUDED.duration_ms
"""


def _connect():
    if not SETTINGS.db_url:
        return None
    try:
        import psycopg2
    except ImportError:
        return None
    try:
        return psycopg2.connect(SETTINGS.db_url, connect_timeout=8)
    except Exception:
        return None


def persist(session: Session, lead_status: str | None = None) -> bool:
    """Upsert the run. Returns False when persistence was skipped or failed.

    Never raises: the caller is a chat turn, and a failed write must not become
    a failed reply.
    """
    conn = _connect()
    if conn is None:
        return False

    f = session.fields
    params = {
        "session_id": session.session_id,
        "visitor_name": f.get("visitor_name"),
        "visitor_email": f.get("visitor_email"),
        "visitor_phone": f.get("visitor_phone"),
        "company_name": f.get("company_name"),
        "website_url": f.get("website_url"),
        "industry": f.get("industry"),
        "lead_status": lead_status,
        "service_recommended": f.get("service_recommended"),
        "meeting_booked": session.meeting_booked,
        "proposal_requested": session.proposal_requested,
        "handoff_requested": session.handoff_requested,
        "messages_count": len(session.messages),
        "transcript": json.dumps(
            [
                {"role": m["role"], "content": m.get("content", "")}
                for m in session.messages
                if m["role"] in ("user", "assistant")
            ]
        ),
        "actions_taken": json.dumps(session.actions),
        "pages_visited": json.dumps(session.pages),
        "cal_booking_uid": session.cal_booking_uid,
        "ended_at": session.ended_at,
        "duration_ms": session.duration_ms(),
    }
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_UPSERT, params)
        return True
    except Exception:
        return False
    finally:
        conn.close()


def demo() -> None:
    s = SESSIONS.get("demo-1")
    s.add("user", "Hi, I'm Ayesha")
    s.add("assistant", "Hello Ayesha, what does your business do?")
    s.fields["visitor_name"] = "Ayesha"
    s.note_action("save_lead")

    assert SESSIONS.get("demo-1") is s, "session not reused"
    assert s.user_turns() == 1, s.user_turns()
    assert "user: Hi, I'm Ayesha" in s.transcript_text()
    assert s.actions == ["save_lead"]

    long = SESSIONS.get("demo-2")
    for i in range(100):
        long.add("user", f"m{i}")
    assert len(long.history()) <= MAX_HISTORY * 2, len(long.history())

    SESSIONS.drop("demo-1")
    SESSIONS.drop("demo-2")
    print("OK  session store")
    print(f"OK  persist reachable={persist(Session('probe')) }  (False is fine without DB_URL)")


if __name__ == "__main__":
    demo()
