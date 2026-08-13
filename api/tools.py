"""Server-side tools the model can call.

Every tool follows the same shape: validate, mutate session state, persist,
fan out to n8n, return a short string for the model to speak from. The return
value is model-facing, so it says what happened rather than dumping JSON.

Nothing here raises. A tool that throws would abort the turn and lose the
visitor's reply; a tool that fails should hand the model a graceful line
instead.
"""

from __future__ import annotations

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


async def book_meeting(session: Session, **kwargs: Any) -> str:
    """Book the call at the time the visitor asked for.

    Three guards, each from a defect that reached a real visitor:

      * Contact details first. This used to require only an email, so the agent
        booked before qualification finished and AutoCRM got a lead with no
        phone number -- the rep could not call them.
      * The slot must actually be free. Booking blind meant the only feedback
        was a 400 after the fact, and the model then invented a different day
        and announced it as booked.
      * The visitor must have named the time. The agent may never move someone
        to a slot they did not say aloud.
    """
    tz_name = SETTINGS.meeting_timezone
    if session.cal_booking_uid:
        return (
            "ALREADY_BOOKED: this visitor already has a meeting. Do not create "
            "another one. If they requested a different time, use "
            "reschedule_meeting; otherwise confirm the existing booking."
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

    result = await n8n_client.call_cal_action(
        "book",
        session.session_id,
        {
            # Keys below must match cal-booking-actions -> Validate Request.
            "start": start.isoformat().replace("+00:00", "Z"),
            "attendee": {
                "name": name or "Website visitor",
                "email": email,
                "timeZone": tz_name,
            },
            "reason": _clean(kwargs.get("notes")) or "Booked by the website assistant",
        },
    )

    # THE UID IS THE ONLY PROOF A MEETING EXISTS.
    #
    # Extracted before any state is mutated, because `ok:true` is not enough: a
    # response can be well-formed and still carry no booking. Previously
    # meeting_booked was set first and the uid read afterwards, so an ok-but-
    # uidless response marked the run booked and emitted meeting_booked to n8n
    # for a meeting nobody had.
    booking_uid = result.get("booking_uid") or result.get("uid")

    if not result.get("ok") or not booking_uid:
        # A failed booking must not lose the lead: persist and let a human recover.
        log.warning("booking failed for %s: %s", session.session_id, result)
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

    session.meeting_booked = True
    session.cal_booking_uid = booking_uid
    session.pending_meeting_start = None
    session.pending_meeting_label = None
    session.note_action("book_meeting")
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
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "uid": session.cal_booking_uid,
                "meeting_url": result.get("meeting_url"),
                "status": result.get("status", "accepted"),
            },
            # Kept flat alongside for the humans reading n8n's execution log --
            # the workflow derives its own localised string from start_time.
            "meeting_time_local": human,
        },
    )
    return (
        f"BOOKED for {human} ({tz_name}). Confirm that warmly, tell them the "
        f"calendar invite is on its way to {email}, and stop selling."
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
    if not wanted:
        return "ASK: no new time given. Ask what day and time they would prefer instead."

    start = parse_desired_time(wanted, tz_name)
    if start is None:
        return (
            f"ASK: could not pin down {wanted!r}. Ask for a day plus a rough "
            "time within the next 3 working days."
        )

    local = start.astimezone(ZoneInfo(tz_name))
    human = local.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")

    is_free, free = await _slot_check(start, tz_name)
    if not is_free:
        alternatives = _offer(free, local.strftime("%H:%M"))
        return (
            f"SLOT_TAKEN: {human} is not available. Their existing meeting is "
            f"UNCHANGED. Tell them that time is taken, offer these instead: "
            f"{alternatives}, and ask which they prefer. Do NOT move the meeting "
            "until they pick one."
        )

    old_uid = session.cal_booking_uid
    result = await n8n_client.call_cal_action(
        "reschedule",
        session.session_id,
        {
            "booking_uid": old_uid,
            "start": start.isoformat().replace("+00:00", "Z"),
            "reason": _clean(kwargs.get("reason")) or "Rescheduled at the visitor's request",
        },
    )

    new_uid = result.get("booking_uid") or result.get("uid")
    if not result.get("ok") or not new_uid:
        log.warning("reschedule failed for %s: %s", session.session_id, result)
        return (
            f"NOT_RESCHEDULED: could not move the meeting to {human}. Do NOT say "
            "it has been moved. Their ORIGINAL meeting still stands -- say that "
            "plainly, and offer to try another time. Do not mention systems."
        )

    session.cal_booking_uid = new_uid
    session.meeting_booked = True
    session.note_action("reschedule_meeting")
    status = _lead_status(session)
    persist(session, status)
    n8n_client.emit_event(
        "meeting_booked",
        session.session_id,
        {
            **_lead_payload(session, status),
            "meeting": {
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "uid": new_uid,
                "meeting_url": result.get("meeting_url"),
                "status": result.get("status", "accepted"),
            },
            "meeting_time_local": human,
            "rescheduled_from_uid": old_uid,
        },
    )
    return (
        f"RESCHEDULED to {human} ({tz_name}). The original slot is released -- "
        "there is only one meeting. Confirm the new time warmly and stop selling."
    )


async def cancel_meeting(session: Session, **kwargs: Any) -> str:
    """Cancel the existing meeting. Only on an explicit request."""
    if not session.cal_booking_uid:
        return "ASK: there is no booking on record for this visitor, so nothing to cancel."

    result = await n8n_client.call_cal_action(
        "cancel",
        session.session_id,
        {
            "booking_uid": session.cal_booking_uid,
            "reason": _clean(kwargs.get("reason")) or "Cancelled at the visitor's request",
        },
    )

    if not result.get("ok"):
        log.warning("cancel failed for %s: %s", session.session_id, result)
        return (
            "NOT_CANCELLED: the meeting could not be cancelled. Do NOT say it is "
            "cancelled -- it still stands. Offer to have someone sort it out, and "
            "do not mention systems."
        )

    session.meeting_booked = False
    session.cal_booking_uid = None
    session.note_action("cancel_meeting")
    persist(session, _lead_status(session))
    return (
        "CANCELLED. Confirm it is cancelled, and ask -- once, without pushing -- "
        "whether they would like a different time instead."
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
