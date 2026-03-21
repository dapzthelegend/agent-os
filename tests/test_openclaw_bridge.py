from __future__ import annotations

import unittest

from agentic_os.openclaw_bridge import normalize_openclaw_daily_routine_payload


class OpenClawBridgeTests(unittest.TestCase):
    def test_grouped_payload_passthrough_normalization(self) -> None:
        payload = {
            "date": "2026-03-21",
            "timezone": "Europe/London",
            "recipient": "franchieinc@gmail.com",
            "delivery_time": "08:30",
            "calendar": {
                "events": [
                    {
                        "title": "Product sync",
                        "start": "2026-03-21T09:00:00+00:00",
                        "prep_needed": "Review notes",
                    }
                ],
                "constraints": ["Morning is busy"],
            },
            "personal_inbox": {
                "urgent": [{"subject": "Flight confirmation", "sender": "airline@example.com"}],
                "needs_reply": [{"subject": "Partnership intro", "actionable": True}],
                "important_fyi": [{"subject": "Monthly statement"}],
            },
            "agent_inbox": {
                "operational_items": [{"subject": "Cron hook pending", "actionable": True}],
                "alerts": [{"subject": "Notion sync warning"}],
            },
            "notion": {
                "blocked": [{"title": "Investigate flaky test", "blocked_reason": "Need logs"}],
            },
        }

        normalized = normalize_openclaw_daily_routine_payload(payload)
        self.assertEqual(normalized["date"], "2026-03-21")
        self.assertEqual(normalized["calendar"]["events"][0]["title"], "Product sync")
        self.assertEqual(normalized["calendar"]["events"][0]["prep_needed"], "Review notes")
        self.assertEqual(len(normalized["personal_inbox"]["urgent"]), 1)
        self.assertEqual(len(normalized["agent_inbox"]["operational_items"]), 1)
        self.assertEqual(len(normalized["notion"]["blocked"]), 1)

    def test_flat_payload_bucket_classification(self) -> None:
        payload = {
            "calendarSummary": {
                "items": [
                    {
                        "summary": "Roadmap review",
                        "start_time": "2026-03-21T10:00:00+00:00",
                        "requiredPrep": "Review roadmap doc",
                    }
                ],
                "notes": ["Block deep work after the meeting"],
            },
            "personalInboxSummary": {
                "messages": [
                    {
                        "subject": "Reply needed: legal review",
                        "category": "needs_reply",
                        "requestedAction": "Confirm legal timeline",
                    },
                    {
                        "subject": "FYI: statement ready",
                        "category": "fyi",
                    },
                ]
            },
            "agentInboxSummary": {
                "items": [
                    {"subject": "SLA alert", "bucket": "alert", "summary": "Error rate elevated"},
                    {
                        "subject": "Queue triage",
                        "category": "ops",
                        "requested_action": "Review new intake tickets",
                    },
                ]
            },
            "notionSummary": {
                "items": [
                    {"title": "Draft launch checklist", "status": "Inbox"},
                    {"title": "Approve dashboard scope", "status": "Review"},
                    {"title": "Fix CI issue", "status": "Blocked", "blockedReason": "Need logs"},
                    {"title": "Old task", "status": "Waiting"},
                ]
            },
        }

        normalized = normalize_openclaw_daily_routine_payload(payload)
        self.assertEqual(normalized["calendar"]["events"][0]["title"], "Roadmap review")
        self.assertEqual(len(normalized["personal_inbox"]["needs_reply"]), 1)
        self.assertEqual(len(normalized["personal_inbox"]["important_fyi"]), 1)
        self.assertEqual(len(normalized["agent_inbox"]["alerts"]), 1)
        self.assertEqual(len(normalized["agent_inbox"]["operational_items"]), 1)
        self.assertEqual(len(normalized["notion"]["inbox"]), 1)
        self.assertEqual(len(normalized["notion"]["review"]), 1)
        self.assertEqual(len(normalized["notion"]["blocked"]), 1)
        self.assertEqual(len(normalized["notion"]["stale"]), 1)
        self.assertTrue(normalized["notion"]["stale"][0]["stale"])


if __name__ == "__main__":
    unittest.main()
