"""n8n fan-out.

The whole reason n8n is behind an event rather than in the chat path: a CRM
write, a Slack ping and a rep email take seconds, and none of them belong in a
visitor's reply latency. So `emit_event` returns immediately and the HTTP call
runs on a background task.

Two directions out, one back in:

  emit_event()      -> sales-agent-events   fire-and-forget, never awaited
  fire_cal_action() -> cal-action           awaited only until n8n ACCEPTS it;
                                            the booking outcome is not on this
                                            call
  /api/cal-callback <- cal-action           n8n posts the real outcome back, and
                                            resolve_ack() wakes the waiting turn

Both outbound webhooks use the same n8n Header Auth credential, sent as
X-Webhook-Secret; the inbound callback is checked against the same value.
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


async def fire_cal_action(action: str, session_id: str, payload: dict[str, Any]) -> bool:
    """POST a booking action at n8n. Returns whether n8n ACCEPTED the request.

    Deliberately NOT the outcome. cal-booking-actions responds onReceived, so
    this returns in milliseconds and the real result -- uid, status, error --
    arrives separately on /api/cal-callback and is applied by
    tools.apply_booking_ack.

    This used to await the entire workflow with an 8s timeout. The workflow
    responds only after Cal Create Booking, Sync Booking State and Write
    Booking State finish, and each of those retries three times with 5s waits,
    so any retry outran the timeout. httpx raised ReadTimeout, _post swallowed
    it, and the tool reported "that time is not available" to four separate
    visitors while Cal.com was emailing them invites for meetings that did
    exist. Never put the booking outcome back on this call.
    """
    body = {"action": action, "session_id": session_id, **payload}
    record: dict[str, Any] = {
        "event_type": f"cal_action:{action}",
        "session_id": session_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "payload": body,
        "status": "pending",
    }
    _sent.append(record)

    res = await _post(SETTINGS.n8n_cal_action_url, body, SETTINGS.n8n_timeout_seconds)
    if res is None:
        record["status"] = "unreachable"
        return False
    record["status"] = f"http_{res.status_code}"
    return res.status_code < 400


def record_ack(session_id: str, payload: dict[str, Any]) -> None:
    """Log an inbound /api/cal-callback so the test console can see the handshake."""
    _sent.append(
        {
            "event_type": f"cal_ack:{payload.get('action') or 'unknown'}",
            "session_id": session_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
            "status": "ok" if payload.get("ok") else "failed",
        }
    )


# --------------------------------------------------------------------------
# Booking acknowledgements
# --------------------------------------------------------------------------
#
# session_id -> Event. A booking turn registers before it fires and then parks
# on the event; the /api/cal-callback route sets it when n8n reports back.
#
# Keyed by session alone, with no correlation id, because tools.py permits only
# one booking action in flight per session -- see the booking_intent guard. That
# guard is also what stops a second Cal.com booking being created while the
# first is still unconfirmed.
_acks: dict[str, asyncio.Event] = {}


def register_ack(session_id: str) -> asyncio.Event:
    """Claim the ack slot for this session. Call BEFORE firing, never after.

    Registering after the POST loses the race against a fast workflow: the
    callback would find no waiter and the turn would sit out its whole timeout
    for an ack that had already arrived.
    """
    event = asyncio.Event()
    _acks[session_id] = event
    return event


def resolve_ack(session_id: str) -> bool:
    """Wake the turn waiting on this session. False when nobody is waiting.

    False is normal, not an error: it means the turn already timed out and gave
    the visitor a BOOKING_UNCONFIRMED answer. The state has still been applied
    by then, so the next prompt carries the truth.
    """
    event = _acks.get(session_id)
    if event is None:
        return False
    event.set()
    return True


def discard_ack(session_id: str) -> None:
    _acks.pop(session_id, None)


def waiting_for_ack(session_id: str) -> bool:
    return session_id in _acks


async def flush(timeout: float = 5.0) -> None:
    """Await in-flight emits. For shutdown and for tests."""
    if not _pending:
        return
    await asyncio.wait(set(_pending), timeout=timeout)


async def cal_slots(day: str, tz_name: str) -> dict[str, Any]:
    """Return Cal.com's availability state for one day. Never raises.

    Read-only, so it goes straight to Cal.com rather than through n8n --
    cal-booking-actions only handles the three write actions.

    Why this exists: booking blind meant the only signal was a 400 *after* the
    attempt, and the model then invented a different day and announced it. With
    real slots in hand the agent can say "5pm is taken, 4:30 or 5:30 work" and
    wait for an answer.

    `available` means Cal.com returned a non-empty authoritative schedule.
    `no_schedule` means the request succeeded but Cal.com returned no slots;
    this deployment intentionally falls back to local working-day rules.
    `error` means availability could not be checked and must not be guessed.
    """
    if not SETTINGS.cal_api_key or not SETTINGS.cal_username:
        log.warning("cal slots: CAL_API_KEY / CAL_USERNAME unset")
        return {"status": "error", "slots": [], "error": "not_configured"}

    params = {
        "eventTypeSlug": SETTINGS.cal_event_type_slug,
        "username": SETTINGS.cal_username,
        "start": day,
        "end": day,
        "timeZone": tz_name,
    }
    try:
        async with httpx.AsyncClient(timeout=SETTINGS.n8n_timeout_seconds) as client:
            res = await client.get(
                "https://api.cal.com/v2/slots",
                params=params,
                headers={
                    "Authorization": f"Bearer {SETTINGS.cal_api_key}",
                    # Per-endpoint, NOT global. /v2/slots is 2024-09-04 while
                    # /v2/bookings is 2024-08-13; sending the booking version
                    # here returns 404 Cannot GET /v2/slots, which reads like a
                    # bad URL rather than a bad header.
                    "cal-api-version": "2024-09-04",
                },
            )
        if res.status_code != 200:
            log.warning("cal slots %s -> %s %s", day, res.status_code, res.text[:160])
            return {"status": "error", "slots": [], "error": f"http_{res.status_code}"}
        slots = (res.json().get("data") or {}).get(day) or []
        # "2026-08-12T15:30:00.000+05:00" -> "15:30". Cal.com already returns
        # these in the requested timezone.
        starts = [s["start"][11:16] for s in slots if isinstance(s, dict) and s.get("start")]
        return {"status": "available" if starts else "no_schedule", "slots": starts}
    except Exception as exc:
        log.warning("cal slots %s unreachable: %s", day, exc)
        return {"status": "error", "slots": [], "error": type(exc).__name__}


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
    accepted = await fire_cal_action("book", "s-1", {"start": "2026-08-12T09:00:00Z"})
    assert accepted is False, accepted
    print("OK  cal action reports not-accepted instead of raising")

    # The handshake: a turn parks on the event, the callback wakes it. Without
    # this the booking turn would sit out its full 45s timeout every time.
    event = register_ack("s-2")
    assert waiting_for_ack("s-2")
    assert resolve_ack("s-2") is True
    await asyncio.wait_for(event.wait(), timeout=1)
    discard_ack("s-2")
    assert resolve_ack("s-2") is False, "a discarded ack slot still had a waiter"
    print("OK  ack registry wakes a waiting turn, and tolerates a late callback")


if __name__ == "__main__":
    asyncio.run(_demo())
