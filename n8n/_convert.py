"""One-shot: move every Postgres node onto the Supabase credential.

Reads  -> native Supabase node (getAll + filterString)
Writes -> HTTP Request node calling POST /rest/v1/rpc/<fn>
          (the Supabase node has no upsert and cannot express COALESCE writes)

Run once, then delete. n8n round-trips these settings on export.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SB_CRED = {"supabaseApi": {"id": "REPLACE_SUPABASE_CRED_ID", "name": "AI Sales Agent Supabase"}}
RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000}


def sb_read(name, table, filter_string, select, limit=1):
    """Native Supabase node, Get Many + PostgREST filter string."""
    return {
        "parameters": {
            "operation": "getAll",
            "tableId": table,
            "returnAll": False,
            "limit": limit,
            "filterType": "string",
            "filterString": filter_string,
            "options": {"select": select} if select else {},
        },
        "name": name,
        "type": "n8n-nodes-base.supabase",
        "typeVersion": 1,
        "credentials": SB_CRED,
    }


def sb_rpc(name, fn, body, notes):
    """HTTP Request node hitting a Postgres function through PostgREST."""
    return {
        "parameters": {
            "method": "POST",
            "url": "={{ $credentials.host }}/rest/v1/rpc/" + fn,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "supabaseApi",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body,
            "options": {"timeout": 15000},
        },
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "credentials": SB_CRED,
        "notes": notes,
    }


SESSION = "$('Webhook').item.json.body.session_id"

# ---------------------------------------------------------------- events -----
EVENTS = {
    "Check Existing Sync": lambda: sb_read(
        "Check Existing Sync", "agent_runs",
        "=session_id=eq." + "{{ " + SESSION + " }}", "crm_lead_id",
    ),
    "Lookup Lead (meeting)": lambda: sb_read(
        "Lookup Lead (meeting)", "agent_runs",
        "=session_id=eq." + "{{ " + SESSION + " }}",
        "crm_lead_id,visitor_name,visitor_email",
    ),
    "Lookup Lead (proposal)": lambda: sb_read(
        "Lookup Lead (proposal)", "agent_runs",
        "=session_id=eq." + "{{ " + SESSION + " }}",
        "crm_lead_id,visitor_name,visitor_email",
    ),
    "Write Back (lead_created)": lambda: sb_rpc(
        "Write Back (lead_created)", "mark_run_outcome",
        "={{ JSON.stringify({\n"
        "  p_session_id: " + SESSION + ",\n"
        "  p_crm_lead_id: $('Upsert AutoCRM Lead').item.json.id,\n"
        "  p_crm_synced: true,\n"
        "  p_sheets_synced: !$('Append to Sheets').item.json.error,\n"
        "  p_notification_sent: !$('Notify Rep (Email)').item.json.error,\n"
        "}) }}",
        "Was a Postgres UPDATE. mark_run_outcome COALESCEs every optional arg, so "
        "omitted keys leave their column untouched.",
    ),
    "Write Back (meeting)": lambda: sb_rpc(
        "Write Back (meeting)", "mark_run_outcome",
        "={{ JSON.stringify(Object.assign({\n"
        "  p_session_id: " + SESSION + ",\n"
        "  p_meeting_booked: true,\n"
        "  p_notification_sent: !$('Notify Rep (meeting)').item.json.error,\n"
        "}, $('Create Prep Task').item.json.error ? { p_error: 'prep task not created' } : {})) }}",
        "p_error is added only on failure. Sending null would be COALESCEd to a "
        "no-op anyway, but omitting the key keeps the request honest.",
    ),
    "Write Back (proposal)": lambda: sb_rpc(
        "Write Back (proposal)", "mark_run_outcome",
        "={{ JSON.stringify(Object.assign({\n"
        "  p_session_id: " + SESSION + ",\n"
        "  p_proposal_requested: true,\n"
        "  p_notification_sent: !$('Notify Rep (proposal)').item.json.error,\n"
        "}, $('Create Follow-up Task').item.json.error ? { p_error: 'follow-up task not created' } : {})) }}",
        "See Write Back (meeting).",
    ),
    "Record Failure": lambda: sb_rpc(
        "Record Failure", "record_run_error",
        "={{ JSON.stringify({\n"
        "  p_session_id: " + SESSION + ",\n"
        "  p_error: 'n8n exec ' + $execution.id + ' failed at: ' + $prevNode.name,\n"
        "}) }}",
        "INSERT .. ON CONFLICT inside record_run_error, so the failure is captured "
        "even when no agent_runs row exists yet.",
    ),
}

ERRH = {
    "Record Error on Run": lambda: sb_rpc(
        "Record Error on Run", "record_run_error",
        "={{ JSON.stringify({ p_session_id: $json.session_id, p_error: $json.error_text }) }}",
        "Shares record_run_error with the events workflow.",
    ),
}

FAQ = {
    "Fetch Recent Transcripts": lambda: sb_read(
        "Fetch Recent Transcripts", "agent_runs",
        "=started_at=gt.{{ new Date(Date.now() - 7*24*60*60*1000).toISOString() }}"
        "&transcript=not.is.null&order=started_at.desc",
        "transcript", limit=300,
    ),
    "Upsert faq_summary": lambda: sb_rpc(
        "Upsert faq_summary", "bump_faq",
        "={{ JSON.stringify({ p_question: $json.question, p_frequency: $json.frequency }) }}",
        "bump_faq does frequency = frequency + EXCLUDED.frequency in one statement. "
        "Get -> IF -> Update would lose increments under concurrency.",
    ),
}


def convert(fname, table):
    path = HERE / fname
    doc = json.loads(path.read_text(encoding="utf-8"))
    changed = []

    for i, node in enumerate(doc["nodes"]):
        builder = table.get(node["name"])
        if not builder or node["type"] != "n8n-nodes-base.postgres":
            continue
        new = builder()
        # carry over everything n8n keeps outside parameters
        new["id"] = node["id"]
        new["position"] = node["position"]
        for key in ("onError", "alwaysOutputData", "disabled"):
            if key in node:
                new[key] = node[key]
        new.update(RETRY)
        if "notes" in node and "notes" not in new:
            new["notes"] = node["notes"]
        doc["nodes"][i] = new
        changed.append(f"{node['name']} -> {new['type'].split('.')[-1]}")

    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def recompact(fname):
    """json.dumps explodes short arrays; put position/connection literals back."""
    path = HERE / fname
    txt = path.read_text(encoding="utf-8")
    txt = re.sub(r"\[\s*\n\s*(-?\d+),\s*\n\s*(-?\d+)\s*\n\s*\]", r"[\1, \2]", txt)
    txt = re.sub(
        r'\{\s*\n\s*"node": "([^"]+)",\s*\n\s*"type": "main",\s*\n\s*"index": (\d+)\s*\n\s*\}',
        r'{ "node": "\1", "type": "main", "index": \2 }', txt)
    path.write_text(txt, encoding="utf-8")


for fname, table in (("sales-agent-events.json", EVENTS),
                     ("sales-agent-error-handler.json", ERRH),
                     ("faq-clustering-nightly.json", FAQ)):
    print(f"\n{fname}")
    for line in convert(fname, table):
        print("  ", line)
    recompact(fname)
