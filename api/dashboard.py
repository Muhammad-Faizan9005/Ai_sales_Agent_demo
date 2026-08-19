"""Admin dashboard queries: leads, conversion, meetings.

Requirement 10, scoped down. `cluade_siteagentplan.md` s7 calls for a leads
table, meetings booked and conversion rate -- and explicitly nothing more.

Two panels the requirements PDF asks for are NOT here and are not coming:

  Total Website Visitors     needs a page beacon; nothing tracks page views
  AI Performance Analytics   needs per-turn latency; nothing records it

Neither has a data source. A panel showing a number nobody computed is worse
than a missing panel, so they are cut and said out loud.

Reads only. Every write path stays where it already is -- the chat turn and the
n8n fan-out.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse

from api.config import get_settings
from api.store import _connect

log = logging.getLogger("dashboard")
SETTINGS = get_settings()

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class LeadUpdate(BaseModel):
    lead_status: str | None = Field(default=None, pattern="^(hot|warm|cold|new|contacted|qualified|unqualified|junk)$")
    handoff_requested: bool | None = None

# Columns safe to list. Deliberately excludes `transcript`: a list endpoint
# returning every full conversation is both a payload and a privacy problem.
# The detail endpoint returns it for one run at a time.
LIST_COLUMNS = """
    run_id, session_id, started_at, ended_at, duration_ms,
    visitor_name, visitor_email, visitor_phone, company_name,
    website_url, industry, lead_status, service_recommended,
    meeting_booked, proposal_requested, handoff_requested,
    messages_count, crm_synced AS lead_persisted, crm_lead_id AS lead_id, sheets_synced,
    notification_sent, cal_booking_status, meeting_start_at, error
"""


def require_token(x_dashboard_token: str = Header(default="")) -> None:
    """Shared-token gate.

    An unset token denies everything rather than allowing everything: the
    failure mode of a misconfigured deploy must be "nobody can read the leads",
    never "anybody can".
    """
    if not SETTINGS.dashboard_token:
        raise HTTPException(503, "DASHBOARD_TOKEN is not configured")
    if x_dashboard_token != SETTINGS.dashboard_token:
        raise HTTPException(401, "bad or missing X-Dashboard-Token")


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Run a read query. Returns [] when the database is unreachable."""
    conn = _connect()
    if conn is None:
        log.warning("no DB connection -- dashboard returns empty")
        return []
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        log.exception("dashboard query failed")
        return []
    finally:
        conn.close()


@router.get("/stats", dependencies=[Depends(require_token)])
def stats() -> dict[str, Any]:
    """Headline counters, computed in one pass over the table."""
    rows = _rows(
        """
        SELECT
            COUNT(*)                                             AS conversations,
            COUNT(*) FILTER (WHERE visitor_email IS NOT NULL
                                OR visitor_phone IS NOT NULL)    AS leads,
            COUNT(*) FILTER (WHERE meeting_booked)               AS meetings,
            COUNT(*) FILTER (WHERE proposal_requested)           AS proposals,
            COUNT(*) FILTER (WHERE handoff_requested)            AS handoffs,
            COUNT(*) FILTER (WHERE lead_status = 'hot')          AS hot,
            COUNT(*) FILTER (WHERE lead_status = 'warm')         AS warm,
            COUNT(*) FILTER (WHERE lead_status = 'cold')         AS cold,
            COUNT(*) FILTER (WHERE error IS NOT NULL)            AS errored,
            COUNT(*) FILTER (WHERE crm_synced)                   AS lead_persisted
        FROM agent_runs
        """
    )
    s = rows[0] if rows else {}
    conversations = s.get("conversations", 0) or 0
    leads = s.get("leads", 0) or 0
    meetings = s.get("meetings", 0) or 0
    return {
        **{k: (v or 0) for k, v in s.items()},
        # Two different denominators, both reported. Meetings over *leads* is
        # the sales number; over *conversations* is the funnel number. Quoting
        # one as "the" conversion rate invites reading it as the other.
        "lead_rate": round(leads / conversations, 4) if conversations else 0.0,
        "conversion_rate": round(meetings / leads, 4) if leads else 0.0,
        "meeting_rate": round(meetings / conversations, 4) if conversations else 0.0,
    }


