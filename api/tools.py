"""Server-side tools the model can call.

Every tool follows the same shape: validate, mutate session state, persist,
fan out to n8n, return a short string for the model to speak from. The return
value is model-facing, so it says what happened rather than dumping JSON.

Nothing here raises. A tool that throws would abort the turn and lose the
visitor's reply; a tool that fails should hand the model a graceful line
instead.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api import n8n_client
from api.config import get_settings
from api.kb import allowed_paths
from api.scoring import score_lead
from api.store import Session, persist

SETTINGS = get_settings()

log = logging.getLogger("tools")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
# Deliberately permissive: international formats vary and rejecting a real
# number costs a lead, while a bad one only costs a rep one call.
PHONE_RE = re.compile(r"^[+\d][\d\s\-().]{6,}$")

REQUIRED_QUALIFICATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("visitor_name", "full name"),
    ("company_name", "company name"),
    ("visitor_email", "email address"),
    ("visitor_phone", "phone number"),
    ("website_url", "website URL, or confirmation that they have no website"),
    ("industry", "business industry"),
    ("service_recommended", "required service"),
)


def _missing_qualification_fields(session: Session) -> list[tuple[str, str]]:
    return [
        (key, label)
        for key, label in REQUIRED_QUALIFICATION_FIELDS
        if not session.fields.get(key)
    ]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _lead_status(session: Session) -> str:
    f = session.fields
    return score_lead(
        visitor_name=f.get("visitor_name"),
        visitor_email=f.get("visitor_email"),
        visitor_phone=f.get("visitor_phone"),
        company_name=f.get("company_name"),
        website_url=f.get("website_url"),
        industry=f.get("industry"),
        service_recommended=f.get("service_recommended"),
        meeting_booked=session.meeting_booked,
        proposal_requested=session.proposal_requested,
        messages_count=len(session.messages),
        pages_visited=session.pages,
        transcript_text=session.transcript_text(),
    ).status


def _lead_payload(session: Session, status: str) -> dict[str, Any]:
    """The exact contract sales-agent-events expects. Keys must not drift.

    The workflow reads body.score and body.fields.* (see the Upsert AutoCRM Lead
    node), so the visitor details are nested under "fields" and the lead grade is
    "score", not "lead_status". A flat payload silently produces a lead with
    every column undefined, which is how this was wrong the first time.
    """
    f = session.fields
    return {
        "score": status,
        "fields": {
            "visitor_name": f.get("visitor_name"),
            "visitor_email": f.get("visitor_email"),
            "visitor_phone": f.get("visitor_phone"),
            "company_name": f.get("company_name"),
            "website_url": f.get("website_url"),
            "industry": f.get("industry"),
            "service_recommended": f.get("service_recommended"),
        },
        "transcript_text": session.transcript_text(),
        "pages_visited": list(session.pages),
        "actions_taken": list(session.actions),
        "message_count": len(session.messages),
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def save_lead(session: Session, **kwargs: Any) -> str:
    """Capture or update qualification details.

    Called repeatedly as details arrive, so it always persists what it has --
    a half-filled lead a rep can chase beats a perfect one we never wrote.
    The return value names the next missing field, because the previous version
    fell silent after name+email and the agent stopped asking for phone or
    company entirely.
    """
    email = _clean(kwargs.get("visitor_email"))
    phone = _clean(kwargs.get("visitor_phone"))

    if email and not EMAIL_RE.match(email):
        return "ASK: that email looks malformed. Ask them to confirm it."
    if phone and not PHONE_RE.match(phone):
        return "ASK: that phone number looks malformed. Ask them to confirm it."

    for key in (
        "visitor_name",
        "visitor_email",
        "visitor_phone",
        "company_name",
        "website_url",
        "industry",
        "service_recommended",
    ):
        value = _clean(kwargs.get(key))
        if value:
            session.fields[key] = value

    f = session.fields
    session.note_action("save_lead")
    status = _lead_status(session)
    persist(session, status)
    missing = _missing_qualification_fields(session)
    if missing:
        return (
            "PARTIAL_LEAD_SAVED. Qualification is incomplete. Still missing: "
            + ", ".join(label for _, label in missing)
            + f". Ask only for {missing[0][1]} next, naturally and with a reason. "
            "Do not offer a meeting, callback or proposal yet."
        )

    n8n_client.emit_event("lead_created", session.session_id, _lead_payload(session, status))
    return (
        "QUALIFIED. All seven required fields are saved. You may now offer the "
        "appropriate next step, including a meeting or callback."
    )


BUSINESS_START_HOUR = 9   # local, inclusive
BUSINESS_END_HOUR = 17    # local, exclusive -- last start is 16:xx
BOOKING_HORIZON_DAYS = 3  # "within the next 3 days"

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))  # longest first: 'sept' before 'sep'

# "12 August", "12th Aug"
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\b")
# "August 12", "Aug 12th"
_MONTH_DAY = re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
# "12/08", "12-8". DAY-FIRST: the audience is PK/UK, where 12/08 is 12 August.
# A US visitor typing 08/12 for December would be read as 8 August -- accepted,
# because the horizon check below rejects anything more than ~5 days out, so the
# wrong reading is refused rather than silently booked.
_NUMERIC = re.compile(r"\b(\d{1,2})[/-](\d{1,2})\b")


def _explicit_date(raw: str, today):
    """A calendar date the visitor typed, or None.

    Without this "12 August at 3:30 PM" matched no branch, `day` stayed None and
    fell back to *today* -- the agent then booked today at 15:30, Cal.com
    refused it for breaching minimum notice, and the visitor was told a
    specialist would confirm a meeting that did not exist.
    """
    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md"), (_NUMERIC, "dm")):
        m = pattern.search(raw)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        if order == "dm":
            day_num, month = int(a), (MONTHS.get(b) if b in MONTHS else int(b))
        else:
            day_num, month = int(b), MONTHS[a]
        if not (1 <= month <= 12 and 1 <= day_num <= 31):
            return None
        # No year given: assume the next occurrence, so "5 January" in December
        # books next year rather than eleven months ago.
        year = today.year
        try:
            candidate = date(year, month, day_num)
        except ValueError:
            return None  # 31 February and friends
        if candidate < today:
            try:
                candidate = date(year + 1, month, day_num)
            except ValueError:
                return None
        return candidate
    return None


def parse_desired_time(text: str, tz_name: str) -> datetime | None:
    """Turn what the visitor actually typed into a concrete UTC start.

    We do not show a slot picker: the visitor says "tomorrow at 3" or "Tuesday
    morning" and we book that. Anything we cannot pin down returns None and the
    agent asks once more rather than guessing a time nobody agreed to.
    """
    if not text:
        return None
    raw = text.lower().strip()
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    day = None
    if "today" in raw or "tonight" in raw:
        day = now.date()
    elif "day after tomorrow" in raw:
        day = now.date() + timedelta(days=2)
    elif "tomorrow" in raw or "tomorow" in raw:
        day = now.date() + timedelta(days=1)
    else:
        # Explicit calendar date first: "12 August" is more specific than any
        # weekday name that might also appear in the sentence.
        day = _explicit_date(raw, now.date())
        if day is None:
            for i, name in enumerate(
                ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            ):
                if name in raw:
                    ahead = (i - now.weekday()) % 7 or 7
                    day = now.date() + timedelta(days=ahead)
                    break

    hour = minute = None
    # Separator is [:.\s]? so "3:30pm", "3.30 pm", "3 30 pm" and "330pm" all
    # parse. Two real failures came from this one regex:
    #   "3:30pm" (no space) missed entirely -> fell through to the bare-number
    #     branch and raised ValueError: hour must be in 0..23.
    #   "3 30 pm" matched the *minutes* as the hour ("30 pm" -> hour=30) and
    #     raised the same way. That exact string came from a real visitor.
    m = re.search(r"\b(\d{1,2})(?:[:.\s]?(\d{2}))?\s*(am|pm)\b", raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        if m.group(3) == "am" and hour == 12:
            hour = 0
    elif (m := re.search(r"\b(?:at|around)\s+(\d{1,2})(?::(\d{2}))?\b", raw)):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        # Bare "at 3" in a business context means the afternoon.
        if hour < BUSINESS_START_HOUR:
            hour += 12
    elif "morning" in raw:
        hour, minute = 10, 0
    elif "afternoon" in raw:
        hour, minute = 14, 0
    elif "evening" in raw:
        hour, minute = 16, 0

    if day is None and hour is None:
        return None
    if day is None:
        day = now.date()
    if hour is None:
        hour, minute = 10, 0

    # Last line of defence. Two separate regex quirks have produced hour=30 and
    # crashed a live turn with ValueError; returning None instead makes the
    # agent ask again, which is always better than a 500 mid-conversation.
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.warning("unparseable time in %r -> hour=%s minute=%s", text, hour, minute)
        return None

    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)

    # Parsing is interpretation only. Never silently move a visitor's date or
    # time; validation and any proposed correction happen in book_meeting.
    if local > now + timedelta(days=BOOKING_HORIZON_DAYS + 2):
        return None  # too far out to be what "next few days" meant
    return local.astimezone(timezone.utc)


async def _slot_check(start: datetime, tz_name: str) -> tuple[str, list[str]]:
    """Return available | unavailable | no_schedule | error plus free times."""
    local = start.astimezone(ZoneInfo(tz_name))
    result = await n8n_client.cal_slots(local.date().isoformat(), tz_name)
    # Compatibility with older isolated tests/mocks while callers migrate.
    if isinstance(result, list):
        result = {"status": "available" if result else "no_schedule", "slots": result}
    status = str(result.get("status") or "error")
    free = list(result.get("slots") or [])
    if status != "available":
        return status, free
    return ("available" if local.strftime("%H:%M") in free else "unavailable"), free


def _next_working_day(local: datetime) -> datetime:
    proposed = local
    while proposed.weekday() >= 5:
        proposed += timedelta(days=1)
    return proposed


def _human_time(local: datetime) -> str:
    return local.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")


def _is_confirmation(text: str) -> bool:
    value = re.sub(r"[^a-z0-9\s]", "", (text or "").lower()).strip()
    return value in {
        "yes", "yes please", "okay", "ok", "that works", "works for me",
        "book it", "schedule it", "confirm", "confirmed", "sure", "go ahead",
    }


def _offer(free: list[str], around: str, limit: int = 3) -> str:
    """The free times nearest the one asked for, as '4:30 PM, 5:30 PM'."""
    def mins(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    target = mins(around)
    nearest = sorted(free, key=lambda s: abs(mins(s) - target))[:limit]
    return ", ".join(
        datetime.strptime(s, "%H:%M").strftime("%I:%M %p").lstrip("0")
        for s in sorted(nearest, key=mins)
    )


# --------------------------------------------------------------------------
# The booking handshake
# --------------------------------------------------------------------------
#
# Booking is asynchronous. We fire the cal-action webhook, n8n books with
# Cal.com, and n8n POSTs the outcome back to /api/cal-callback, which applies it
# and wakes the waiting turn. The turn does wait -- the visitor is sitting there
# expecting a yes or no -- but it waits on the *callback*, not on the workflow's
# own HTTP response.
#
# The distinction is the whole point. Awaiting the workflow response meant an 8s
# httpx timeout decided whether a booking had happened, and every run slower
# than that was reported to the visitor as "that time is not available" while
# Cal.com emailed them a real invite. Four meetings were created that way in one
# conversation, none of which the agent knew about.

# How long a pending intent keeps the door shut. Long enough that a slow-but-
# working n8n cannot produce a duplicate; short enough that a visitor whose ack
# was lost outright is not locked out of booking for the rest of the session.
BOOKING_LOCK_MINUTES = 10


def _iso_z(value: str | None) -> str | None:
    """Normalise an ISO timestamp to the trailing-Z form n8n and Cal.com use."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _human_from_iso(value: str | None) -> str | None:
    iso = _iso_z(value)
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _human_time(parsed.astimezone(ZoneInfo(SETTINGS.meeting_timezone)))


