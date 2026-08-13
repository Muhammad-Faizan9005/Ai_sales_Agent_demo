"""The chat turn: SSE out, tool round trip in the middle.

SSE rather than newline-JSON because the widget consumes it with EventSource
semantics and browsers/proxies handle text/event-stream flushing correctly.

Each turn retrieves the relevant KB chunks first and passes them in as a user
message. The system prompt carries only guardrails, persona and the list of
page names -- the page *content* arrives per turn from api/retrieval.py.

Event types on the wire:
    token    incremental assistant text
    tool     a tool ran (widget uses this for a status line)
    handoff  open live chat
    done     turn complete
    error    unrecoverable

    debug    ONLY when the caller asks for it (ChatRequest.debug). Carries
             retrieval hits, tool arguments and results, and the live lead
             score. The test console reads these to prove the machinery ran;
             the widget never sets the flag, so a visitor's stream is
             byte-identical to before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from api import n8n_client, retrieval, tools
from api.kb import system_prompt
from api.llm import MAX_TOOL_ROUNDS, normalise_tool_call, stream_turn
from api.schemas import TOOL_SCHEMAS
from api.scoring import score_lead
from api.store import SESSIONS, Session, persist

log = logging.getLogger("chat")

HANDOFF_MARKER = "__HANDOFF__"


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _retrieval_query(session: Session, message: str) -> str:
    """The current message, prefixed with the previous one.

    "What about the second one?" has no retrievable subject by itself -- it
    embeds to nothing useful and the visitor gets a blank stare. Carrying one
    turn back restores the subject. Only one: reach further and the older topic
    starts outvoting the actual question.
    """
    said = [
        m.get("content", "")
        for m in session.history()
        if m["role"] == "user" and m.get("content")
    ]
    # history() already contains the current message, so the prior one is -2.
    prior = said[-2] if len(said) >= 2 else ""
    return f"{prior}\n{message}" if prior else message


def _build_messages(
    session: Session,
    page_path: str | None,
    kb_context: str = "",
) -> list[dict[str, Any]]:
    """System prompt first and byte-identical every turn, so it stays cacheable."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt()}]

    if page_path:
        # Per-turn context goes in a user-role message, never in the system
        # prompt -- mutating the prefix would defeat prompt caching.
        messages.append(
            {
                "role": "user",
                "content": f"[The visitor is currently on the page {page_path}]",
            }
        )

    if kb_context:
        # Ahead of history, not appended after it: on a tool round the last
        # messages are tool results, and wedging excerpts between a tool_call
        # and its result corrupts the sequence. This position is always valid.
        messages.append({"role": "user", "content": kb_context})

    for message in session.history():
        if message["role"] in ("user", "assistant", "tool"):
            entry = {"role": message["role"], "content": message.get("content", "")}
            if message.get("tool_calls"):
                entry["tool_calls"] = message["tool_calls"]
            if message.get("tool_name"):
                entry["name"] = message["tool_name"]
            messages.append(entry)
    return messages


async def run_turn(
    message: str,
    session_id: str,
    page_path: str | None = None,
    debug: bool = False,
) -> AsyncIterator[str]:
    """Serialize a full turn for one session, including all side effects."""
    session = SESSIONS.get(session_id)
    async with session.turn_lock:
        session.finalised = False
        session.ended_at = None
        session.touch()
        async for frame in _run_turn_locked(message, session, page_path, debug):
            yield frame


