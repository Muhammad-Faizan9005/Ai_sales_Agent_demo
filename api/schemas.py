"""Tool schemas sent to the model, plus the request/response models.

Descriptions are prompt surface, not documentation -- they are what makes the
model call `check_page` before naming a URL and `save_lead` before the
conversation ends, so they read as instructions to the caller.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "save_lead",
            "description": (
                "Save the visitor's contact and qualification details. Call this as "
                "soon as you have a name plus an email or phone -- do not wait for "
                "the end of the conversation. Call it again with new fields as you "
                "learn more."
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
                "Book the call with a sales rep at the time the visitor asked for. "
                "There is no slot list: ask which day and rough time suits them in "
                "the next 3 days, then pass their words straight through as "
                "preferred_time. Requires their email."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preferred_time": {
                        "type": "string",
                        "description": (
                            "The visitor's own words for when they want the call, "
                            "e.g. 'tomorrow at 3pm', 'Thursday morning', 'today 4'. "
                            "Do not convert to a timestamp."
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
                "required": ["preferred_time", "visitor_email"],
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
    {
        "type": "function",
        "function": {
            "name": "check_page",
            "description": (
                "Verify a site path exists before you mention it. Call this whenever "
                "you are about to point the visitor at a page. The site has no "
                "pricing page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. /seo/local-seo"}
                },
                "required": ["path"],
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
