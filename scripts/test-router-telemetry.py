#!/usr/bin/env python3
"""Tests for the router's handling of telemetry-gap findings (B-148).

    python scripts/test-router-telemetry.py

telemetry/src/gap.py emits `telemetry-gap` events in dossier shape. The router
had no normaliser for them, so every one went to the review queue as
"unrecognised event shape" and the digest could not read them as findings.

What these tests guard:
  - the event is recognised, and the account it is ABOUT (alarm.account_id)
    decides the tier, not the Audit envelope it arrived in;
  - the scan's declared class is honoured only as a downgrade, so a
    persistent gap in test tickets and never pages;
  - the routing record carries the reason, which the digest reads.

Stubs SSM, SQS and the clock; needs no AWS account.
"""

import datetime
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "alerting" / "src"))
# The router is packaged with instrumentation/src/eventspec.py (publish-router.sh).
sys.path.insert(0, str(ROOT / "instrumentation" / "src"))

import router  # noqa: E402

AUDIT = "692869638531"
DEV = "624606539840"
TEST = "754280127660"
PROD = "111111111111"
TIERS = {AUDIT: "prod", DEV: "dev", TEST: "test", PROD: "prod"}


class StubSSM:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": json.dumps(TIERS)}}


class StubSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, json.loads(MessageBody)))


def gap_event(account_id, routing, scenario="agent-stopped"):
    """As gap.py _emit builds it, delivered from Audit."""
    return {"source": "platform.telemetry", "detail-type": "telemetry-gap",
            "account": AUDIT, "region": "us-east-2",
            "time": "2026-10-14T09:15:00Z",
            "detail": {
                "record_type": "telemetry-gap-finding",
                "recorded_at": "2026-10-14T09:15:00+00:00",
                "current_state": "ALARM",
                "routing": routing,
                "alarm": {"name": f"platform-telemetry-{scenario}",
                          "resource_id": "i-0abc123def4567890",
                          "account_id": account_id},
                "assessment": {"probable_cause": "CloudWatch agent stopped publishing"},
                "telemetry_gap": {"scenario": scenario, "tier": TIERS.get(account_id)},
            }}


class TelemetryGaps(unittest.TestCase):

    def setUp(self):
        self._saved = (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
                       router.JIRA_QUEUE_URL, router.page)
        router.ssm, router.sqs = StubSSM(), StubSQS()
        router._now = lambda: datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc)
        router.REVIEW_QUEUE_URL, router.JIRA_QUEUE_URL = "review", "jira"
        router._TIER_CACHE.clear()
        self.paged = []
        router.page = lambda norm, routing, why: self.paged.append(routing) or {"status": 202}

    def tearDown(self):
        (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
         router.JIRA_QUEUE_URL, router.page) = self._saved
        router._TIER_CACHE.clear()

    def test_recognised_and_attributed_to_the_member_account(self):
        out = router.handler(gap_event(DEV, "digest"), None)
        self.assertEqual(out["record_type"], "routing-decision")
        self.assertNotIn("unrecognised", json.dumps(out))
        self.assertEqual(out["account_id"], DEV)
        self.assertEqual(out["environment_tier"], "dev")
        self.assertEqual(out["source"], "platform.telemetry")
        self.assertEqual(out["title"], "platform-telemetry-agent-stopped")
        self.assertEqual(out["resource"], "i-0abc123def4567890")

    def test_dev_gap_is_a_digest_line(self):
        out = router.handler(gap_event(DEV, "digest"), None)
        self.assertEqual(out["routing"], "digest")
        self.assertTrue(out["digest"])
        self.assertEqual(self.paged, [])

    def test_persistent_gap_in_test_tickets_and_never_pages(self):
        # gap.py declares pagerduty for any tier other than dev once the gap
        # outlives the association. Test does not page, so the router refuses.
        out = router.handler(gap_event(TEST, "pagerduty"), None)
        self.assertEqual(out["routing"], "digest_and_jira")
        self.assertIn("REFUSED", out["why"])
        self.assertEqual(self.paged, [])

    def test_persistent_gap_in_prod_pages(self):
        out = router.handler(gap_event(PROD, "pagerduty"), None)
        self.assertEqual(out["routing"], "pagerduty")
        self.assertEqual(self.paged, ["pagerduty"])

    def test_scan_incomplete_goes_to_review(self):
        out = router.handler(gap_event(DEV, "review_queue", "scan-incomplete"), None)
        self.assertEqual(out["routing"], "review_queue")
        self.assertFalse(out["digest"])

    def test_record_carries_the_reason_for_the_digest(self):
        out = router.handler(gap_event(DEV, "digest"), None)
        self.assertEqual(out["reason"], "CloudWatch agent stopped publishing")

    def test_long_reason_is_bounded(self):
        ev = gap_event(DEV, "digest")
        ev["detail"]["assessment"]["probable_cause"] = "x" * 5000
        out = router.handler(ev, None)
        self.assertEqual(len(out["reason"]), 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
