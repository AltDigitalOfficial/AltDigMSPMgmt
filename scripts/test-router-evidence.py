#!/usr/bin/env python3
"""Tests for the router's handling of the evidence pipeline's own findings.

    python scripts/test-router-evidence.py

The evidence pipeline (collect, sender, completeness, custody) reports on
itself with `evidence-completeness` events. The router had no normaliser for
them, so every one landed in the review queue as "unrecognised event shape".
That buried "a tenant's evidence is going nowhere" from Arc8's vesting on
2026-10-01 until a runaway page found it on 2026-10-03 (B-156).

They arrive from Audit, which is prod-tier, so what these tests guard is the
downgrade: each emitter's declared class is honoured, and nothing pages.

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


class StubSSM:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": json.dumps({AUDIT: "prod"})}}


class StubSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, json.loads(MessageBody)))


def evidence_event(detail):
    """As EventBridge delivers it: the emitter's put_events, from Audit."""
    return {"source": "platform.evidence", "detail-type": "evidence-completeness",
            "account": AUDIT, "region": "us-east-2",
            "time": "2026-10-03T09:22:00Z", "detail": detail}


def sender_finding(name, routing="review_queue"):
    """evidence/src/sender.py _emit."""
    return {"record_type": "evidence-delivery-finding",
            "recorded_at": "2026-10-03T09:22:00+00:00", "current_state": "ALARM",
            "routing": routing,
            "alarm": {"name": name, "resource_id": "oeight-oeight"},
            "assessment": {"probable_cause": "no Truveon ingest key"},
            "evidence_delivery": {"tenant": "oeight-oeight"}}


class EvidenceFindings(unittest.TestCase):

    def setUp(self):
        self._saved = (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
                       router.JIRA_QUEUE_URL)
        router.ssm, router.sqs = StubSSM(), StubSQS()
        router._now = lambda: datetime.datetime(2026, 10, 4, tzinfo=datetime.timezone.utc)
        router.REVIEW_QUEUE_URL, router.JIRA_QUEUE_URL = "review", "jira"
        router._TIER_CACHE.clear()

        def no_paging(*a, **k):
            raise AssertionError("an evidence finding tried to page")
        self._page, router.page = router.page, no_paging

    def tearDown(self):
        (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
         router.JIRA_QUEUE_URL) = self._saved
        router.page = self._page
        router._TIER_CACHE.clear()

    def test_no_credential_is_recognised_and_named(self):
        out = router.handler(evidence_event(sender_finding("platform-evidence-no-credential")), None)
        self.assertEqual(out["routing"], "review_queue")
        self.assertEqual(out["title"], "platform-evidence-no-credential")
        self.assertNotIn("unrecognised", json.dumps(out))
        (url, msg), = router.sqs.sent
        self.assertEqual(url, "review")
        self.assertEqual(msg["event"]["kind"], "dossier")

    def test_every_declared_class_is_honoured_and_none_pages(self):
        for declared, queues in (("review_queue", {"review"}),
                                 ("digest", {"review"}),
                                 ("digest_and_jira", {"jira"})):
            with self.subTest(declared=declared):
                router.sqs.sent.clear()
                out = router.handler(evidence_event(sender_finding("x", declared)), None)
                self.assertEqual(out["routing"], declared)
                self.assertIn("Honoured as a downgrade", out["why"])
                self.assertEqual({u for u, _ in router.sqs.sent}, queues)

    def test_every_emitter_shape_is_recognised(self):
        # One per module, as each _emit builds it.
        shapes = {
            "collect": {"record_type": "evidence-collection-finding",
                        "current_state": "ALARM", "routing": "review_queue",
                        "alarm": {"name": "platform-evidence-unattributed-account",
                                  "resource_id": "904883674541"},
                        "assessment": {"probable_cause": "c"},
                        "evidence_collection": {"account_id": "904883674541"}},
            "completeness": {"record_type": "evidence-completeness-finding",
                             "recorded_at": "2026-10-03T09:22:00+00:00",
                             "current_state": "ALARM", "routing": "digest_and_jira",
                             "alarm": {"name": "platform-evidence-gap",
                                       "resource_id": "oeight-oeight#security-finding"},
                             "assessment": {"probable_cause": "c"}},
            "custody": {"record_type": "evidence-custody-finding",
                        "current_state": "ALARM", "routing": "digest",
                        "alarm": {"name": "platform-evidence-log-custody-failed",
                                  "resource_id": "evidence"},
                        "assessment": {"probable_cause": "c"}},
        }
        for emitter, detail in shapes.items():
            with self.subTest(emitter=emitter):
                out = router.handler(evidence_event(detail), None)
                self.assertEqual(out["record_type"], "routing-decision")
                self.assertEqual(out["source"], "platform.evidence")
                self.assertEqual(out["title"], detail["alarm"]["name"])

    def test_an_emitter_that_declares_nothing_escalates(self):
        # The else. A completeness finding with no declared class is routed by
        # Audit's tier, which pages: loud is the right way for this to fail.
        paged = []
        router.page = lambda norm, routing, why: paged.append(routing) or {"status": 202}
        detail = sender_finding("platform-evidence-undeclared")
        del detail["routing"]
        out = router.handler(evidence_event(detail), None)
        self.assertEqual(out["routing"], "pagerduty")
        self.assertEqual(paged, ["pagerduty"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