def _pending_intent(session: Session) -> dict[str, Any] | None:
    """The booking still awaiting an acknowledgement, if there is one.

    A `pending` intent is a lock: while one exists no second Cal.com booking may
    be fired for this session. It expires after BOOKING_LOCK_MINUTES so a
    genuinely lost acknowledgement does not strand the visitor.
    """
    intent = session.booking_intent
    if not intent or intent.get("state") != "pending":
        return None
    fired_at = intent.get("fired_at")
    try:
        fired = datetime.fromisoformat(str(fired_at))
    except (TypeError, ValueError):
        return intent
    if fired.tzinfo is None:
        fired = fired.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fired > timedelta(minutes=BOOKING_LOCK_MINUTES):
        intent["state"] = "stale"
        return None
    return intent


async def _fire_and_wait(session: Session, action: str, payload: dict[str, Any]) -> str:
    """Fire a cal action, then wait for n8n's callback.

    Returns "acked" | "rejected" | "timeout". It deliberately reports nothing
    about the booking itself: the callback route has already applied the outcome
    to the session by the time this returns "acked", and the caller reads it from
    there. One writer, so a retried or late callback cannot race the turn.
    """
    event = n8n_client.register_ack(session.session_id)
    try:
        accepted = await n8n_client.fire_cal_action(action, session.session_id, payload)
        if not accepted:
            return "rejected"
        await asyncio.wait_for(event.wait(), timeout=SETTINGS.cal_ack_timeout_seconds)
        return "acked"
    except asyncio.TimeoutError:
        log.warning(
            "no cal ack for %s after %.0fs (%s)",
            session.session_id,
            SETTINGS.cal_ack_timeout_seconds,
            action,
        )
        return "timeout"
    finally:
        # Always release the slot. A callback arriving after this point finds no
        # waiter, which is fine -- apply_booking_ack still runs, so the next
        # turn's prompt carries the real state.
        n8n_client.discard_ack(session.session_id)


