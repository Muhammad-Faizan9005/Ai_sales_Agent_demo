"""Tool schemas sent to the model, plus the request/response models.

Descriptions are prompt surface, not documentation. Read-only website paths are
provided by the validated server prompt; tools are reserved for business side
effects such as saving leads and changing bookings.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "save_lead",
            "description": (
                "Progressively save all seven required lead fields: full name, "
                "company name, email, phone, website URL (or 'no website'), business "
                "industry, and required service. Keep qualifying until this tool "
                "reports QUALIFIED. Do not offer or book a meeting before then."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "visitor_name": {"type": "string"},
                    "visitor_email": {"type": "string"},
                    "visitor_phone": {"type": "string"},
                    "company_name": {"type": "string"},
                    "website_url": {"type": "string"},
                    "industry": {"type": "string"},
                    "service_recommended": {
                        "type": "string",
                        "description": "Site path of the service you recommended, e.g. /seo/local-seo",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_meeting",
            "description": (
                "Book a NEW call at the time the visitor asked for. Only when they "
                "have no meeting yet -- if one exists, use reschedule_meeting "
                "instead, or you will create a second meeting. "
                "Requires all seven lead fields to be saved first: name, company, "
                "email, phone, website/no website, industry and required service. "
                "Only then ask which day and rough time suits them in the next 3 "
                "working days, then pass their words through unchanged. "
                "If the result says SLOT_TAKEN, offer the alternatives it gives "
                "and WAIT for the visitor to choose -- never pick for them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preferred_time": {
                        "type": "string",
                        "description": (
                            "The visitor's own words for when they want the call, "
                            "e.g. 'tomorrow at 3pm', '12 August at 3:30 pm', "
                            "'Thursday morning'. Do not convert to a timestamp, and "
                            "never invent a time they did not say. If the previous "
                            "tool result proposed a corrected weekend slot and the "
                            "visitor explicitly agrees, pass their confirmation "
                            "word such as 'yes' unchanged; the server resolves the "
                            "stored proposal."
                        ),
                    },
                    "visitor_name": {"type": "string"},
                    "visitor_email": {"type": "string"},
                    "visitor_phone": {"type": "string"},
                    "notes": {
                        "type": "string",
                        "description": "What they want to discuss, for the rep.",
                    },
                },
                "required": ["preferred_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_meeting",
            "description": (
                "Move the visitor's EXISTING meeting to a new time. Use this "
                "whenever they already have a booking and want a different slot -- "
                "calling book_meeting again would leave two meetings on the "
                "calendar. Same rule as booking: if the result says SLOT_TAKEN, "
                "offer the alternatives and wait for them to choose. Their "
                "original meeting stays put until they agree to a new time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preferred_time": {
                        "type": "string",
                        "description": (
                            "The visitor's own words for the NEW time, e.g. "
                            "'Thursday at 4' or '13 August at 11am'."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why they are moving it, if they said.",
                    },
                },
                "required": ["preferred_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_meeting",
            "description": (
                "Cancel the visitor's existing meeting. Only on an explicit "
                "request to cancel -- if they want a different time instead, use "
                "reschedule_meeting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why they are cancelling, if they said.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_proposal",
            "description": (
                "Log that the visitor wants a written proposal. Requires their email. "
                "Do not state or imply a price when confirming."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "visitor_email": {"type": "string"},
                    "visitor_name": {"type": "string"},
                    "company_name": {"type": "string"},
                    "service_recommended": {"type": "string"},
                    "requirements": {
                        "type": "string",
                        "description": "What they need, in their own words.",
                    },
                },
                "required": ["visitor_email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_to_human",
            "description": (
                "Connect the visitor to a human colleague. Call this when they ask "
                "for a person, sound frustrated, or raise something you cannot "
                "answer. Prefer handing off early over stalling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why you're escalating."}
                },
            },
        },
    },
]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=8, max_length=64)
    page_path: str | None = Field(default=None, max_length=300)
    # Adds `debug` SSE frames carrying retrieval hits, tool args/results and the
    # lead score. The test console sets it; the widget never does, so a
    # visitor's stream is unchanged. Reveals only what the server already knows
    # about that session -- no credentials, no cross-session data.
    debug: bool = False


class HealthResponse(BaseModel):
    status: str
    model: str
    kb_tokens: int
    allowlist_paths: int
    active_sessions: int
    retrieval_ready: bool
    indexed_chunks: int
    embed_model: str


class CalCallback(BaseModel):
    """What cal-booking-actions posts back once Cal.com has answered.

    Shaped by the Notify Agent OK / Notify Agent Error nodes in
    n8n/cal-booking-actions.json. Everything but session_id is optional: the
    error branch carries no uid, and a cancel carries no start. `ok` arriving as
    the string "true" is tolerated because n8n Set nodes stringify values.

    This is the ONLY channel that reports whether a booking happened. The
    workflow's own HTTP response no longer does -- awaiting it behind an 8s
    timeout is what told four visitors their real meetings had failed.
    """

    session_id: str = Field(min_length=1, max_length=64)
    action: str = Field(default="book", max_length=20)
    ok: bool = False
    booking_uid: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=40)
    start: str | None = Field(default=None, max_length=40)
    meeting_url: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, max_length=500)
    execution_id: str | None = Field(default=None, max_length=40)
