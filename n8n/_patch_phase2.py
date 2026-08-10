"""Phase 2 workflow patches. Applies each edit to BOTH .local.json and the
base sibling, since nothing syncs them.

    python n8n/_patch_phase2.py [--check]

Idempotent: re-running detects already-applied edits and skips them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 2.2 -- conversation_ended is emitted by api/chat.py::end_conversation but has
# no Switch branch, so it lands on the "Unknown Event" fallback and reads like a
# bug in the execution log. It is expected traffic, so give it its own labelled
# terminal. It intentionally does NO work: api/store.py writes ended_at and
# duration_ms directly over SQL, so a workflow branch would be a second writer
# for columns that already have one.
CONV_RULE = {
    "conditions": {
        "options": {
            "caseSensitive": True,
            "leftValue": "",
            "typeValidation": "strict",
            "version": 2,
        },
        "conditions": [
            {
                "id": "sw-ended",
                "leftValue": "={{ $('Webhook').item.json.body.event_type }}",
                "rightValue": "conversation_ended",
                "operator": {"type": "string", "operation": "equals"},
            }
        ],
        "combinator": "and",
    },
    "renameOutput": True,
    "outputKey": "conversation_ended",
}

CONV_NODE = {
    "parameters": {},
    "id": "a1000000-0000-4000-8000-000000000042",
    "name": "Conversation Ended",
    "type": "n8n-nodes-base.noOp",
    "typeVersion": 1,
    "position": [740, 1040],
    "notes": (
        "Expected traffic, not an error -- api/chat.py emits this on widget "
        "unload. Deliberately does nothing: api/store.py already writes "
        "ended_at and duration_ms straight to agent_runs over SQL. A branch "
        "here would be a second writer for the same two columns. It exists so "
        "the execution log says 'Conversation Ended' instead of 'Unknown Event'."
    ),
}

# 2.3 -- website_url and industry are collected by save_lead and sent in
# fields.*, but no node read them, so they never reached AutoCRM. Appended to
# the note text rather than added as RPC params: widening mark_run_outcome()
# means dropping the old signature or PostgREST answers 300 "could not choose
# the best candidate function" and every write-back breaks at once (schema.sql).
NOTES_OLD = (
    "notes: ('AI sales agent conversation.\\n\\nScore: ' "
    "+ ($('Webhook').item.json.body.score ?? 'n/a') "
    "+ '\\nService discussed: ' "
    "+ ($('Webhook').item.json.body.fields.service_recommended ?? 'n/a') "
    "+ '\\nPages walked: '"
)
NOTES_NEW = (
    "notes: ('AI sales agent conversation.\\n\\nScore: ' "
    "+ ($('Webhook').item.json.body.score ?? 'n/a') "
    "+ '\\nService discussed: ' "
    "+ ($('Webhook').item.json.body.fields.service_recommended ?? 'n/a') "
    "+ '\\nWebsite: ' "
    "+ ($('Webhook').item.json.body.fields.website_url ?? 'not given') "
    "+ '\\nIndustry: ' "
    "+ ($('Webhook').item.json.body.fields.industry ?? 'not given') "
    "+ '\\nPages walked: '"
)

# 2.5 -- the lookup hard-coded the node name 'Webhook', which only exists in
# sales-agent-events. A failure escaping either cal workflow (whose triggers are
# 'Actions Webhook' and 'Cal Webhook') arrived unattributed. Scan every node's
# run data for a webhook-shaped body instead.
ERROR_CODE_NEW = """// The error trigger hands us the failed execution. Dig the session_id out of
// the original webhook payload so the dashboard can tie the failure to a run.
//
// Shape: { execution: { id, url, error, lastNodeExecuted, ... }, workflow: { id, name } }
const e = $input.first().json;

let sessionId = null;
try {
  // Do NOT hard-code 'Webhook'. That node name exists only in
  // sales-agent-events; cal-booking-actions calls its trigger 'Actions
  // Webhook' and cal-booking-events calls it 'Cal Webhook', so a failure in
  // either arrived with no session and only reached the ops email. Scan every
  // node's first run for a webhook-shaped body instead.
  const runData = e.execution?.data?.resultData?.runData ?? {};
  for (const nodeName of Object.keys(runData)) {
    const body = runData[nodeName]?.[0]?.data?.main?.[0]?.[0]?.json?.body;
    if (!body) continue;
    // sales-agent-events and cal-booking-actions both carry session_id at the
    // top level. Cal.com's own webhook nests ours under booking metadata.
    const found = body.session_id ?? body.payload?.metadata?.session_id ?? null;
    if (found) { sessionId = found; break; }
  }
} catch (_) {
  // fall through -- we still log the failure, just unattributed
}

