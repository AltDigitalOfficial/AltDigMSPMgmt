"""Tests for the re-issue reminder (security/90-secret-age.yaml, ADR-020).

    python scripts/test-secret-age.py

The function is inline in the template, so this loads that exact text and runs
it against stub AWS clients. What is tested is what deploys.
"""
import datetime
import json
import pathlib
import sys
import types
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "security" / "90-secret-age.yaml"


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def load():
    t = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)
    code = t["Resources"]["SecretAgeFunction"]["Properties"]["Code"]["ZipFile"]
    sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda name: None))
    mod = types.ModuleType("secret_age")
    exec(compile(code, str(TEMPLATE), "exec"), mod.__dict__)
    return mod


class StubSM:
    def __init__(self, pages):
        self.pages = pages

    def list_secrets(self, NextToken=None):
        i = int(NextToken or 0)
        page = {"SecretList": self.pages[i]}
        if i + 1 < len(self.pages):
            page["NextToken"] = str(i + 1)
        return page


class StubEvents:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def put_events(self, Entries):
        self.sent.extend(Entries)
        if self.fail:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "x"}]}
        return {"FailedEntryCount": 0}


def secret(name, **tags):
    return {"Name": name, "ARN": f"arn:aws:secretsmanager:us-east-2:111111111111:secret:{name}-abc",
            "Tags": [{"Key": k.replace("_", "-"), "Value": v} for k, v in tags.items()]}


TUESDAY = datetime.date(2026, 11, 3)
MONDAY = datetime.date(2026, 11, 2)


