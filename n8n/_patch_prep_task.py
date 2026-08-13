"""Rewrite the Create Prep Task body -- both .local.json and its base sibling.

    python n8n/_patch_prep_task.py [--check]

The old body dumped Join / Reschedule / Cancel URLs at the rep. Those are the
VISITOR's controls; putting them on the rep's task invites the wrong person to
cancel the meeting. The rep needs one fact -- who, when, and how to reach them.

Idempotent: re-running detects the new body and skips.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

NEW_BODY = """={{ JSON.stringify({
  title: 'Meeting booked \\u2014 ' + ($('Meeting Details').item.json.visitorName || 'website visitor'),
  description:
    $('Meeting Details').item.json.visitorName + ' booked a meeting with you '
    + $('Meeting Details').item.json.whenLocal + '.\\n\\n'
    + 'Email: ' + $('Meeting Details').item.json.visitorEmail + '\\n'
    + ($('Meeting Details').item.json.visitorPhone
        ? 'Phone: ' + $('Meeting Details').item.json.visitorPhone + '\\n' : '')
    + ($('Meeting Details').item.json.companyName
        ? 'Company: ' + $('Meeting Details').item.json.companyName + '\\n' : '')
    + '\\nFor the join link, or to reschedule or cancel, see the booking in Cal.com.',
  entity_type: 'lead',
  entity_id: $('Meeting Details').item.json.crm_lead_id,
  due_at: $('Meeting Details').item.json.dueAt,
  priority: 'high',
  status: 'backlog',
  source: 'ai_sales_agent',
}) }}"""

# Meeting Details never exposed the phone, so the task could not show it even
# though agent_runs had it. Added to the node's return object.
PHONE_OLD = "    companyName:   b.fields?.company_name  ?? '',"
PHONE_NEW = (
    "    companyName:   b.fields?.company_name  ?? '',\n"
    "    // Surfaced for the prep task: a rep who cannot phone the visitor\n"
    "    // cannot rescue a no-show.\n"
    "    visitorPhone:  b.fields?.visitor_phone ?? '',"
)


def patch(doc: dict) -> list[str]:
    done: list[str] = []

    task = next(n for n in doc["nodes"] if n["name"] == "Create Prep Task")
    if "see the booking in Cal.com" not in task["parameters"]["jsonBody"]:
        task["parameters"]["jsonBody"] = NEW_BODY
        done.append("prep task body: who/when/contact, no URL dump")

    details = next(n for n in doc["nodes"] if n["name"] == "Meeting Details")
    code = details["parameters"]["jsCode"]
    if "visitorPhone" not in code:
        if PHONE_OLD not in code:
            raise SystemExit("Meeting Details changed shape -- patch by hand")
        details["parameters"]["jsCode"] = code.replace(PHONE_OLD, PHONE_NEW)
        done.append("Meeting Details exposes visitorPhone")

    return done


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report, do not write")
    args = parser.parse_args()

    total = 0
    for suffix in (".local.json", ".json"):
        path = HERE / f"sales-agent-events{suffix}"
        doc = json.loads(path.read_text(encoding="utf-8"))
        applied = patch(doc)
        if applied:
            total += len(applied)
            if not args.check:
                path.write_text(
                    json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            for line in applied:
                print(f"  {path.name:34} {line}")
        else:
            print(f"  {path.name:34} (already applied)")
    print(f"\n{total} edit(s) {'pending' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
