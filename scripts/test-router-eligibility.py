#!/usr/bin/env python3
"""Tests for the router's handling of HIPAA eligibility events.

    python scripts/test-router-eligibility.py

docs/hipaa-eligibility.md §4.4. The sweep runs in Audit, a prod-tier account,
and reports on member accounts at every tier. What these tests guard:

  - Routing is by the tier of the account the detection is ABOUT (in the
    detail), never the envelope account. Read from the envelope, every dev
    detection would be judged as production.
  - The tier table: dev digest, test digest+Jira, uat Jira+commercial, prod
    pages only at HIGH or CRITICAL.
  - The emitter may downgrade and may not escalate, by rank: a digest cannot
    be declared up into a commercial ticket any more than into a page.

Stubs SSM, SQS, PagerDuty and the clock; needs no AWS account.
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
sys.path.insert(0, str(ROOT / "instrumentation" / "src"))

import router  # noqa: E402

AUDIT, DEV, TEST, UAT, PROD = ("692869638531", "624606539840", "904883674541",
                               "111111111111", "222222222222")
TIERS = {AUDIT: "prod", DEV: "dev", TEST: "test", UAT: "uat", PROD: "prod"}


class StubSSM:
    def get_parameter(self, Name):
        if Name == router.ACCOUNT_TIER_PARAM:
            return {"Parameter": {"Value": json.dumps(TIERS)}}
        raise KeyError(Name)


class StubSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, json.loads(MessageBody)))


def event(account, severity, routing=None, check="usage", subject="apprunner"):
    """As the sweep's put_events delivers it: envelope from Audit."""
    detail = {"framework": "hipaa", "check": check, "account_id": account,
              "subject": subject, "severity": severity, "summary": "s",
              "detected_at": "2026-10-04T11:30:00+00:00"}
    if routing:
        detail["routing"] = routing
    return {"source": "platform.eligibility", "detail-type": "hipaa-eligibility",
            "account": AUDIT, "region": "us-east-2",
            "time": "2026-10-04T11:30:00Z", "detail": detail}


