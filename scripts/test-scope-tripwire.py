"""Tests for the cardholder scope tripwire (security/70-scope-tripwire.yaml, ADR-017).

    python scripts/test-scope-tripwire.py

The function is inline in the template, so this loads that exact text and runs
it against stub AWS clients. What is tested is what deploys.
"""
import json
import os
import pathlib
import sys
import types
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "security" / "70-scope-tripwire.yaml"


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def load_tripwire():
    t = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)
    code = t["Resources"]["TripwireFunction"]["Properties"]["Code"]["ZipFile"]
    # boto3 is replaced before the module body runs; the function only ever
    # reaches AWS through _client().
    sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda name: None))
    mod = types.ModuleType("tripwire")
    exec(compile(code, str(TEMPLATE), "exec"), mod.__dict__)
    return mod


class StubSSM:
    def __init__(self, tiers=None, fail=False):
        self.tiers, self.fail = tiers or {}, fail

    def get_parameter(self, Name):
        if self.fail:
            raise RuntimeError("no access")
        return {"Parameter": {"Value": json.dumps(self.tiers)}}


class StubEvents:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def put_events(self, Entries):
        self.sent.extend(Entries)
        if self.fail:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "x"}]}
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}


def finding(detections, account="111111111111", sample=False, ftype="SensitiveData:S3Object/Financial"):
    return {"detail": {
        "id": "f-1", "type": ftype, "accountId": account, "region": "us-east-2",
        "sample": sample,
        "resourcesAffected": {"s3Bucket": {"name": "tenant-bucket"},
                              "s3Object": {"key": "exports/2026/report.csv"}},
        "classificationDetails": {
            "originType": "AUTOMATED_SENSITIVE_DATA_DISCOVERY",
            "result": {"sensitiveData": [
                {"category": "FINANCIAL_INFORMATION",
                 "detections": [{"type": t, "count": n} for t, n in detections]}]}},
    }}


