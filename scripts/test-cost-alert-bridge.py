"""Tests for the cost alert bridge (commercial/20-cost-alert-bridge.yaml, ADR-023).

    python scripts/test-cost-alert-bridge.py

The function is inline in the template, so this loads that exact text and runs
it against a stub EventBridge client. What is tested is what deploys. The
messages are shaped like AWS's, with invented amounts.
"""
import json
import pathlib
import sys
import types
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "commercial" / "20-cost-alert-bridge.yaml"


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def load():
    t = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)
    code = t["Resources"]["BridgeFunction"]["Properties"]["Code"]["ZipFile"]
    sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda name: None))
    mod = types.ModuleType("bridge")
    exec(compile(code, str(TEMPLATE), "exec"), mod.__dict__)
    return mod


class StubEvents:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def put_events(self, Entries):
        self.sent.extend(Entries)
        if self.fail:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "x"}]}
        return {"FailedEntryCount": 0}


ANOMALY = json.dumps({
    "accountId": "111111111111", "anomalyId": "a1b2c3", "dimensionalValue": "oeight",
    "monitorName": "platform-tenant-cost", "anomalyStartDate": "2026-11-02",
    "impact": {"totalImpact": 1.0, "totalImpactPercentage": 40.0},
    "rootCauses": [{"service": "Amazon Elastic Compute Cloud - Compute"}],
    "anomalyDetailsLink": "https://console.aws.amazon.com/cost-management/home"})

BUDGET_TEXT = (
    "AWS Budget Notification November 02, 2026\nAWS Account 111111111111\n\n"
    "Dear AWS Customer,\n\nYou requested that we alert you when the ACTUAL Cost "
    "associated with your platform.oeight.oeight.arc8.pre-production budget is "
    "greater than 80.00% of 1.00 for the current month.")


def sns(subject, message):
    return {"Records": [{"Sns": {"Subject": subject, "Message": message, "MessageId": "m"}}]}


class Bridge(unittest.TestCase):
    def setUp(self):
        import os
        os.environ["ALERT_BUS_NAME"] = "platform-alerts"
        self.m = load()
        self.events = StubEvents()
        self.m._clients["events"] = self.events

    def detail(self, i=0):
        return json.loads(self.events.sent[i]["Detail"])

    def test_anomaly(self):
        self.m.handler(sns("AWS Cost Anomaly Detection", ANOMALY))
        d = self.detail()
        self.assertEqual(d["commercial_event"], "cost-anomaly")
        self.assertEqual(d["tenant"], "oeight")
        self.assertEqual(d["incident_id"], "cost-anomaly:a1b2c3")
        self.assertEqual(d["routing"], "digest")
        e = self.events.sent[0]
        self.assertEqual((e["Source"], e["DetailType"]), ("platform.cost", "Platform Incident Dossier"))

    def test_budget(self):
        self.m.handler(sns("AWS Budgets: platform.oeight.oeight.arc8.pre-production has exceeded your alert threshold", BUDGET_TEXT))
        d = self.detail()
        self.assertEqual(d["commercial_event"], "budget-breach")
        self.assertEqual(d["tenant"], "oeight-oeight-arc8")
        self.assertEqual(d["partner"], "oeight")
        self.assertEqual(d["tier_group"], "pre-production")
        self.assertTrue(d["incident_id"].startswith("budget-breach:platform.oeight.oeight.arc8.pre-production:"))

    def test_budget_named_only_in_subject(self):
        self.m.handler(sns("AWS Budgets: platform.p.c.a.production has exceeded", "no name here"))
        self.assertEqual(self.detail()["tier_group"], "production")

    def test_same_alert_same_month_dedups(self):
        self.m.handler(sns("s", BUDGET_TEXT))
        self.m.handler(sns("s", BUDGET_TEXT))
        self.assertEqual(self.detail(0)["incident_id"], self.detail(1)["incident_id"])

    def test_unrecognised_is_reported_not_commercial(self):
        self.m.handler(sns("Something else", "hello"))
        d = self.detail()
        self.assertNotIn("commercial_event", d)
        self.assertEqual(d["current_state"], "unrecognised")
        self.assertIn("Something else", d["assessment"]["probable_cause"])

    def test_never_pages(self):
        for s, msg in (("a", ANOMALY), ("b", BUDGET_TEXT), ("c", "x")):
            self.m.handler(sns(s, msg))
        for e in self.events.sent:
            self.assertEqual(json.loads(e["Detail"])["routing"], "digest")

    def test_put_failure_raises(self):
        self.m._clients["events"] = StubEvents(fail=True)
        with self.assertRaises(RuntimeError):
            self.m.handler(sns("a", ANOMALY))


if __name__ == "__main__":
    unittest.main(verbosity=1)
