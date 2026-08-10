"""Deterministic hot/warm/cold lead scoring.

Deliberately not model-scored. The n8n workflow routes on this value -- 'hot'
emails a rep immediately -- so the same conversation must always produce the
same routing. An LLM asked to rate a lead 1-10 gives a different answer to the
same transcript on a second call, which would make the notification behaviour
irreproducible and the pipeline impossible to test.

Signals are drawn from what a sales rep actually acts on: reachability first
(you cannot chase a lead with no contact detail), then stated intent, then
engagement depth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Phrases that indicate a live buying timeline. Matched case-insensitively
# against the full transcript.
URGENCY_PHRASES = (
    "as soon as possible",
    "asap",
    "urgent",
    "urgently",
    "this week",
    "next week",
    "this month",
    "immediately",
    "right away",
    "deadline",
    "already have a budget",
    "ready to start",
    "ready to go",
    "need this done",
)

# Explicit requests to speak to a human. Asking for a call is a commitment,
# not a hint -- it belongs with meeting_booked, not with the point arithmetic.
# A real lead who said "ok am ready for a call" was scored warm before this
# existed, so the rep was never paged.
CALL_REQUEST_PHRASES = (
    "ready for a call",
    "ready for the call",
    "up for a call",
    "book a call",
    "book the call",
    "schedule a call",
    "arrange a call",
    "set up a call",
    "have someone call",
    "someone call me",
    "give me a call",
    "call me",
    "let's talk",
    "lets talk",
    "speak to someone",
    "talk to someone",
    "speak to a specialist",
    "talk to a specialist",
    "when can we talk",
    "i'm available",
    "im available",
)

# Explicit disqualifiers. A student researching for a class is not a warm lead
# no matter how many pages they read.
COLD_PHRASES = (
    "just curious",
    "just browsing",
    "just looking",
    "for a school project",
    "for my studies",
    "writing an article",
    "doing research for",
    "i'm a student",
    "im a student",
    "not looking to buy",
    "no budget",
)


@dataclass(frozen=True)
class Score:
    status: str  # hot | warm | cold
    points: int
    reasons: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict:
        return {"status": self.status, "points": self.points, "reasons": list(self.reasons)}


def _has(text: str, phrases: tuple[str, ...]) -> str | None:
    for phrase in phrases:
        if phrase in text:
            return phrase
    return None


def score_lead(
    *,
    visitor_name: str | None = None,
    visitor_email: str | None = None,
    visitor_phone: str | None = None,
    company_name: str | None = None,
    website_url: str | None = None,
    industry: str | None = None,
    service_recommended: str | None = None,
    meeting_booked: bool = False,
    proposal_requested: bool = False,
    messages_count: int = 0,
    pages_visited: list[str] | None = None,
    transcript_text: str = "",
) -> Score:
    """Map qualification state to hot/warm/cold. Pure function.

    Thresholds: >=8 hot, >=4 warm, else cold. A booked meeting or a requested
    proposal is hot regardless of points -- someone who committed to a calendar
    slot is qualified by the act itself.
    """
    text = (transcript_text or "").lower()
    pages = pages_visited or []
    points = 0
    reasons: list[str] = []

    def add(n: int, why: str) -> None:
        nonlocal points
        points += n
        reasons.append(f"{why} (+{n})")

    # Reachability. Email and phone are not additive at full weight: a second
    # channel helps, but the first is what makes the lead workable at all.
    if visitor_email:
        add(3, "email captured")
    if visitor_phone:
        add(2 if visitor_email else 3, "phone captured")
    if visitor_name:
        add(1, "name captured")

    # Firmographics -- a named company with a live site is a real business.
    if company_name:
        add(1, "company named")
    if website_url:
        add(1, "website given")
    if industry:
        add(1, "industry known")

    # Intent.
    if service_recommended:
        add(1, "specific service identified")
    urgency = _has(text, URGENCY_PHRASES)
    if urgency:
        add(3, f"urgency: {urgency!r}")

    # Engagement. Capped so a long conversation cannot alone reach hot.
    if messages_count >= 10:
        add(2, "long conversation")
    elif messages_count >= 5:
        add(1, "sustained conversation")
    if len(pages) >= 3:
        add(1, "browsed 3+ pages")

    # Committed actions override the arithmetic.
    if meeting_booked:
        reasons.append("meeting booked (override -> hot)")
        return Score("hot", max(points, 8), tuple(reasons))
    if proposal_requested:
        reasons.append("proposal requested (override -> hot)")
        return Score("hot", max(points, 8), tuple(reasons))

    # Self-disqualification overrides upward signals, but never a real
    # commitment -- hence checked after the two overrides above.
    cold = _has(text, COLD_PHRASES)
    if cold:
        reasons.append(f"self-disqualified: {cold!r} (-> cold)")
        return Score("cold", points, tuple(reasons))

    # Asking to be called is nearly the commitment that booking is, minus the
    # time. Checked *below* the cold gate on purpose: it is phrase evidence, so
    # it must not outrank the opposing phrase evidence of "I'm a student".
    # Requires a contact detail -- a call request we cannot act on is not hot.
    call_request = _has(text, CALL_REQUEST_PHRASES)
    if call_request and (visitor_email or visitor_phone):
        reasons.append(f"asked to be called: {call_request!r} (override -> hot)")
        return Score("hot", max(points, 8), tuple(reasons))

    status = "hot" if points >= 8 else "warm" if points >= 4 else "cold"
    return Score(status, points, tuple(reasons))