async def _run_turn_locked(
    message: str,
    session: Session,
    page_path: str | None = None,
    debug: bool = False,
) -> AsyncIterator[str]:
    """Handle one visitor message while its per-session lock is held."""
    session_id = session.session_id
    session.add("user", message)
    if page_path and page_path not in session.pages:
        session.pages.append(page_path)

    handoff = False
    started = time.perf_counter()
    # Only what n8n emitted *during this turn*: the console needs the delta, and
    # the process-wide log carries every prior turn too.
    events_before = len(n8n_client.recent_events(session_id))

    # Pre-fetched once per turn, not exposed as a tool. As a tool it would cost
    # a full extra round trip to minimax on every question -- the dominant
    # latency here -- and the model would sometimes skip calling it and answer
    # from general knowledge instead. Pre-fetching is one round trip and the
    # context is always present. Off the event loop: encode() is ~20ms of CPU
    # and would otherwise stall every other in-flight turn.
    kb_context = ""
    try:
        query = _retrieval_query(session, message)
        hits = await asyncio.to_thread(retrieval.search, query)
        kb_context = retrieval.as_context(hits)
        log.info("retrieved %d chunks: %s", len(hits), [h.path for h in hits])
        if debug:
            yield _sse(
                "debug",
                {
                    "kind": "retrieval",
                    "query": query,
                    "chars": len(kb_context),
                    "hits": [
                        {"path": h.path, "title": h.title, "score": round(h.score, 4)}
                        for h in hits
                    ],
                },
            )
    except Exception:
        # search() already swallows its own failures; this is belt-and-braces
        # so a retrieval bug can never take down the turn.
        log.exception("retrieval pre-fetch failed")

    try:
        for round_index in range(MAX_TOOL_ROUNDS):
            messages = _build_messages(
                session,
                page_path if round_index == 0 else None,
                kb_context,
            )

            spoken: list[str] = []
            pending_calls: list[dict] = []

            async for delta in stream_turn(messages, tools=TOOL_SCHEMAS):
                if delta.text:
                    spoken.append(delta.text)
                if delta.tool_calls:
                    pending_calls.extend(delta.tool_calls)
                if delta.done:
                    break

            assistant_text = "".join(spoken)

            if not pending_calls:
                if assistant_text:
                    session.add("assistant", assistant_text)
                    # Text is released only after we know this model response
                    # contains no tool call. This prevents unverified claims
                    # such as "booked" reaching the visitor before Cal.com.
                    for text in spoken:
                        yield _sse("token", {"text": text})
                break

            # Record the assistant turn that requested the tools, so the model
            # sees its own call alongside the result on the next round.
            session.add("assistant", assistant_text, tool_calls=pending_calls)

            for call in pending_calls:
                name, args = normalise_tool_call(call)
                result = await tools.dispatch(name, session, args)

                if result.startswith(HANDOFF_MARKER):
                    handoff = True
                    result = result[len(HANDOFF_MARKER) :].strip()

                session.add("tool", result, tool_name=name)
                yield _sse("tool", {"name": name})
                if debug:
                    # Arguments and the raw result string. Tool results are
                    # instructions addressed to the model ("ASK: ...",
                    # "SAVED. ...") and never shown to a visitor, so seeing them
                    # is the only way to tell a tool that ran from one that ran
                    # and refused.
                    yield _sse(
                        "debug",
                        {"kind": "tool", "name": name, "args": args, "result": result},
                    )

            if handoff:
                yield _sse("handoff", {"reason": session.fields.get("handoff_reason", "")})
        else:
            log.warning("session %s hit MAX_TOOL_ROUNDS", session_id)

    except asyncio.CancelledError:
        # Visitor navigated away or closed the tab mid-turn. Persist what we
        # have rather than losing the conversation.
        _finalise(session)
        raise
    except Exception:
        log.exception("turn failed for session %s", session_id)
        yield _sse("error", {"message": "Something went wrong. Please try again."})
        return

    score = _finalise(session)

    if debug:
        # What happened this turn: the n8n events that left, the final score with
        # the reasons behind it, and total latency. The console uses this to
        # prove the contract worked end to end.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        new_events = n8n_client.recent_events(session_id)[events_before:]
        yield _sse(
            "debug",
            {
                "kind": "summary",
                "score": score.as_dict(),
                "fields": {k: v for k, v in session.fields.items() if v},
                "meeting_booked": session.meeting_booked,
                "proposal_requested": session.proposal_requested,
                "handoff_requested": session.handoff_requested,
                "pages_visited": list(session.pages),
                "actions_taken": list(session.actions),
                "n8n_events": [
                    {"event_type": e["event_type"], "status": e["status"]}
                    for e in new_events
                ],
                "elapsed_ms": elapsed_ms,
            },
        )

    yield _sse("done", {"session_id": session_id})


def _finalise(session: Session):
    """Persist the run and keep the lead score current.

    Returns the Score so a caller can report *why* a lead graded as it did --
    scoring is deterministic code, and its reasons are the evidence for that.
    """
    fields = session.fields
    score = score_lead(
        visitor_name=fields.get("visitor_name"),
        visitor_email=fields.get("visitor_email"),
        visitor_phone=fields.get("visitor_phone"),
        company_name=fields.get("company_name"),
        website_url=fields.get("website_url"),
        industry=fields.get("industry"),
        service_recommended=fields.get("service_recommended"),
        meeting_booked=session.meeting_booked,
        proposal_requested=session.proposal_requested,
        messages_count=len(session.messages),
        pages_visited=session.pages,
        transcript_text=session.transcript_text(),
    )
    persist(session, score.status)
    return score


def _end_conversation_locked(session: Session) -> bool:
    """Finalize a session while its turn lock is held. Returns whether it changed."""
    if not session.messages or session.finalised:
        return False
    session.ended_at = datetime.now(timezone.utc)
    session.finalised = True
    _finalise(session)
    n8n_client.emit_event(
        "conversation_ended",
        session.session_id,
        {
            "message_count": len(session.messages),
            "transcript_text": session.transcript_text(),
            "pages_visited": list(session.pages),
            "ended_at": session.ended_at.isoformat().replace("+00:00", "Z"),
            "duration_ms": session.duration_ms(),
        },
    )
    return True


async def end_conversation(session_id: str) -> bool:
    """Explicitly finalize without racing an in-flight turn."""
    session = SESSIONS.get(session_id)
    async with session.turn_lock:
        return _end_conversation_locked(session)


async def finalise_idle_sessions(idle_seconds: int) -> int:
    """Finalize and evict sessions inactive for the configured TTL."""
    now = datetime.now(timezone.utc)
    finalised = 0
    for session in SESSIONS.snapshot():
        if (now - session.last_activity_at).total_seconds() < idle_seconds:
            continue
        if session.turn_lock.locked():
            continue
        async with session.turn_lock:
            if session.messages and not session.finalised:
                finalised += int(_end_conversation_locked(session))
            SESSIONS.drop(session.session_id)
    return finalised