const node = e.execution?.lastNodeExecuted ?? 'unknown node';
const message = e.execution?.error?.message ?? 'unknown error';
const workflow = e.workflow?.name ?? 'unknown workflow';

return [{
  json: {
    session_id: sessionId,
    workflow_name: workflow,
    error_text: `[n8n:${workflow}] ${node} failed: ${message}`,
    execution_url: e.execution?.url ?? null,
  },
}];"""


def load(name: str) -> tuple[Path, dict]:
    path = HERE / name
    return path, json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def patch_events(doc: dict) -> list[str]:
    done: list[str] = []
    names = {n["name"] for n in doc["nodes"]}

    # 2.2
    switch = next(n for n in doc["nodes"] if n["name"] == "Switch Event")
    rules = switch["parameters"]["rules"]["values"]
    if not any(r.get("outputKey") == "conversation_ended" for r in rules):
        rules.append(CONV_RULE)
        doc["nodes"].append(json.loads(json.dumps(CONV_NODE)))
        conns = doc["connections"]["Switch Event"]["main"]
        # The fallback output is always last, so insert before it.
        fallback = conns.pop()
        conns.append([{"node": "Conversation Ended", "type": "main", "index": 0}])
        conns.append(fallback)
        done.append("2.2 conversation_ended branch")

    # 2.3
    upsert = next(n for n in doc["nodes"] if n["name"] == "Upsert AutoCRM Lead")
    body = upsert["parameters"]["jsonBody"]
    if "website_url" not in body:
        if NOTES_OLD not in body:
            raise SystemExit("2.3: notes expression changed shape -- patch by hand")
        upsert["parameters"]["jsonBody"] = body.replace(NOTES_OLD, NOTES_NEW)
        done.append("2.3 website_url + industry in CRM note")

    return done


def patch_cal_events(doc: dict) -> list[str]:
    # 2.6 -- IF Cancelled routes BOTH outputs to AutoCRM Login, so it decides
    # nothing. The real cancel/reschedule split happens inside Update Prep
    # Task's body via Resolve Run's isCancel flag.
    if not any(n["name"] == "IF Cancelled" for n in doc["nodes"]):
        return []
    doc["nodes"] = [n for n in doc["nodes"] if n["name"] != "IF Cancelled"]
    doc["connections"].pop("IF Cancelled", None)
    for node_conns in doc["connections"].values():
        for output in node_conns.get("main", []):
            for conn in output:
                if conn["node"] == "IF Cancelled":
                    conn["node"] = "AutoCRM Login"
    return ["2.6 removed no-op IF Cancelled"]


def patch_error_handler(doc: dict) -> list[str]:
    node = next(n for n in doc["nodes"] if n["name"] == "Extract Failure Context")
    if "Do NOT hard-code" in node["parameters"]["jsCode"]:
        return []
    node["parameters"]["jsCode"] = ERROR_CODE_NEW
    # The subject line said "sales-agent-events failed" regardless of which
    # workflow actually failed.
    alert = next(n for n in doc["nodes"] if n["name"] == "Alert Ops")
    alert["parameters"]["subject"] = "=[n8n] {{ $json.workflow_name }} failed"
    return ["2.5 generalised webhook lookup + honest subject"]


PATCHES = [
    ("sales-agent-events", patch_events),
    ("cal-booking-events", patch_cal_events),
    ("sales-agent-error-handler", patch_error_handler),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report, do not write")
    args = parser.parse_args()

    total = 0
    for stem, fn in PATCHES:
        for suffix in (".local.json", ".json"):
            path, doc = load(stem + suffix)
            applied = fn(doc)
            if applied:
                total += len(applied)
                if not args.check:
                    save(path, doc)
                for line in applied:
                    print(f"  {path.name:38} {line}")
            else:
                print(f"  {path.name:38} (already applied)")
    print(f"\n{total} edit(s) {'pending' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