def apply_booking_ack(session: Session, payload: dict[str, Any]) -> bool:
    """Apply n8n's booking outcome. The ONLY writer of booking state.

    Called from /api/cal-callback and from nowhere else; the tool that fired the
    action only reads what this wrote. Returns whether anything changed.

    Idempotent, because n8n retries a failed Notify Agent node up to three times.
    Re-applying a uid we already hold must not emit a second meeting_booked
    event -- the proposal branch has no idempotency guard and would create a
    duplicate AutoCRM prep task.
    """
    action = str(payload.get("action") or "book").lower()
    ok = bool(payload.get("ok"))
    uid = _clean(payload.get("booking_uid")) or _clean(payload.get("uid"))
    intent = session.booking_intent or {}

    # ok-with-no-uid is not a booking. That rule predates the callback and still
    # holds: a response can be well-formed and carry no booking at all.
    if not ok or (action != "cancel" and not uid):
        session.booking_error = (
            _clean(payload.get("error")) or "no booking_uid returned"
        )
        if intent:
            intent["state"] = "failed"
        log.warning("booking ack failed for %s: %s", session.session_id, payload)
        persist(session, _lead_status(session))
        return True

    if action == "cancel":
        if not session.meeting_booked and session.cal_booking_uid is None:
            return False  # already applied
        session.meeting_booked = False
        session.cal_booking_uid = None
        session.booking_error = None
        if intent:
            intent["state"] = "confirmed"
        persist(session, _lead_status(session))
        return True

    if session.cal_booking_uid == uid and session.meeting_booked:
        return False  # duplicate ack for a booking we already hold

    start_iso = _iso_z(_clean(payload.get("start"))) or intent.get("start_iso")
    human = _human_from_iso(start_iso) or intent.get("human") or "your meeting"

    session.meeting_booked = True
    session.cal_booking_uid = uid
    session.pending_meeting_start = None
    session.pending_meeting_label = None
    session.booking_error = None
    if intent:
        intent.update(state="confirmed", uid=uid, start_iso=start_iso, human=human)
    else:
        # A booking we never fired -- a stray or replayed callback. Record it
        # anyway: an unknown real meeting is exactly what we lost before.
        session.booking_intent = {
            "action": action,
            "start_iso": start_iso,
            "human": human,
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "state": "confirmed",
            "uid": uid,
        }

    status = _lead_status(session)
    persist(session, status)
    n8n_client.emit_event(
        "meeting_booked",
        session.session_id,
        {
            **_lead_payload(session, status),
            # NESTED under "meeting" because that is what the workflow reads.
            # sales-agent-events -> Meeting Details does `b.meeting?.start_time`
            # and THROWS when it is absent, routing the whole branch to Record
            # Failure: no lead status update, no prep task, no rep email, no
            # visitor confirmation. A flat meeting_time key silently killed
            # every booking. See n8n/docs_n8n/contracts.md s2.3.
            "meeting": {
                "start_time": start_iso,
                "uid": uid,
                "meeting_url": _clean(payload.get("meeting_url")),
                "status": _clean(payload.get("status")) or "accepted",
            },
            # Kept flat alongside for the humans reading n8n's execution log --
            # the workflow derives its own localised string from start_time.
            "meeting_time_local": human,
            **(
                {"rescheduled_from_uid": intent.get("from_uid")}
                if intent.get("from_uid")
                else {}
            ),
        },
    )
    return True