class Tripwire(unittest.TestCase):

    def setUp(self):
        self.m = load_tripwire()
        os.environ.update(ALERT_BUS_NAME="platform-alerts",
                          ACCOUNT_TIER_PARAM="/platform/routing/account-tiers",
                          SCOPE_CHANGE_TIERS="dev,prod,test,uat")
        self.events = StubEvents()
        self.ssm = StubSSM({"111111111111": "dev", "222222222222": "prod"})
        self.m._clients.update(events=self.events, ssm=self.ssm)

    def sent(self):
        self.assertEqual(len(self.events.sent), 1)
        e = self.events.sent[0]
        self.assertEqual(e["Source"], "platform.scope")
        self.assertEqual(e["DetailType"], "Platform Incident Dossier")
        return json.loads(e["Detail"])

    def test_card_number_in_dev_opens_a_conversation_and_a_ticket(self):
        out = self.m.handler(finding([("CREDIT_CARD_NUMBER", 3)]))
        self.assertTrue(out["raised"])
        d = self.sent()
        self.assertEqual(d["commercial_event"], "scope-change")
        self.assertEqual(d["routing"], "digest_and_jira")
        self.assertEqual(d["macie"]["detections"], {"CREDIT_CARD_NUMBER": 3})
        self.assertEqual(d["alarm"]["account_id"], "111111111111")

    def test_prod_never_declares_a_page(self):
        self.m.handler(finding([("CREDIT_CARD_NUMBER_(NO_KEYWORD)", 1)], account="222222222222"))
        d = self.sent()
        self.assertEqual(d["routing"], "digest_and_jira")
        self.assertEqual(d["alarm"]["environment_tier"], "prod")

    def test_expiry_alone_is_not_cardholder_data(self):
        out = self.m.handler(finding([("CREDIT_CARD_EXPIRATION", 12)]))
        self.assertFalse(out["raised"])
        self.assertEqual(self.events.sent, [])

    def test_bank_account_alone_is_not_pci_scope(self):
        out = self.m.handler(finding([("BANK_ACCOUNT_NUMBER", 5)]))
        self.assertFalse(out["raised"])

    def test_card_data_alongside_other_categories_is_found(self):
        f = finding([("CREDIT_CARD_SECURITY_CODE", 1)], ftype="SensitiveData:S3Object/Multiple")
        f["detail"]["classificationDetails"]["result"]["sensitiveData"].insert(
            0, {"category": "PERSONAL_INFORMATION", "detections": [{"type": "NAME", "count": 9}]})
        self.m.handler(f)
        self.assertEqual(self.sent()["macie"]["detections"], {"CREDIT_CARD_SECURITY_CODE": 1})

    def test_sample_finding_is_recorded_without_a_conversation(self):
        self.m.handler(finding([("CREDIT_CARD_NUMBER", 1)], sample=True))
        d = self.sent()
        self.assertNotIn("commercial_event", d)
        self.assertEqual(d["routing"], "digest")
        self.assertTrue(d["alarm"]["name"].startswith("[SAMPLE]"))

    def test_tier_outside_scope_records_without_a_conversation(self):
        os.environ["SCOPE_CHANGE_TIERS"] = "uat,prod"
        self.m.handler(finding([("CREDIT_CARD_NUMBER", 1)]))
        d = self.sent()
        self.assertNotIn("commercial_event", d)
        self.assertEqual(d["routing"], "digest")
        self.assertIn("false for tier dev", d["routing_reason"])

    def test_account_missing_from_tier_map_is_treated_as_in_scope(self):
        self.m.handler(finding([("CREDIT_CARD_NUMBER", 1)], account="999999999999"))
        d = self.sent()
        self.assertEqual(d["commercial_event"], "scope-change")
        self.assertIn("not in the tier map", d["routing_reason"])

    def test_unreadable_tier_map_is_treated_as_in_scope(self):
        self.m._clients["ssm"] = StubSSM(fail=True)
        self.m.handler(finding([("CREDIT_CARD_NUMBER", 1)]))
        self.assertEqual(self.sent()["commercial_event"], "scope-change")

    def test_failed_put_raises_so_the_errors_alarm_fires(self):
        self.m._clients["events"] = StubEvents(fail=True)
        with self.assertRaises(RuntimeError):
            self.m.handler(finding([("CREDIT_CARD_NUMBER", 1)]))

    def test_no_detection_values_leave_the_function(self):
        # Macie findings carry types and counts. If one ever carried a value,
        # nothing here would copy it: only these keys are read.
        f = finding([("CREDIT_CARD_NUMBER", 1)])
        f["detail"]["classificationDetails"]["result"]["sensitiveData"][0]["detections"][0][
            "occurrences"] = {"cells": [{"value": "4111111111111111"}]}
        self.m.handler(f)
        self.assertNotIn("4111", self.events.sent[0]["Detail"])

    def test_dossier_matches_what_the_router_reads(self):
        # alerting/src/router.py from_dossier reads these; a renamed field
        # would route the event with no account and no tier.
        self.m.handler(finding([("CREDIT_CARD_NUMBER", 1)]))
        d = self.sent()
        for k in ("account_id", "resource_id", "name", "environment_tier"):
            self.assertIn(k, d["alarm"])
        self.assertIn("probable_cause", d["assessment"])


class TierModel(unittest.TestCase):

    def test_every_tier_treats_cardholder_data_as_scope_change(self):
        # The decision of 2026-09-25. If a tier is ever set false, this fails
        # and the change has to be made on purpose, with this test.
        sys.path.insert(0, str(ROOT / "scripts" / "lib"))
        import tiers  # noqa: E402
        model = tiers.parse(str(ROOT / "config" / "tiers.yaml"))
        for t in model["tiers"]:
            self.assertIs(tiers.resolve(model, t)["settings"]["cardholder_data_is_scope_change"],
                          True, t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