class Assess(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def kind(self, tags, today=TUESDAY):
        return self.m.assess("s", tags, today)[:2]

    def test_reminder_days(self):
        # issued 2025-11-03 + 365 = due 2026-11-03
        base = {"platform-max-age-days": "365"}
        for left in (30, 14, 7, 1):
            issued = (TUESDAY + datetime.timedelta(days=left) - datetime.timedelta(days=365)).isoformat()
            self.assertEqual(self.kind({**base, "platform-issued": issued}),
                             (f"due-in-{left}", "digest_and_jira"))

    def test_between_reminders_is_quiet(self):
        issued = (TUESDAY + datetime.timedelta(days=20) - datetime.timedelta(days=365)).isoformat()
        self.assertEqual(self.kind({"platform-max-age-days": "365", "platform-issued": issued}),
                         ("ok", None))

    def test_overdue_on_the_day_then_weekly(self):
        for over, want in ((0, "digest_and_jira"), (3, None), (7, "digest_and_jira"), (14, "digest_and_jira")):
            issued = (TUESDAY - datetime.timedelta(days=over) - datetime.timedelta(days=90)).isoformat()
            self.assertEqual(self.kind({"platform-max-age-days": "90", "platform-issued": issued})[1], want, over)

    def test_none_is_a_decision(self):
        self.assertEqual(self.kind({"platform-max-age-days": "none"}, MONDAY),
                         ("no-cadence-by-decision", None))

    def test_untagged_raises_on_mondays_only(self):
        self.assertEqual(self.kind({}, MONDAY), ("untagged", "digest"))
        self.assertEqual(self.kind({}, TUESDAY), ("untagged", None))

    def test_missing_issue_date(self):
        self.assertEqual(self.kind({"platform-max-age-days": "365"}, MONDAY), ("no-issue-date", "digest"))

    def test_bad_tags(self):
        self.assertEqual(self.kind({"platform-max-age-days": "a year"}, MONDAY), ("bad-tag", "digest"))
        self.assertEqual(self.kind({"platform-max-age-days": "0"}, MONDAY), ("bad-tag", "digest"))
        self.assertEqual(self.kind({"platform-max-age-days": "365", "platform-issued": "Sept 19"}, MONDAY),
                         ("bad-tag", "digest"))


class Handler(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.events = StubEvents()
        import os
        os.environ["ALERT_BUS_NAME"] = "platform-alerts"

    def run_with(self, pages, today=MONDAY, events=None):
        events = events or self.events
        self.m._clients.update({"secretsmanager": StubSM(pages), "events": events})
        return self.m.handler(today=today)

    def test_pages_and_dossier_shape(self):
        due = (MONDAY + datetime.timedelta(days=7) - datetime.timedelta(days=365)).isoformat()
        out = self.run_with([
            [secret("platform/jira/service-account", platform_max_age_days="365",
                    platform_issued=due, platform_owner="platform-operations")],
            [secret("platform/pagerduty/routing-key", platform_max_age_days="none"),
             secret("platform/new/thing")],
        ])
        self.assertEqual(out["raised"], 2)
        self.assertEqual(len(out["secrets"]), 3)
        sent = [json.loads(e["Detail"]) for e in self.events.sent]
        self.assertTrue(all(e["Source"] == "platform.secrets" for e in self.events.sent))
        self.assertTrue(all(e["DetailType"] == "Platform Incident Dossier" for e in self.events.sent))
        reissue = [d for d in sent if d["routing"] == "digest_and_jira"][0]
        self.assertIn("Re-issue credential: platform/jira/service-account", reissue["alarm"]["name"])
        self.assertIn("Owner: platform-operations", reissue["assessment"]["probable_cause"])
        self.assertEqual(reissue["alarm"]["account_id"], "111111111111")
        hygiene = [d for d in sent if d["routing"] == "digest"][0]
        self.assertIn("platform/new/thing", hygiene["alarm"]["name"])

    def test_never_escalates(self):
        due = (MONDAY - datetime.timedelta(days=365)).isoformat()
        self.run_with([[secret("x", platform_max_age_days="365", platform_issued=due), secret("y")]])
        for e in self.events.sent:
            self.assertIn(json.loads(e["Detail"])["routing"], ("digest", "digest_and_jira"))

    def test_put_failure_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_with([[secret("y")]], events=StubEvents(fail=True))

    def test_batches_of_ten(self):
        self.run_with([[secret(f"s{i}") for i in range(23)]])
        self.assertEqual(len(self.events.sent), 23)


class StubSNS:
    def __init__(self, subs=("arn:aws:sns:us-east-2:111111111111:platform-owner-platform-operations:x",),
                 missing=False):
        self.subs, self.missing, self.published = subs, missing, []

    def list_subscriptions_by_topic(self, TopicArn):
        if self.missing:
            raise type("NotFoundException", (Exception,), {})("Topic does not exist")
        return {"Subscriptions": [{"SubscriptionArn": s} for s in self.subs]}

    def publish(self, TopicArn, Subject, Message):
        self.published.append({"TopicArn": TopicArn, "Subject": Subject, "Message": Message})


FIRST = datetime.date(2026, 12, 1)


class Calendar(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.events = StubEvents()
        import os
        os.environ["ALERT_BUS_NAME"] = "platform-alerts"
        os.environ["OWNER_TOPIC_PREFIX"] = "arn:aws:sns:us-east-2:111111111111:platform-owner-"

    def run_with(self, pages, sns=None):
        self.sns = sns or StubSNS()
        self.m._clients.update({"secretsmanager": StubSM(pages), "events": self.events, "sns": self.sns})
        return self.m.handler({"mode": "monthly"}, today=FIRST)

    def test_months_until(self):
        mu = self.m.months_until
        self.assertEqual(mu(FIRST, datetime.date(2027, 9, 19)), 9)
        self.assertEqual(mu(FIRST, datetime.date(2027, 12, 1)), 12)
        self.assertEqual(mu(FIRST, datetime.date(2026, 12, 31)), 0)

    def test_lines_use_jamies_wording(self):
        line = self.m.calendar_line("Jira", datetime.date(2027, 9, 19), FIRST)
        self.assertEqual(line, "You have 9 months until 19 September 2027, on which date "
                               "the API key for Jira needs to be cycled.")
        self.assertIn("1 month until", self.m.calendar_line("X", datetime.date(2027, 1, 5), FIRST))
        self.assertIn("less than a month", self.m.calendar_line("X", datetime.date(2026, 12, 20), FIRST))
        self.assertIn("3 days ago", self.m.calendar_line("X", datetime.date(2026, 11, 28), FIRST))

    def test_one_email_per_owner_sorted_by_due(self):
        out = self.run_with([[
            secret("platform/pagerduty/api-token", platform_max_age_days="365",
                   platform_issued="2026-09-21", platform_owner="platform-operations",
                   platform_tool="PagerDuty REST API"),
            secret("platform/jira/service-account", platform_max_age_days="365",
                   platform_issued="2026-09-19", platform_owner="platform-operations"),
            secret("platform/pagerduty/routing-key", platform_max_age_days="none"),
        ]])
        self.assertEqual(len(self.sns.published), 1)
        msg = self.sns.published[0]
        self.assertTrue(msg["TopicArn"].endswith(":platform-owner-platform-operations"))
        self.assertEqual(msg["Subject"], "Credential calendar: next re-issue 2027-09-19")
        body = msg["Message"]
        self.assertLess(body.index("platform/jira/service-account"), body.index("PagerDuty REST API"))
        self.assertIn("the API key for platform/jira/service-account", body)   # no tool tag: name
        self.assertEqual(out["raised"], 0)
        self.assertEqual(self.events.sent, [])

    def test_unowned_cadence_goes_to_digest(self):
        self.run_with([[secret("t", platform_max_age_days="365", platform_issued="2026-09-19")]])
        self.assertEqual(self.sns.published, [])
        d = json.loads(self.events.sent[0]["Detail"])
        self.assertEqual((d["routing"], d["current_state"]), ("digest", "no-owner"))

    def test_unconfirmed_or_missing_mailbox_is_reported_not_published(self):
        rows = [[secret("t", platform_max_age_days="365", platform_issued="2026-09-19",
                        platform_owner="platform-operations")]]
        for sns in (StubSNS(subs=("PendingConfirmation",)), StubSNS(missing=True)):
            self.events.sent.clear()
            self.run_with(rows, sns=sns)
            self.assertEqual(sns.published, [])
            d = json.loads(self.events.sent[0]["Detail"])
            self.assertEqual((d["routing"], d["current_state"]), ("digest", "owner-mailbox"))
            self.assertIn("platform-owner-platform-operations", d["alarm"]["name"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