def booking_state_note(session: Session) -> str:
    """The booking line injected into every prompt, or "" when there is nothing.

    This is what closes the loop on an asynchronous booking. The ack may land
    after the turn that fired it has already answered, so the model has to be
    told the current state on every subsequent call rather than inferring it
    from a tool result it saw once.
    """
    intent = session.booking_intent or {}
    human = intent.get("human") or session.pending_meeting_label

    if session.meeting_booked and session.cal_booking_uid:
        when = human or "an agreed time"
        return (
            f"[Booking status: CONFIRMED for {when}. This meeting is real and on "
            "the calendar. Do not book again and do not offer to book -- to move "
            "it use reschedule_meeting, to cancel it use cancel_meeting.]"
        )

    if _pending_intent(session):
        when = human or "the time they asked for"
        return (
            f"[Booking status: AWAITING CONFIRMATION for {when}. Do not say it is "
            "booked and do not say the time is unavailable -- neither is known "
            "yet. Do not start another booking. If they ask, say you are still "
            "confirming it and the invite will arrive by email.]"
        )

    if intent.get("state") in ("failed", "stale"):
        when = human or "the time they asked for"
        reason = session.booking_error or "it could not be confirmed"
        return (
            f"[Booking status: NOT BOOKED. The attempt for {when} did not go "
            f"through ({reason}). Nothing is on the calendar. Do not claim "
            "otherwise. You may ask for another time within the next 3 working "
            "days, 9am-5pm.]"
        )

    return ""


