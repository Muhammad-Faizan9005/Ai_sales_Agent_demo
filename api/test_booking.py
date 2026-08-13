"""Booking contract checks.

    python -m api.test_booking

Two things are guarded here, both of which reached a real visitor:

  1. Date parsing. "12 August at 3:30 PM" silently became *today* at 15:30,
     which Cal.com rejected for breaching its minimum booking notice. Every
     phrasing a visitor actually used is pinned below.

  2. The failure contract. book_meeting used to return an instruction telling
     the agent to say "a specialist will confirm {time} by email" after a
     booking had FAILED -- so the visitor believed they had a meeting that did
     not exist. Confirmation language is now gated on a real booking_uid.

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


@contextmanager
def isolated(cal_response: dict, slots: list[str] | None = None):
    """Stub every outbound call for the duration of a test.

    Not optional. An earlier version stubbed only call_cal_action, so running
    the suite wrote real rows to production Supabase (test-ok-0001,
    test-nouid-0001 are still there) and fired real n8n events that reached
    AutoCRM. A test run must not be able to touch live systems.

    Stubs, in order of damage they would otherwise do:
      call_cal_action -> would book on the real Cal.com calendar
      emit_event      -> would drive the n8n fan-out into AutoCRM and Sheets
      persist         -> would write to the production agent_runs table
      cal_slots       -> read-only, but stubbed so tests are deterministic
    """
    async def fake_action(*_a, **_k):
        return cal_response

    async def fake_slots(*_a, **_k):
        return slots if slots is not None else []

    originals = (
        n8n_client.call_cal_action,
        n8n_client.emit_event,
        tools.persist,
        store.persist,
        n8n_client.cal_slots,
        tools.n8n_client.cal_slots,
    )
    n8n_client.call_cal_action = fake_action
    n8n_client.emit_event = lambda *a, **k: None
    tools.persist = lambda *a, **k: True
    store.persist = lambda *a, **k: True
    n8n_client.cal_slots = fake_slots
    tools.n8n_client.cal_slots = fake_slots
    try:
        yield
    finally:
        (
            n8n_client.call_cal_action,
            n8n_client.emit_event,
            tools.persist,
            store.persist,
            n8n_client.cal_slots,
            tools.n8n_client.cal_slots,
        ) = originals


def _next(day: int, month: int) -> datetime:
    """The next occurrence of a day/month, matching parse_desired_time."""
    today = datetime.now(ZoneInfo(TZ)).date()
    year = today.year + (1 if (month, day) < (today.month, today.day) else 0)
    return datetime(year, month, day, tzinfo=ZoneInfo(TZ))


def test_explicit_dates() -> None:
    """The exact phrasings from the transcript that broke this."""
    target = _next(12, 8)
    for text in (
        "12 August at 3:30 PM",
        "12 august at 3 30 pm",   # spaces, not a colon -- the real one
        "Aug 12 at 3:30pm",       # no space before pm
        "12th August 3:30pm",
        "12/08 at 3:30pm",        # day-first
        "330pm on 12 August",
    ):
        got = tools.parse_desired_time(text, TZ)
        assert got is not None, f"{text!r} did not parse"
        local = got.astimezone(ZoneInfo(TZ))
        assert (local.month, local.day) == (8, 12), f"{text!r} -> {local} (wrong date)"
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
    assert tools.parse_desired_time("25 December at 2pm", TZ) is None, "far future accepted"
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


def _qualified(session_id: str) -> Session:
    """A session with all seven PDF-required lead fields."""
    s = Session(session_id=session_id)
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


# The slot the tests ask for, so the stubbed slots list makes it available.
WANTED = "12 August at 3:30 PM"
FREE = ["09:00", "15:30", "16:00"]


def test_requires_contact_details() -> None:
    """book_meeting must refuse until all seven lead fields are known.

    It used to require only an email, so the agent booked before qualification
    finished and AutoCRM received a lead with no phone number -- the rep had no
    way to reach the visitor.
    """
    with isolated({"ok": True, "booking_uid": "bk_x"}, FREE):
        s = Session(session_id="test-gate-0001")
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
    with isolated({"ok": True, "booking_uid": "bk_weekend"}, []):
        s = _qualified("test-weekend-0001")
        out = _run(tools.book_meeting, s, preferred_time="15 August at 3 PM")
        assert out.startswith("WEEKEND_PROPOSAL"), out
        assert "Monday 17 August at 3:00 PM" in out, out
        assert not s.meeting_booked
        assert s.pending_meeting_start

        out = _run(tools.book_meeting, s, preferred_time="yes")
        assert out.startswith("BOOKED"), out
        assert "Monday 17 August at 3:00 PM" in out, out
        assert s.meeting_booked
        assert s.pending_meeting_start is None
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
    print("\nbooking: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
