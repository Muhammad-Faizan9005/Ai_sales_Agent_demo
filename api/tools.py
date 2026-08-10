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
from datetime import datetime, timedelta, timezone
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
    if not (f.get("visitor_email") or f.get("visitor_phone")):
        return "ASK: no contact detail yet. Ask for an email or phone number."

    session.note_action("save_lead")
    status = _lead_status(session)
    persist(session, status)
    n8n_client.emit_event("lead_created", session.session_id, _lead_payload(session, status))

    # Ask for at most one more thing, in the order a rep actually needs it.
    for field, prompt in (
        ("visitor_name", "their name"),
        ("visitor_email", "their email"),
        ("visitor_phone", "a phone number, so a rep can call"),
        ("company_name", "their company name"),
    ):
        if not f.get(field):
            return (
                f"SAVED. Lead is in the CRM. Still missing {prompt} -- work that "
                "into your reply as one natural question, not a form."
            )

    return (
        "SAVED. Lead is in the CRM with full contact details. Do not ask for more "
        "details. Move to booking the call, or confirm the next step and stop."
    )


BUSINESS_START_HOUR = 9   # local, inclusive
BUSINESS_END_HOUR = 17    # local, exclusive -- last start is 16:xx
BOOKING_HORIZON_DAYS = 3  # "within the next 3 days"


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
        for i, name in enumerate(
            ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        ):
            if name in raw:
                ahead = (i - now.weekday()) % 7 or 7
                day = now.date() + timedelta(days=ahead)
                break

    hour = minute = None
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", raw)
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

    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)

    # Roll a time that has already passed to the same clock time tomorrow.
    if local <= now + timedelta(minutes=15):
        local += timedelta(days=1)
    # Clamp into business hours instead of booking 3am.
    if local.hour < BUSINESS_START_HOUR:
        local = local.replace(hour=BUSINESS_START_HOUR, minute=0)
    elif local.hour >= BUSINESS_END_HOUR:
        local = (local + timedelta(days=1)).replace(hour=BUSINESS_START_HOUR, minute=0)
    # Weekends are not staffed.
    while local.weekday() >= 5:
        local = (local + timedelta(days=1)).replace(hour=BUSINESS_START_HOUR, minute=0)

    if local > now + timedelta(days=BOOKING_HORIZON_DAYS + 2):
        return None  # too far out to be what "next few days" meant
    return local.astimezone(timezone.utc)


async def book_meeting(session: Session, **kwargs: Any) -> str:
    """Book the call at the time the visitor asked for.

    No slot picker: the visitor names a time, we parse it, and we book it. The
    payload shape is dictated by the cal-booking-actions workflow's Validate
    Request node -- it reads `start` and `attendee.email`, so a flat
    start_time/visitor_email payload is rejected before Cal.com is ever called.
    """
    tz_name = SETTINGS.meeting_timezone
    name = _clean(kwargs.get("visitor_name")) or session.fields.get("visitor_name")
    email = _clean(kwargs.get("visitor_email")) or session.fields.get("visitor_email")
    phone = _clean(kwargs.get("visitor_phone")) or session.fields.get("visitor_phone")
    wanted = _clean(kwargs.get("preferred_time"))

    if not email:
        return "ASK: no email yet. Ask for their email so the invite can be sent."
    if not EMAIL_RE.match(email):
        return "ASK: that email looks malformed. Ask them to confirm it."
    if not wanted:
        return (
            "ASK: no time given. Ask which day and rough time suits them in the "
            "next 3 days, e.g. 'tomorrow afternoon' or 'Thursday at 11'."
        )

    start = parse_desired_time(wanted, tz_name)
    if start is None:
        return (
            f"ASK: could not pin down {wanted!r} to a real slot. Ask for a day "
            "plus a rough time within the next 3 working days."
        )

    session.fields["visitor_email"] = email
    if name:
        session.fields["visitor_name"] = name
    if phone:
        session.fields["visitor_phone"] = phone

    local = start.astimezone(ZoneInfo(tz_name))
    human = local.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")

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

    if not result.get("ok"):
        # A failed booking must not lose the lead: persist and let a human recover.
        log.warning("booking failed for %s: %s", session.session_id, result)
        save_lead(session, visitor_name=name, visitor_email=email, visitor_phone=phone)
        session.fields["requested_meeting_time"] = human
        persist(session, _lead_status(session))
        return (
            f"FAILED_SILENTLY: booking did not go through. Do NOT mention systems, "
            f"errors or the calendar. Say a specialist will confirm {human} by email, "
            "and that their details are with the team."
        )

    session.meeting_booked = True
    session.cal_booking_uid = result.get("booking_uid") or result.get("uid")
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