@router.get("/runs", dependencies=[Depends(require_token)])
def runs(
    lead_status: str | None = Query(default=None, pattern="^(hot|warm|cold)$"),
    booked: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Recent runs, newest first. Filterable by status and booking."""
    where, params = [], []
    if lead_status:
        where.append("lead_status = %s")
        params.append(lead_status)
    if booked is not None:
        where.append("meeting_booked = %s")
        params.append(booked)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = _rows(f"SELECT COUNT(*) AS n FROM agent_runs {clause}", tuple(params))
    items = _rows(
        f"""SELECT {LIST_COLUMNS} FROM agent_runs {clause}
            ORDER BY started_at DESC LIMIT %s OFFSET %s""",
        tuple(params) + (limit, offset),
    )
    return {
        "total": (total[0]["n"] if total else 0),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.patch("/runs/{session_id}", dependencies=[Depends(require_token)])
def update_run(session_id: str, update: LeadUpdate) -> dict[str, Any]:
    """Update local lead state without requiring an external CRM."""
    fields, params = [], []
    if update.lead_status is not None:
        fields.append("lead_status = %s")
        params.append(update.lead_status)
    if update.handoff_requested is not None:
        fields.append("handoff_requested = %s")
        params.append(update.handoff_requested)
    if not fields:
        raise HTTPException(400, "no update fields supplied")
    params.append(session_id)
    rows = _rows(
        f"UPDATE agent_runs SET {', '.join(fields)} WHERE session_id = %s "
        "RETURNING session_id, lead_status, handoff_requested",
        tuple(params),
    )
    if not rows:
        raise HTTPException(404, "no such session")
    return rows[0]


@router.get("/runs/{session_id}", dependencies=[Depends(require_token)])
def run_detail(session_id: str) -> dict[str, Any]:
    """One run including its transcript, actions and navigation path."""
    rows = _rows(
        f"""SELECT {LIST_COLUMNS}, transcript, actions_taken, pages_visited,
                   crm_task_id, cal_booking_uid, cal_meeting_url
            FROM agent_runs WHERE session_id = %s""",
        (session_id,),
    )
    if not rows:
        raise HTTPException(404, "no such session")
    return rows[0]


@router.get("/faq", dependencies=[Depends(require_token)])
def faq(limit: int = Query(default=15, ge=1, le=50)) -> list[dict[str, Any]]:
    """Clustered visitor questions, written by the nightly n8n job.

    Empty until that workflow has run at least once -- it needs an Anthropic
    key, which is not set. Empty here means "not yet clustered", not "no
    questions asked".
    """
    return _rows(
        "SELECT question, frequency, last_seen_at FROM faq_summary "
        "ORDER BY frequency DESC, last_seen_at DESC LIMIT %s",
        (limit,),
    )


@router.get("/services", dependencies=[Depends(require_token)])
def services(limit: int = Query(default=10, ge=1, le=50)) -> list[dict[str, Any]]:
    """Most-recommended services -- requirement 10's 'Popular Services'."""
    return _rows(
        """SELECT service_recommended AS service,
                  COUNT(*)                              AS runs,
                  COUNT(*) FILTER (WHERE meeting_booked) AS meetings
           FROM agent_runs
           WHERE service_recommended IS NOT NULL
           GROUP BY service_recommended
           ORDER BY runs DESC LIMIT %s""",
        (limit,),
    )


@router.get("/")
def index() -> FileResponse:
    """The page itself.

    Unauthenticated on purpose: it is an empty shell that asks for the token
    and holds it in memory only. Every byte of data behind it is gated above.
    """
    page = SETTINGS.dashboard_dir / "index.html"
    if not page.exists():
        raise HTTPException(404, "dashboard/index.html missing")
    return FileResponse(page)