async def book_meeting(session: Session, **kwargs: Any) -> str:
    """Book the call at the time the visitor asked for.

    Four guards, each from a defect that reached a real visitor:

      * Contact details first. This used to require only an email, so the agent
        booked before qualification finished and AutoCRM got a lead with no
        phone number -- the rep could not call them.
      * The slot must actually be free. Booking blind meant the only feedback
        was a 400 after the fact, and the model then invented a different day
        and announced it as booked.
      * The visitor must have named the time. The agent may never move someone
        to a slot they did not say aloud.
      * One booking in flight at a time. The outcome now arrives asynchronously,
        so without this a visitor naming a second date before the first ack
        lands fires a second Cal.com booking. One conversation produced four
        real meetings that way.
    """
    tz_name = SETTINGS.meeting_timezone
    if session.cal_booking_uid:
        return (
            "ALREADY_BOOKED: this visitor already has a meeting. Do not create "
            "another one. If they requested a different time, use "
            "reschedule_meeting; otherwise confirm the existing booking."
        )
    in_flight = _pending_intent(session)
    if in_flight:
        return (
            f"BOOKING_IN_FLIGHT: a booking for {in_flight.get('human') or 'this visitor'} "
            "was already sent and is still being confirmed. Do NOT start another "
            "one. Tell them it is still being confirmed and the calendar invite "
            "will arrive by email, then stop. Do not offer a different time."
        )
    missing = _missing_qualification_fields(session)
    if missing:
        return (
            "NOT_QUALIFIED: do not discuss times or attempt a booking. Missing: "
            + ", ".join(label for _, label in missing)
            + ". Ask for only the first missing field next."
        )
    name = _clean(kwargs.get("visitor_name")) or session.fields.get("visitor_name")
    email = _clean(kwargs.get("visitor_email")) or session.fields.get("visitor_email")
    phone = _clean(kwargs.get("visitor_phone")) or session.fields.get("visitor_phone")
    wanted = _clean(kwargs.get("preferred_time"))

    # Persist whatever arrived before any refusal below, so a blocked booking
    # still improves the lead rather than throwing the details away.
    if name:
        session.fields["visitor_name"] = name
    if email and EMAIL_RE.match(email):
        session.fields["visitor_email"] = email
    if phone and PHONE_RE.match(phone):
        session.fields["visitor_phone"] = phone

    if not email:
        return "ASK: no email yet. Ask for their email so the invite can be sent."
    if not EMAIL_RE.match(email):
        return "ASK: that email looks malformed. Ask them to confirm it."
    if not name:
        return "ASK: no name yet. Ask what to put on the invite."
    # A meeting with no reachable number is a meeting the rep cannot rescue if
    # the visitor does not show. Blocking here is what stops the agent racing
    # to book on an email alone.
    if not phone:
        return (
            "ASK: no phone number yet. Ask for one so the specialist can call "
            "if anything changes -- then book. Do not book without it."
        )
    if not wanted:
        return (
            "ASK: no time given. Ask which day and rough time suits them in the "
            "next 3 working days, e.g. 'tomorrow afternoon' or 'Thursday at 11'."
        )

    # "Yes" confirms only the exact server-proposed slot retained in session.
    if session.pending_meeting_start and _is_confirmation(wanted):
        try:
            start = datetime.fromisoformat(session.pending_meeting_start)
        except ValueError:
            session.pending_meeting_start = None
            session.pending_meeting_label = None
            return "ASK: the proposed time expired. Ask them for a weekday and time again."
    else:
        start = parse_desired_time(wanted, tz_name)
    if start is None:
        return (
            f"ASK: could not pin down {wanted!r} to a real slot. Ask for a day "
            "plus a rough time within the next 3 working days."
        )

    local = start.astimezone(ZoneInfo(tz_name))
    now = datetime.now(ZoneInfo(tz_name))
    human = _human_time(local)

    if local <= now + timedelta(minutes=15):
        return f"PAST_TIME: {human} has already passed or is too close. Ask for another weekday and time."
    if local.hour < BUSINESS_START_HOUR or local.hour >= BUSINESS_END_HOUR:
        return (
            f"OUTSIDE_HOURS: {human} is outside meeting hours, which are "
            f"{BUSINESS_START_HOUR}:00 AM to {BUSINESS_END_HOUR}:00 PM, Monday to Friday. "
            "Ask for a time within those hours; do not change it yourself."
        )
    if local.weekday() >= 5:
        proposed = _next_working_day(local)
        proposed_human = _human_time(proposed)
        session.pending_meeting_start = proposed.isoformat()
        session.pending_meeting_label = proposed_human
        persist(session, _lead_status(session))
        return (
            f"WEEKEND_PROPOSAL: {human} falls on a weekend, when meetings are not "
            f"available. The nearest working day at the SAME requested time is "
            f"{proposed_human}. Ask whether they want that exact slot. Do not book "
            "anything until they explicitly confirm."
        )

    availability, free = await _slot_check(start, tz_name)
    if availability == "error":
        return (
            f"AVAILABILITY_UNKNOWN: could not verify {human}. Nothing has been "
            "booked. Tell them availability could not be confirmed and ask to try again."
        )
    if availability == "unavailable":
        session.fields["requested_meeting_time"] = human
        persist(session, _lead_status(session))
        alternatives = _offer(free, local.strftime("%H:%M"))
        # Offer and STOP. The agent must not pick one of these itself: a
        # visitor who asked for 5pm was silently moved to 9am the next day and
        # told it was booked.
        return (
            f"SLOT_TAKEN: {human} is not available. Tell them that time is "
            f"taken and offer these on the same day: {alternatives}. "
            "Ask which they prefer, or ask for another day. Do NOT book any of "
            "them until they choose one. Do not mention systems or calendars."
        )
    # `no_schedule` is intentional in this deployment: weekday/business-hour
    # rules permit an exact booking attempt, whose UID remains the authority.

    start_iso = start.isoformat().replace("+00:00", "Z")
    session.booking_intent = {
        "action": "book",
        "start_iso": start_iso,
        "human": human,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "state": "pending",
    }
    session.booking_error = None
    session.note_action("book_meeting")
    persist(session, _lead_status(session))

    outcome = await _fire_and_wait(
        session,
        "book",
        {
            # Keys below must match cal-booking-actions -> Validate Request.
            "start": start_iso,
            "attendee": {
                "name": name or "Website visitor",
                "email": email,
                "timeZone": tz_name,
            },
            "reason": _clean(kwargs.get("notes")) or "Booked by the website assistant",
        },
    )

    if outcome == "rejected":
        # n8n never accepted the request, so nothing was created. This is the one
        # case where "not available" is honest.
        session.booking_intent["state"] = "failed"
        session.booking_error = "the booking request was not accepted"
        # A failed booking must not lose the lead: persist and let a human recover.
        save_lead(session, visitor_name=name, visitor_email=email, visitor_phone=phone)
        session.fields["requested_meeting_time"] = human
        persist(session, _lead_status(session))
        # This string used to instruct: "Say a specialist will confirm {human}
        # by email, and that their details are with the team." The agent obeyed
        # it exactly, and a visitor whose booking had 400'd was told their
        # meeting was arranged. The prompt forbids claiming an unconfirmed
        # booking, but a tool result is a direct instruction and outranks it --
        # so the fix belongs here, not in the prompt.
        return (
            f"NOT_BOOKED: {human} could not be taken. Do NOT say it is booked, "
            "confirmed, arranged, or that anyone will confirm it later -- none of "
            "that is true. Tell them plainly that time is not available, then ask "
            "for another time within the next 3 working days, 9am-5pm. Do not "
            "mention systems, errors or calendars."
        )

    if outcome == "timeout":
        # The intent stays pending on purpose: the booking may well exist, and a
        # late callback will still confirm it. Saying "not available" here is the
        # exact lie that told four visitors their meetings had failed.
        session.fields["requested_meeting_time"] = human
        persist(session, _lead_status(session))
        return (
            f"BOOKING_UNCONFIRMED: {human} was sent for booking but has not come "
            "back confirmed yet. Do NOT say it is booked and do NOT say the time "
            "is unavailable -- neither is known. Tell them you are still "
            "confirming it and the calendar invite will follow by email shortly, "
            "then stop. Do not attempt another booking and do not offer a "
            "different time. Do not mention systems, errors or calendars."
        )

    # Acked. The callback has already applied the outcome, so the session -- not
    # this coroutine -- is the authority on what happened.
    if session.meeting_booked and session.cal_booking_uid:
        # The acknowledged time, not the requested one. If Cal.com booked
        # something other than what we asked for, the visitor must be told what
        # is actually on the calendar -- and it must match the booking status
        # line the model sees on the next turn.
        confirmed = (session.booking_intent or {}).get("human") or human
        return (
            f"BOOKED for {confirmed} ({tz_name}). Confirm that warmly, tell them the "
            f"calendar invite is on its way to {email}, and stop selling."
        )

    save_lead(session, visitor_name=name, visitor_email=email, visitor_phone=phone)
    session.fields["requested_meeting_time"] = human
    persist(session, _lead_status(session))
    return (
        f"NOT_BOOKED: {human} could not be taken. Do NOT say it is booked, "
        "confirmed, arranged, or that anyone will confirm it later -- none of "
        "that is true. Tell them plainly that time is not available, then ask "
        "for another time within the next 3 working days, 9am-5pm. Do not "
        "mention systems, errors or calendars."
    )


