"""n8n fan-out.

The whole reason n8n is behind an event rather than in the chat path: a CRM
write, a Slack ping and a rep email take seconds, and none of them belong in a
visitor's reply latency. So `emit_event` returns immediately and the HTTP call
runs on a background task.

Two directions:

  emit_event()     -> sales-agent-events   fire-and-forget, never awaited
  call_cal_action() -> cal-action           awaited, because booking needs a
                                            confirmed slot before we can answer

Both webhooks use the same n8n Header Auth credential, sent as X-Webhook-Secret.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx

from api.config import get_settings

SETTINGS = get_settings()
log = logging.getLogger("n8n")

VALID_EVENTS = {
    "lead_created",
    "meeting_booked",
    "proposal_requested",
    "conversation_ended",
}

# Retained so the tasks are not garbage-collected mid-flight; asyncio only
# holds weak references to bare tasks.
_pending: set[asyncio.Task] = set()

# What we actually sent, for the test console. emit_event is fire-and-forget, so
# without this there is no way to see whether an event left at all -- a silent
# no-op and a delivered event look identical from the chat side.
# Bounded: this is a debugging aid on a long-lived process, not storage.
_sent: deque[dict[str, Any]] = deque(maxlen=200)


def recent_events(session_id: str | None = None) -> list[dict[str, Any]]:
    """Events emitted this process, oldest first. For /api/console only."""
    out = list(_sent)
    if session_id:
        out = [e for e in out if e["session_id"] == session_id]
    return out


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if SETTINGS.n8n_webhook_secret:
        headers["X-Webhook-Secret"] = SETTINGS.n8n_webhook_secret
    return headers


async def _post(url: str, payload: dict[str, Any], timeout: float) -> httpx.Response | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(url, json=payload, headers=_headers())
            if res.status_code >= 400:
                log.warning("n8n %s -> %s %s", url, res.status_code, res.text[:200])
            return res
    except Exception as exc:  # network, DNS, timeout -- all non-fatal here
        log.warning("n8n %s unreachable: %s", url, exc)
        return None


def emit_event(event_type: str, session_id: str, payload: dict[str, Any]) -> None:
    """Fire an event at n8n and return immediately.

    Synchronous by signature on purpose -- tool handlers can call it without
    awaiting, which makes it impossible to accidentally put n8n on the critical
    path. If n8n is down the visitor sees no difference.
    """
    if event_type not in VALID_EVENTS:
        raise ValueError(f"unknown event_type {event_type!r}; expected {sorted(VALID_EVENTS)}")

    body = {"event_type": event_type, "session_id": session_id, **payload}

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (scripts, tests): drop rather than block. The agent_runs row
        # is already written, so nothing is lost that matters.
        log.debug("no running loop; skipping emit of %s", event_type)
        return

    record: dict[str, Any] = {
        "event_type": event_type,
        "session_id": session_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "payload": body,
        "status": "pending",
    }
    _sent.append(record)

    task = loop.create_task(_post(SETTINGS.n8n_events_url, body, SETTINGS.n8n_timeout_seconds))
    _pending.add(task)

    def _done(t: asyncio.Task) -> None:
        _pending.discard(t)
        try:
            res = t.result()
        except Exception as exc:  # task itself blew up
            record["status"] = f"error: {exc}"
            return
        # _post swallows transport errors and returns None, so "no response"
        # is the unreachable case rather than an exception.
        record["status"] = "unreachable" if res is None else f"http_{res.status_code}"

    task.add_done_callback(_done)


async def call_cal_action(action: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Book / cancel / reschedule via the cal-action workflow. Awaited.

    Returns {"ok": bool, ...}. Never raises: a booking failure must degrade to
    "I'll have someone confirm by email", not a broken chat turn.
    """
    body = {"action": action, "session_id": session_id, **payload}
    res = await _post(SETTINGS.n8n_cal_action_url, body, SETTINGS.n8n_timeout_seconds)
    if res is None:
        return {"ok": False, "error": "unreachable"}
    if res.status_code >= 400:
        return {"ok": False, "error": f"http_{res.status_code}"}
    try:
        data = res.json()
    except ValueError:
        return {"ok": False, "error": "bad_json"}
    if isinstance(data, list):
        data = data[0] if data else {}
    return {"ok": True, **(data if isinstance(data, dict) else {"data": data})}


async def flush(timeout: float = 5.0) -> None:
    """Await in-flight emits. For shutdown and for tests."""
    if not _pending:
        return
    await asyncio.wait(set(_pending), timeout=timeout)


async def _demo() -> None:
    import time

    # The property that matters: emit_event does not block, even pointed at a
    # black hole.
    SETTINGS.n8n_events_url = "http://127.0.0.1:9/never-listens"
    start = time.perf_counter()
    for i in range(5):
        emit_event("lead_created", f"s-{i}", {"lead_status": "hot"})
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"emit_event blocked for {elapsed:.3f}s"
    print(f"OK  5 emits queued in {elapsed * 1000:.1f}ms (never blocks)")

    await flush(timeout=2)
    print("OK  unreachable n8n degraded quietly")

    try:
        emit_event("not_a_real_event", "s", {})
    except ValueError as exc:
        print(f"OK  rejects bad event_type: {exc}")
    else:
        raise AssertionError("bad event_type was accepted")

    SETTINGS.n8n_cal_action_url = "http://127.0.0.1:9/never-listens"
    result = await call_cal_action("book", "s-1", {"start": "2026-08-12T09:00:00Z"})
    assert result == {"ok": False, "error": "unreachable"}, result
    print("OK  cal action returns ok=False instead of raising")


if __name__ == "__main__":
    asyncio.run(_demo())