class EligibilityRouting(unittest.TestCase):

    def setUp(self):
        self._saved = (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
                       router.JIRA_QUEUE_URL, router.page)
        router.ssm, router.sqs = StubSSM(), StubSQS()
        router._now = lambda: datetime.datetime(2026, 10, 4, tzinfo=datetime.timezone.utc)
        router.REVIEW_QUEUE_URL, router.JIRA_QUEUE_URL = "review", "jira"
        router._TIER_CACHE.clear()
        self.paged = []
        router.page = lambda norm, routing, why: self.paged.append(norm["account_id"]) or {"status": 202}

    def tearDown(self):
        (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
         router.JIRA_QUEUE_URL, router.page) = self._saved
        router._TIER_CACHE.clear()

    def decide(self, *a, **k):
        return router.decide(router.NORMALISERS["hipaa-eligibility"](event(*a, **k)))[0]

    def test_recognised_not_review_queue(self):
        out = router.handler(event(DEV, "MEDIUM"), None)
        self.assertNotIn("unrecognised", json.dumps(out))

    def test_tier_is_the_detected_account_not_the_envelope(self):
        self.assertEqual(self.decide(DEV, "HIGH"), "digest")
        self.assertEqual(self.paged, [])

    def test_tier_table(self):
        self.assertEqual(self.decide(DEV, "MEDIUM"), "digest")
        self.assertEqual(self.decide(TEST, "MEDIUM"), "digest_and_jira")
        self.assertEqual(self.decide(UAT, "MEDIUM"), "jira_and_commercial")
        self.assertEqual(self.decide(PROD, "MEDIUM"), "digest_and_jira")
        self.assertEqual(self.decide(PROD, "HIGH"), "pagerduty")
        self.assertEqual(self.decide(PROD, "CRITICAL"), "pagerduty")

    def test_downgrade_honoured(self):
        self.assertEqual(self.decide(PROD, "HIGH", routing="digest_and_jira"), "digest_and_jira")
        self.assertEqual(self.decide(UAT, "LOW", routing="digest"), "digest")

    def test_escalation_refused_by_rank(self):
        self.assertEqual(self.decide(DEV, "MEDIUM", routing="pagerduty"), "digest")
        self.assertEqual(self.decide(DEV, "MEDIUM", routing="jira_and_commercial"), "digest")
        self.assertEqual(self.decide(TEST, "MEDIUM", routing="jira_and_commercial"), "digest_and_jira")

    def test_unknown_declared_class_is_ignored(self):
        self.assertEqual(self.decide(PROD, "HIGH", routing="bogus"), "pagerduty")

    def test_upstream_check_from_audit_does_not_page(self):
        out = router.handler(event(AUDIT, "HIGH", routing="digest_and_jira",
                                   check="upstream", subject="removed: X"), None)
        self.assertEqual(out["routing"], "digest_and_jira")
        self.assertEqual(self.paged, [])

    def test_e8_critical_in_dev_or_test_is_ticketed_and_sent_to_the_tenant(self):
        for acct in (DEV, TEST):
            n = router.NORMALISERS["hipaa-eligibility"](event(acct, "CRITICAL"))
            self.assertEqual(router.decide(n)[0], "digest_and_jira")
            self.assertTrue(n.get("tenant_alert"))
        self.assertEqual(self.paged, [])

    def test_e8_does_not_apply_below_critical_or_in_prod(self):
        n = router.NORMALISERS["hipaa-eligibility"](event(DEV, "HIGH"))
        self.assertEqual(router.decide(n)[0], "digest")
        self.assertFalse(n.get("tenant_alert"))
        n = router.NORMALISERS["hipaa-eligibility"](event(PROD, "CRITICAL"))
        self.assertEqual(router.decide(n)[0], "pagerduty")
        self.assertFalse(n.get("tenant_alert"))

    def test_e8_publishes_addressed_by_tenant(self):
        published = []

        class SNS:
            def publish(self, **kw):
                published.append(kw)
        saved = (router._sns, router.TENANT_ALERT_TOPIC_ARN, router.tenant_facts)
        try:
            router._sns, router.TENANT_ALERT_TOPIC_ARN = SNS(), "arn:aws:sns:us-east-2:1:t"
            router.tenant_facts = lambda a: {"partner": "oeight", "client": "oeight"}
            out = router.handler(event(DEV, "CRITICAL", check="inventory",
                                       subject="AWS::AppRunner::Service/x"), None)
            self.assertEqual(out["delivered"]["tenant_alert"], {"sent": True, "tenant": "oeight-oeight"})
            (msg,) = published
            self.assertEqual(msg["MessageAttributes"]["tenant"]["StringValue"], "oeight-oeight")
            body = json.loads(msg["Message"])
            self.assertIn("not HIPAA-eligible", body["email"])
            self.assertEqual(json.loads(body["lambda"])["tenant"], "oeight-oeight")
            # No tenant record: not guessed at, and said so.
            router.tenant_facts = lambda a: None
            out = router.handler(event(DEV, "CRITICAL"), None)
            self.assertFalse(out["delivered"]["tenant_alert"]["sent"])
        finally:
            router._sns, router.TENANT_ALERT_TOPIC_ARN, router.tenant_facts = saved

    def test_one_incident_per_account_and_subject(self):
        a = router.NORMALISERS["hipaa-eligibility"](event(PROD, "HIGH"))
        b = router.NORMALISERS["hipaa-eligibility"](event(PROD, "HIGH"))
        c = router.NORMALISERS["hipaa-eligibility"](event(PROD, "HIGH", subject="lightsail"))
        self.assertEqual(router.dedup_key(a), router.dedup_key(b))
        self.assertNotEqual(router.dedup_key(a), router.dedup_key(c))


if __name__ == "__main__":
    unittest.main(verbosity=1)