async def reschedule_meeting(session: Session, **kwargs: Any) -> str:
    """Move the existing meeting. Never books a second one.

    Without this tool the model had only book_meeting, so "can we move it to
    5pm?" produced a SECOND booking and left the first live on the calendar --
    two meetings, one visitor. The n8n workflow already routed
    book | cancel | reschedule through Route Action; only the tool was missing.

    Cal.com's reschedule mints a NEW uid and cancels the old one, so the new uid
    is written back to the session -- otherwise the next reschedule or cancel
    would target a uid Cal.com has already retired.
    """
    tz_name = SETTINGS.meeting_timezone
    wanted = _clean(kwargs.get("preferred_time"))

    if not session.cal_booking_uid:
        return (
            "ASK: there is no booking on record for this visitor, so nothing to "
            "move. If they want a call, collect their details and book one."
        )
    in_flight = _pending_intent(session)
    if in_flight:
        return (
            f"BOOKING_IN_FLIGHT: a change for {in_flight.get('human') or 'this visitor'} "
            "was already sent and is still being confirmed. Do NOT send another. "
            "Tell them it is still being confirmed and they will get an updated "
            "invite by email, then stop."
        )
    if not wanted:
        return "ASK: no new time given. Ask what day and time they would prefer instead."

    start = parse_desired_time(wanted, tz_name)
    if start is None:
        return (
            f"ASK: could not pin down {wanted!r}. Ask for a day plus a rough "
            "time within the next 3 working days."
        )

    local = start.astimezone(ZoneInfo(tz_name))
    human = _human_time(local)

    # `availability` is a STATUS STRING, not a bool. This was `is_free, free =
    # ...` followed by `if not is_free:`, which never fired -- every non-empty
    # string is truthy -- so the guard was dead code and a reschedule would move
    # a meeting onto an occupied slot. book_meeting was migrated to the string
    # form; this call site was missed.
    availability, free = await _slot_check(start, tz_name)
    if availability == "error":
        return (
            f"AVAILABILITY_UNKNOWN: could not verify {human}. Their existing "
            "meeting is UNCHANGED and nothing has been moved. Tell them "
            "availability could not be confirmed and ask to try again."
        )
    if availability == "unavailable":
        alternatives = _offer(free, local.strftime("%H:%M"))
        return (
            f"SLOT_TAKEN: {human} is not available. Their existing meeting is "
            f"UNCHANGED. Tell them that time is taken, offer these instead: "
            f"{alternatives}, and ask which they prefer. Do NOT move the meeting "
            "until they pick one."
        )

    old_uid = session.cal_booking_uid
    start_iso = start.isoformat().replace("+00:00", "Z")
    session.booking_intent = {
        "action": "reschedule",
        "start_iso": start_iso,
        "human": human,
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "state": "pending",
        "from_uid": old_uid,
    }
    session.booking_error = None
    session.note_action("reschedule_meeting")

    outcome = await _fire_and_wait(
        session,
        "reschedule",
        {
            "booking_uid": old_uid,
            "start": start_iso,
            "reason": _clean(kwargs.get("reason")) or "Rescheduled at the visitor's request",
        },
    )

    if outcome == "timeout":
        return (
            f"RESCHEDULE_UNCONFIRMED: the move to {human} was sent but has not "
            "come back confirmed. Do NOT say it has been moved and do NOT say it "
            "failed -- neither is known. Tell them you are still confirming the "
            "change and an updated invite will follow by email, then stop. Do not "
            "send another change."
        )

    # Cal.com's reschedule mints a NEW uid and cancels the old one; the callback
    # has already written it back, or the next cancel would target a uid Cal.com
    # has retired.
    if outcome == "acked" and session.cal_booking_uid and session.cal_booking_uid != old_uid:
        confirmed = (session.booking_intent or {}).get("human") or human
        return (
            f"RESCHEDULED to {confirmed} ({tz_name}). The original slot is released -- "
            "there is only one meeting. Confirm the new time warmly and stop selling."
        )

    session.booking_intent["state"] = "failed"
    log.warning("reschedule failed for %s: %s", session.session_id, outcome)
    return (
        f"NOT_RESCHEDULED: could not move the meeting to {human}. Do NOT say "
        "it has been moved. Their ORIGINAL meeting still stands -- say that "
        "plainly, and offer to try another time. Do not mention systems."
    )


