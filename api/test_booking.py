"""Booking contract checks.

    python -m api.test_booking

Three things are guarded here, all of which reached a real visitor:

  1. Date parsing. "12 August at 3:30 PM" silently became *today* at 15:30,
     which Cal.com rejected for breaching its minimum booking notice. Every
     phrasing a visitor actually used is pinned below.

  2. The failure contract. book_meeting used to return an instruction telling
     the agent to say "a specialist will confirm {time} by email" after a
     booking had FAILED -- so the visitor believed they had a meeting that did
     not exist. Confirmation language is now gated on a real booking_uid.

  3. The async handshake. Booking waited on the n8n workflow's own HTTP response
     behind an 8s timeout. Anything slower was reported to the visitor as "that
     time is not available" -- while Cal.com created the booking and emailed
     them the invite. One conversation produced FOUR real meetings the agent
     believed had all failed. The outcome now arrives on /api/cal-callback, an
     unacknowledged booking says so instead of lying, and a pending booking
     blocks a second attempt.

No pytest: one file, plain asserts, runs anywhere.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from api import n8n_client, store, tools
from api.store import Session

TZ = "Asia/Karachi"

# Sessions the stubbed webhook can find, standing in for store.SESSIONS the way
# /api/cal-callback looks one up before applying an acknowledgement.
_LIVE: dict[str, Session] = {}

# Every cal action fired during a test, so a test can prove a second booking was
# never attempted.
_FIRED: list[tuple[str, str]] = []

# Every n8n event emitted, so a test can prove meeting_booked fired exactly once.
_EMITTED: list[tuple[str, str]] = []


@contextmanager
def isolated(
    ack: dict | None,
    slots: list[str] | None = None,
    accept: bool = True,
    delay: float = 0.01,
    ack_timeout: float = 5.0,
):
    """Stub every outbound call, and answer a fired action with `ack`.

    `ack` is the body n8n would POST to /api/cal-callback. `None` means no
    callback ever arrives -- the timeout case. `accept=False` means n8n refused
    the request outright, which is the only case where "not available" is honest.

    Not optional. An earlier version stubbed only the outbound booking call, so
    running the suite wrote real rows to production Supabase (test-ok-0001,
    test-nouid-0001 are still there) and fired real n8n events that reached
    AutoCRM. A test run must not be able to touch live systems.

    Stubs, in order of damage they would otherwise do:
      fire_cal_action -> would book on the real Cal.com calendar
      emit_event      -> would drive the n8n fan-out into AutoCRM and Sheets
      persist         -> would write to the production agent_runs table
      cal_slots       -> read-only, but stubbed so tests are deterministic
    """
    _FIRED.clear()
    _EMITTED.clear()

    async def fake_fire(action: str, session_id: str, payload: dict) -> bool:
        _FIRED.append((action, session_id))
        if not accept:
            return False
        if ack is not None:
            # Delivered from a task, not inline, so the ordering matches
            # production: the turn parks on the event first and the callback
            # wakes it. Inline delivery would never exercise that path.
            async def deliver() -> None:
                await asyncio.sleep(delay)
                session = _LIVE.get(session_id)
                if session is None:
                    return
                tools.apply_booking_ack(session, {"action": action, **ack})
                n8n_client.resolve_ack(session_id)

            asyncio.get_running_loop().create_task(deliver())
        return True

    async def fake_slots(*_a, **_k):
        return slots if slots is not None else []

    def fake_emit(event_type: str, session_id: str, *_a, **_k) -> None:
        _EMITTED.append((event_type, session_id))

    originals = (
        n8n_client.fire_cal_action,
        tools.n8n_client.fire_cal_action,
        n8n_client.emit_event,
        tools.n8n_client.emit_event,
        tools.persist,
        store.persist,
        n8n_client.cal_slots,
        tools.n8n_client.cal_slots,
    )
    previous_timeout = tools.SETTINGS.cal_ack_timeout_seconds
    n8n_client.fire_cal_action = fake_fire
    tools.n8n_client.fire_cal_action = fake_fire
    n8n_client.emit_event = fake_emit
    tools.n8n_client.emit_event = fake_emit
    tools.persist = lambda *a, **k: True
    store.persist = lambda *a, **k: True
    n8n_client.cal_slots = fake_slots
    tools.n8n_client.cal_slots = fake_slots
    tools.SETTINGS.cal_ack_timeout_seconds = ack_timeout
    try:
        yield
    finally:
        (
            n8n_client.fire_cal_action,
            tools.n8n_client.fire_cal_action,
            n8n_client.emit_event,
            tools.n8n_client.emit_event,
            tools.persist,
            store.persist,
            n8n_client.cal_slots,
            tools.n8n_client.cal_slots,
        ) = originals
        tools.SETTINGS.cal_ack_timeout_seconds = previous_timeout
        _LIVE.clear()


def _soon_weekday() -> datetime:
    """A weekday inside the booking horizon, in the meeting timezone.

    The fixtures used to hard-code "12 August", which stopped parsing the moment
    that date passed: _explicit_date rolls a past date to next year and the
    horizon check in parse_desired_time then correctly refuses it. The suite has
    to be runnable on any day, so the dates are computed.
    """
    day = datetime.now(ZoneInfo(TZ)) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=15, minute=30, second=0, microsecond=0)


def _next_saturday() -> datetime:
    now = datetime.now(ZoneInfo(TZ))
    ahead = (5 - now.weekday()) % 7 or 7
    return (now + timedelta(days=ahead)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )


def test_explicit_dates() -> None:
    """The exact phrasings from the transcript that broke this."""
    target = _soon_weekday()
    d, month_full, month_abbr = target.day, target.strftime("%B"), target.strftime("%b")
    for text in (
        f"{d} {month_full} at 3:30 PM",
        f"{d} {month_full.lower()} at 3 30 pm",  # spaces, not a colon -- the real one
        f"{month_abbr} {d} at 3:30pm",           # no space before pm
        f"{d}th {month_full} 3:30pm",
        f"{d:02d}/{target.month:02d} at 3:30pm",  # day-first
        f"330pm on {d} {month_full}",
    ):
        got = tools.parse_desired_time(text, TZ)
        assert got is not None, f"{text!r} did not parse"
        local = got.astimezone(ZoneInfo(TZ))
        assert (local.month, local.day) == (target.month, d), f"{text!r} -> {local}"
        assert (local.hour, local.minute) == (15, 30), f"{text!r} -> {local} (wrong time)"
        assert local.date() == target.date(), f"{text!r} -> {local.date()} not {target.date()}"
    print(f"OK  6 explicit-date phrasings -> {target.date()} 15:30")


def test_relative_dates_still_work() -> None:
    """The explicit-date branch must not shadow the relative one."""
    now = datetime.now(ZoneInfo(TZ))
    got = tools.parse_desired_time("tomorrow at 3pm", TZ).astimezone(ZoneInfo(TZ))
    assert got.date() == (now + timedelta(days=1)).date(), got
    assert (got.hour, got.minute) == (15, 0), got

    got = tools.parse_desired_time("Thursday morning", TZ).astimezone(ZoneInfo(TZ))
    assert got.weekday() == 3, got
    assert got.hour == 10, got
    print("OK  relative dates unaffected")


def test_malformed_never_raises() -> None:
    """Two regex quirks produced hour=30 and crashed a live turn."""
    for text in ("12 August at 99:99", "at 45", "sometime", "", "31 February at 2pm"):
        try:
            tools.parse_desired_time(text, TZ)  # may be None; must not raise
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{text!r} raised {type(exc).__name__}: {exc}") from exc
    print("OK  malformed input returns None instead of raising")


def test_business_hours_and_horizon() -> None:
    got = tools.parse_desired_time("tomorrow at 3am", TZ).astimezone(ZoneInfo(TZ))
    assert got.hour == 3, f"visitor time was silently changed: {got}"
    far = datetime.now(ZoneInfo(TZ)) + timedelta(days=40)
    far_text = f"{far.day} {far.strftime('%B')} at 2pm"
    assert tools.parse_desired_time(far_text, TZ) is None, f"far future accepted: {far_text}"
    print("OK  parser preserves exact time, far-future refused")


def _run(coro_fn, session: Session, **kw):
    """Run one async tool, draining background emits.

    A bare asyncio.run() closes the loop while emit_event's fire-and-forget
    task is still in flight and logs CancelledError -- noise that would hide a
    genuine failure.
    """
    async def go():
        out = await coro_fn(session, **kw)
        await n8n_client.flush(timeout=2)
        return out

    return asyncio.run(go())


def _register(session: Session) -> Session:
    """Make a session findable by the stubbed callback, as SESSIONS would be."""
    _LIVE[session.session_id] = session
    return session


def _qualified(session_id: str) -> Session:
    """A session with all seven PDF-required lead fields."""
    s = _register(Session(session_id=session_id))
    s.fields.update(
        visitor_name="Test Visitor",
        visitor_email="a@b.com",
        visitor_phone="+923001234567",
        company_name="Test Company",
        website_url="https://example.com",
        industry="Retail",
        service_recommended="/seo/shopify-seo",
    )
    return s


# The slot the tests ask for, computed rather than hard-coded so the suite does
# not rot the way "12 August" did. The stubbed slots list makes it available.
_WANT = _soon_weekday()
WANTED = f"{_WANT.day} {_WANT.strftime('%B')} at 3:30 PM"
# A different time on the same day, for proving a second booking is refused.
ANOTHER = f"{_WANT.day} {_WANT.strftime('%B')} at 11 AM"
FREE = ["09:00", "15:30", "16:00"]


def test_requires_contact_details() -> None:
    """book_meeting must refuse until all seven lead fields are known.

    It used to require only an email, so the agent booked before qualification
    finished and AutoCRM received a lead with no phone number -- the rep had no
    way to reach the visitor.
    """
    with isolated({"ok": True, "booking_uid": "bk_x"}, FREE):
        s = _register(Session(session_id="test-gate-0001"))
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("NOT_QUALIFIED"), out
        assert "full name" in out.lower(), out

        s.fields["visitor_email"] = "a@b.com"
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("NOT_QUALIFIED"), out

        s.fields["visitor_name"] = "Test Visitor"
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("NOT_QUALIFIED"), out
        assert not s.meeting_booked, "booked without a phone number"

        s.fields.update(
            visitor_phone="+923001234567",
            company_name="Test Company",
            website_url="https://example.com",
            industry="Retail",
            service_recommended="/seo/shopify-seo",
        )
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("BOOKED"), out
    print("OK  booking gated on all seven required lead fields")


def test_weekend_proposes_same_time_and_waits() -> None:
    saturday = _next_saturday()
    monday = saturday + timedelta(days=2)
    wanted = f"{saturday.day} {saturday.strftime('%B')} at 3 PM"
    expected = f"Monday {monday.day} {monday.strftime('%B')} at 3:00 PM"
    # The next Saturday can be six days out, beyond parse_desired_time's horizon,
    # so the horizon is widened for this check only. The branch under test is the
    # weekend guard, not the horizon -- test_business_hours_and_horizon covers that.
    original_horizon = tools.BOOKING_HORIZON_DAYS
    tools.BOOKING_HORIZON_DAYS = 14
    try:
        with isolated({"ok": True, "booking_uid": "bk_weekend"}, []):
            s = _qualified("test-weekend-0001")
            out = _run(tools.book_meeting, s, preferred_time=wanted)
            assert out.startswith("WEEKEND_PROPOSAL"), out
            assert expected in out, f"{expected!r} not in {out!r}"
            assert not s.meeting_booked
            assert s.pending_meeting_start

            out = _run(tools.book_meeting, s, preferred_time="yes")
            assert out.startswith("BOOKED"), out
            assert expected in out, f"{expected!r} not in {out!r}"
            assert s.meeting_booked
            assert s.pending_meeting_start is None
    finally:
        tools.BOOKING_HORIZON_DAYS = original_horizon
    print("OK  weekend keeps time, proposes Monday, waits for explicit yes")


def test_no_schedule_attempts_exact_weekday() -> None:
    with isolated({"ok": True, "booking_uid": "bk_no_schedule"}, []):
        s = _qualified("test-noschedule-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("BOOKED"), out
    print("OK  no Cal schedule uses exact approved weekday/time")


def test_taken_slot_offers_and_waits() -> None:
    """An unavailable time must offer alternatives, never pick one.

    A visitor asked for 5pm; it was taken; the agent booked 9am the next day
    and announced it as done. Nobody agreed to that.
    """
    with isolated({"ok": True, "booking_uid": "bk_x"}, ["09:00", "16:00", "16:30"]):
        s = _qualified("test-taken-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)  # 15:30 not free
        assert out.startswith("SLOT_TAKEN"), out
        assert "do not book" in out.lower(), out
        assert "04:00 PM" in out or "4:00 PM" in out, f"no alternatives offered: {out}"
        assert not s.meeting_booked, "booked despite the slot being taken"
        assert s.cal_booking_uid is None
    print("OK  taken slot offers alternatives and waits for consent")


def test_failure_never_confirms() -> None:
    """A failed booking must not produce confirmation language.

    The tool result itself used to tell the agent to say a specialist would
    confirm the time.
    """
    with isolated({"ok": False, "error": "http_400"}, FREE):
        s = _qualified("test-fail-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        # The contract is the NOT_BOOKED prefix plus an explicit prohibition.
        # Substring-matching for "confirm" cannot work: the instruction
        # legitimately contains it inside the ban itself.
        assert out.startswith("NOT_BOOKED"), out
        low = out.lower()
        assert "do not say it is booked" in low, out
        assert "none of that is true" in low, out
        assert "say a specialist will confirm" not in low, out
        assert not s.meeting_booked, "meeting_booked set despite failure"
        assert s.cal_booking_uid is None, "uid set despite failure"
    print("OK  failed booking never implies success")


def test_ok_without_uid_is_failure() -> None:
    """ok:true with no booking_uid is not a booking."""
    with isolated({"ok": True, "status": "accepted"}, FREE):  # no uid
        s = _qualified("test-nouid-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("NOT_BOOKED"), out
        assert not s.meeting_booked, "meeting_booked set without a uid"
    print("OK  ok-without-uid treated as failure")


def test_success_sets_state() -> None:
    with isolated({"ok": True, "booking_uid": "bk_test_123", "status": "accepted"}, FREE):
        s = _qualified("test-ok-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("BOOKED"), out
        assert s.meeting_booked is True
        assert s.cal_booking_uid == "bk_test_123"
    print("OK  confirmed booking sets uid and flag")


def test_reschedule_replaces_not_duplicates() -> None:
    """Rescheduling must move the booking and adopt the NEW uid.

    Without a reschedule tool the model had only book_meeting, so "move it to
    5pm" created a SECOND booking and left the first live on the calendar.
    Cal.com's reschedule mints a new uid and cancels the old one.
    """
    with isolated({"ok": True, "booking_uid": "bk_new_456", "status": "accepted"}, FREE):
        s = _qualified("test-resched-0001")
        s.cal_booking_uid = "bk_old_123"
        s.meeting_booked = True
        out = _run(tools.reschedule_meeting, s, preferred_time=WANTED)
        assert out.startswith("RESCHEDULED"), out
        assert s.cal_booking_uid == "bk_new_456", "old uid kept -- next cancel would 400"
        assert s.meeting_booked is True
        assert "reschedule_meeting" in s.actions
        assert "book_meeting" not in s.actions, "reschedule created a second booking"
    print("OK  reschedule replaces the booking and adopts the new uid")


def test_reschedule_without_booking() -> None:
    with isolated({"ok": True, "booking_uid": "bk_x"}, FREE):
        s = _qualified("test-resched-0002")  # no cal_booking_uid
        out = _run(tools.reschedule_meeting, s, preferred_time=WANTED)
        assert out.startswith("ASK"), out
        assert "no booking" in out.lower(), out
    print("OK  reschedule with nothing booked asks instead of booking")


def test_reschedule_taken_slot_keeps_original() -> None:
    """A failed reschedule must leave the existing meeting untouched."""
    with isolated({"ok": True, "booking_uid": "bk_new"}, ["09:00", "16:00"]):
        s = _qualified("test-resched-0003")
        s.cal_booking_uid = "bk_old_123"
        s.meeting_booked = True
        out = _run(tools.reschedule_meeting, s, preferred_time=WANTED)  # 15:30 taken
        assert out.startswith("SLOT_TAKEN"), out
        assert "unchanged" in out.lower(), out
        assert s.cal_booking_uid == "bk_old_123", "original booking was disturbed"
    print("OK  unavailable reschedule leaves the original meeting alone")


def test_cancel_clears_state() -> None:
    with isolated({"ok": True, "status": "cancelled"}, FREE):
        s = _qualified("test-cancel-0001")
        s.cal_booking_uid = "bk_old_123"
        s.meeting_booked = True
        out = _run(tools.cancel_meeting, s)
        assert out.startswith("CANCELLED"), out
        assert s.meeting_booked is False
        assert s.cal_booking_uid is None
    print("OK  cancel clears the booking state")


# --------------------------------------------------------------------------
# The async handshake
# --------------------------------------------------------------------------


def test_unacknowledged_booking_never_says_unavailable() -> None:
    """THE bug. No acknowledgement is not the same as no booking.

    Four real meetings were created on 18-20 August while the agent told the
    visitor each time that the slot was not available: the 8s HTTP timeout was
    being read as a refusal. An unacknowledged booking must claim nothing.
    """
    with isolated(None, FREE, ack_timeout=0.2):  # callback never arrives
        s = _qualified("test-noack-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("BOOKING_UNCONFIRMED"), out
        low = out.lower()
        assert "do not say it is booked" in low, out
        assert "do not say the time is unavailable" in low, out
        assert "still confirming" in low, out
        # Nothing may be claimed either way.
        assert not s.meeting_booked, "an unacknowledged booking was marked booked"
        assert s.cal_booking_uid is None
        # ...but the attempt is remembered, which is what blocks a duplicate.
        assert s.booking_intent["state"] == "pending", s.booking_intent
        assert len(_FIRED) == 1, _FIRED
    print("OK  unacknowledged booking says so instead of claiming unavailable")


def test_pending_booking_blocks_a_second_one() -> None:
    """The guard that would have prevented all four duplicate meetings.

    The uid never arrived, so the ALREADY_BOOKED check could not fire, and every
    new date the visitor named created another real Cal.com booking.
    """
    with isolated(None, FREE, ack_timeout=0.2):
        s = _qualified("test-dupe-0001")
        first = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert first.startswith("BOOKING_UNCONFIRMED"), first

        second = _run(tools.book_meeting, s, preferred_time=ANOTHER)
        assert second.startswith("BOOKING_IN_FLIGHT"), second
        assert "do not start another" in second.lower(), second
        assert len(_FIRED) == 1, f"a second booking was fired: {_FIRED}"
    print("OK  a pending booking blocks a second attempt")


def test_late_acknowledgement_still_lands() -> None:
    """A callback arriving after the turn gave up must still be applied.

    This is what makes the injected prompt line trustworthy: the visitor was
    told "still confirming", so the next turn has to know the truth.
    """
    with isolated(None, FREE, ack_timeout=0.2):
        s = _qualified("test-late-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("BOOKING_UNCONFIRMED"), out
        assert "AWAITING CONFIRMATION" in tools.booking_state_note(s)

        # What /api/cal-callback does when n8n finally reports in.
        changed = tools.apply_booking_ack(
            s, {"action": "book", "ok": True, "booking_uid": "bk_late_1"}
        )
        assert changed is True
        assert s.meeting_booked is True
        assert s.cal_booking_uid == "bk_late_1"
        note = tools.booking_state_note(s)
        assert "CONFIRMED" in note, note
        assert "reschedule_meeting" in note, note
        assert ("meeting_booked", "test-late-0001") in _EMITTED, _EMITTED
    print("OK  a late acknowledgement still applies and reaches the prompt")


def test_acknowledgement_is_idempotent() -> None:
    """n8n retries a failed Notify Agent node three times.

    A repeat must not emit a second meeting_booked event -- the workflow's
    proposal/prep-task branch has no idempotency guard and would create a
    duplicate AutoCRM task.
    """
    with isolated({"ok": True, "booking_uid": "bk_once"}, FREE):
        s = _qualified("test-idem-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("BOOKED"), out
        emitted_once = [e for e in _EMITTED if e[0] == "meeting_booked"]
        assert len(emitted_once) == 1, _EMITTED

        again = tools.apply_booking_ack(
            s, {"action": "book", "ok": True, "booking_uid": "bk_once"}
        )
        assert again is False, "a duplicate ack was applied"
        assert len([e for e in _EMITTED if e[0] == "meeting_booked"]) == 1, _EMITTED
    print("OK  a repeated acknowledgement changes nothing")


def test_rejected_request_is_a_real_failure() -> None:
    """n8n refusing the request outright IS grounds for 'not available'."""
    with isolated({"ok": True, "booking_uid": "bk_x"}, FREE, accept=False):
        s = _qualified("test-reject-0001")
        out = _run(tools.book_meeting, s, preferred_time=WANTED)
        assert out.startswith("NOT_BOOKED"), out
        assert not s.meeting_booked
        assert s.booking_intent["state"] == "failed", s.booking_intent
        # A failed attempt must not lock the visitor out of trying again.
        assert tools._pending_intent(s) is None
    print("OK  a rejected request reports NOT_BOOKED and does not lock")


def test_booking_note_is_silent_when_there_is_nothing() -> None:
    """No booking, no note. The prompt must not carry noise on every turn."""
    s = _qualified("test-note-0001")
    assert tools.booking_state_note(s) == ""
    print("OK  no booking state means no injected line")


def test_lock_expires_so_a_visitor_is_never_stranded() -> None:
    """A lost acknowledgement must not block booking for the whole session."""
    with isolated(None, FREE, ack_timeout=0.2):
        s = _qualified("test-stale-0001")
        _run(tools.book_meeting, s, preferred_time=WANTED)
        assert tools._pending_intent(s) is not None

        stale = datetime.now(ZoneInfo("UTC")) - timedelta(
            minutes=tools.BOOKING_LOCK_MINUTES + 1
        )
        s.booking_intent["fired_at"] = stale.isoformat()
        assert tools._pending_intent(s) is None, "the lock never expires"
        assert "NOT BOOKED" in tools.booking_state_note(s)
    print(f"OK  the booking lock expires after {tools.BOOKING_LOCK_MINUTES} minutes")


def main() -> int:
    test_explicit_dates()
    test_relative_dates_still_work()
    test_malformed_never_raises()
    test_business_hours_and_horizon()
    test_requires_contact_details()
    test_weekend_proposes_same_time_and_waits()
    test_no_schedule_attempts_exact_weekday()
    test_taken_slot_offers_and_waits()
    test_failure_never_confirms()
    test_ok_without_uid_is_failure()
    test_success_sets_state()
    test_reschedule_replaces_not_duplicates()
    test_reschedule_without_booking()
    test_reschedule_taken_slot_keeps_original()
    test_cancel_clears_state()
    test_unacknowledged_booking_never_says_unavailable()
    test_pending_booking_blocks_a_second_one()
    test_late_acknowledgement_still_lands()
    test_acknowledgement_is_idempotent()
    test_rejected_request_is_a_real_failure()
    test_booking_note_is_silent_when_there_is_nothing()
    test_lock_expires_so_a_visitor_is_never_stranded()
    print("\nbooking: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