async def cancel_meeting(session: Session, **kwargs: Any) -> str:
    """Cancel the existing meeting. Only on an explicit request."""
    if not session.cal_booking_uid:
        return "ASK: there is no booking on record for this visitor, so nothing to cancel."

    session.booking_intent = {
        "action": "cancel",
        "start_iso": (session.booking_intent or {}).get("start_iso"),
        "human": (session.booking_intent or {}).get("human"),
        "fired_at": datetime.now(timezone.utc).isoformat(),
        "state": "pending",
    }
    session.note_action("cancel_meeting")

    outcome = await _fire_and_wait(
        session,
        "cancel",
        {
            "booking_uid": session.cal_booking_uid,
            "reason": _clean(kwargs.get("reason")) or "Cancelled at the visitor's request",
        },
    )

    if outcome == "timeout":
        return (
            "CANCEL_UNCONFIRMED: the cancellation was sent but has not come back "
            "confirmed. Do NOT say it is cancelled -- that is not known yet. Tell "
            "them you are processing it and they will get a confirmation by email, "
            "then stop."
        )

    # The callback clears the uid on a successful cancel, so an empty uid here is
    # the proof -- not the fact that the request was accepted.
    if outcome == "acked" and not session.cal_booking_uid:
        return (
            "CANCELLED. Confirm it is cancelled, and ask -- once, without pushing -- "
            "whether they would like a different time instead."
        )

    session.booking_intent["state"] = "failed"
    log.warning("cancel failed for %s: %s", session.session_id, outcome)
    return (
        "NOT_CANCELLED: the meeting could not be cancelled. Do NOT say it is "
        "cancelled -- it still stands. Offer to have someone sort it out, and "
        "do not mention systems."
    )


def request_proposal(session: Session, **kwargs: Any) -> str:
    """Flag that the visitor wants something in writing."""
    email = _clean(kwargs.get("visitor_email")) or session.fields.get("visitor_email")
    if not email:
        return "Need an email address to send a proposal -- ask for it."
    if not EMAIL_RE.match(email):
        return "That email doesn't look right -- ask the visitor to confirm it."

    session.fields["visitor_email"] = email
    for key in ("visitor_name", "company_name", "service_recommended"):
        value = _clean(kwargs.get(key))
        if value:
            session.fields[key] = value

    # Captured before the flag is set, so a second call in the same
    # conversation does not fire a second event -- the proposal branch has no
    # idempotency guard of its own and would create a duplicate AutoCRM task.
    #
    # The guard cannot live in n8n: persist() below writes
    # proposal_requested = true BEFORE the event is emitted, so a workflow
    # checking that column would see true on the FIRST event and skip every
    # proposal. Marking it n8n-side instead would need a new agent_runs column,
    # and widening mark_run_outcome() means dropping its old signature or
    # PostgREST answers 300 and breaks every write-back at once (schema.sql).
    already_requested = session.proposal_requested

    session.proposal_requested = True
    session.note_action("request_proposal")
    status = _lead_status(session)
    persist(session, status)
    if not already_requested:
        n8n_client.emit_event(
            "proposal_requested",
            session.session_id,
            {**_lead_payload(session, status), "requirements": _clean(kwargs.get("requirements"))},
        )
    return "Proposal request logged. Tell them roughly when to expect it, without promising a price."


def handoff_to_human(session: Session, **kwargs: Any) -> str:
    """Escalate to live chat."""
    session.handoff_requested = True
    session.note_action("handoff_to_human")
    session.fields["handoff_reason"] = _clean(kwargs.get("reason")) or "visitor requested"
    persist(session, _lead_status(session))
    # The widget watches for this marker and opens Tawk.to.
    return "__HANDOFF__ Tell the visitor you're connecting them to a colleague now."


def check_page(session: Session, **kwargs: Any) -> str:
    """Confirm a path exists before mentioning it. Guards the /pricing case."""
    path = _clean(kwargs.get("path")) or ""
    normalised = "/" + path.strip("/") if path.strip("/") else "/"
    if normalised in allowed_paths():
        if normalised not in session.pages:
            session.pages.append(normalised)
        return f"{normalised} exists -- safe to mention."
    return (
        f"{normalised} does not exist on the site. Do not mention it. "
        "If the visitor asked about pricing, remember the site publishes none."
    )


# name -> (callable, is_async)
REGISTRY: dict[str, tuple[Callable[..., Any], bool]] = {
    "save_lead": (save_lead, False),
    "book_meeting": (book_meeting, True),
    "reschedule_meeting": (reschedule_meeting, True),
    "cancel_meeting": (cancel_meeting, True),
    "request_proposal": (request_proposal, False),
    "handoff_to_human": (handoff_to_human, False),
    "check_page": (check_page, False),
}


async def dispatch(name: str, session: Session, args: dict[str, Any]) -> str:
    """Run a tool by name. Returns a model-facing string, never raises."""
    entry = REGISTRY.get(name)
    if entry is None:
        return f"No such tool {name!r}. Continue the conversation without it."
    fn, is_async = entry
    try:
        return await fn(session, **args) if is_async else fn(session, **args)
    except Exception as exc:
        log.exception("tool %s failed", name)
        return (
            f"{name} failed ({type(exc).__name__}). Keep helping the visitor and "
            "offer to have a specialist follow up."
        )
