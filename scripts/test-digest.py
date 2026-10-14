#!/usr/bin/env python3
"""Tests for the digest facts gatherer and the manager digest render.

    python scripts/test-digest.py

docs/digest-design.md, B-171 stage A. What these guard:
  - the window: one 24-hour period ending at 12:00 UTC, the same for the
    gatherer and the digest;
  - repeats collapse (the telemetry scan re-emits a standing gap every fifteen
    minutes) and the latest record speaks for the line;
  - the layer-tag split: a tenant's untagged resource goes to the client, a
    platform-managed one does not;
  - the else paths: missing, unreadable and stale facts still produce an
    email that says what it is missing;
  - the 2026-10-10 Resource Explorer line renders as what, where and when.

Needs zoneinfo with a time zone database (on Windows: pip install tzdata).
Stubs every AWS client; needs no AWS account.
"""

import datetime
import io
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "reporting" / "src"))

import facts  # noqa: E402
import digest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

UTC = datetime.timezone.utc
DEV = "624606539840"
RE_INDEX = ("arn:aws:resource-explorer-2:us-east-2:624606539840:index/"
            "f4f465a2-c173-46c8-b365-9ef95c7686f5")


def at(y, m, d, h, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=UTC)


class Window(unittest.TestCase):

    def test_scheduled_run_covers_the_day_to_noon(self):
        start, end = facts.window(at(2026, 10, 15, 12, 15))
        self.assertEqual(end, at(2026, 10, 15, 12))
        self.assertEqual(start, at(2026, 10, 14, 12))

    def test_a_run_before_noon_covers_the_previous_window(self):
        _, end = facts.window(at(2026, 10, 15, 11, 0))
        self.assertEqual(end, at(2026, 10, 14, 12))

    def test_digest_at_13_reads_the_gatherers_window(self):
        self.assertEqual(facts.window(at(2026, 10, 15, 13))[1],
                         facts.window(at(2026, 10, 15, 12, 15))[1])

    def test_key_layout(self):
        self.assertEqual(facts.facts_key(at(2026, 10, 15, 12), DEV),
                         f"facts/2026-10-15/{DEV}.json")


def decision(source, title, resource, recorded_at, **kw):
    return {"record_type": "routing-decision", "digest": True, "account_id": DEV,
            "source": source, "title": title, "resource": resource,
            "recorded_at": recorded_at, **kw}


class GroupFindings(unittest.TestCase):

    def test_repeats_collapse_and_latest_wins(self):
        recs = [decision("platform.telemetry", "platform-telemetry-agent-stopped", "i-1",
                         f"2026-10-15T0{h}:00:00+00:00", routing="digest", reason=f"r{h}")
                for h in (1, 3, 2)]
        out = facts.group_findings(recs)[DEV]
        (f,) = out["findings"]
        self.assertEqual(f["count"], 3)
        self.assertEqual(f["first_seen"], "2026-10-15T01:00:00+00:00")
        self.assertEqual(f["last_seen"], "2026-10-15T03:00:00+00:00")
        self.assertEqual(f["reason"], "r3")

    def test_other_sources_are_counted_not_listed(self):
        recs = [decision("securityhub", "x", "y", "2026-10-15T01:00:00+00:00")] * 4
        out = facts.group_findings(recs)[DEV]
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["other_sources"], {"securityhub": 4})


def row(rtype, rid, tags, status="OK"):
    return {"accountId": DEV, "awsRegion": "us-east-2", "resourceType": rtype,
            "resourceId": rid, "resourceName": rid, "configurationItemStatus": status,
            "tags": [{"key": k, "value": v} for k, v in tags.items()]}


class LayerTags(unittest.TestCase):

    def test_split_by_owner_and_problem(self):
        rows = [
            row("AWS::S3::Bucket", "tagged", {"platform-layer": "application"}),
            row("AWS::S3::Bucket", "upper", {"platform-layer": "Application"}),
            row("AWS::S3::Bucket", "untagged", {}),
            row("AWS::S3::Bucket", "bad", {"platform-layer": "app"}),
            row("AWS::Lambda::Function", "ours", {"platform-managed": "true"}),
            row("AWS::S3::Bucket", "gone", {}, status="ResourceDeleted"),
        ]
        out = facts.classify_layer(rows)[DEV]
        self.assertEqual(out["examined"], 5)
        self.assertEqual([(e["resource_id"], e["problem"]) for e in out["tenant"]],
                         [("bad", "invalid"), ("untagged", "missing")])
        self.assertEqual([e["resource_id"] for e in out["platform"]], ["ours"])


class Naming(unittest.TestCase):

    def test_type_labels(self):
        self.assertEqual(digest.type_label("AWS::ResourceExplorer2::Index"),
                         "Resource Explorer index")
        self.assertEqual(digest.type_label("AWS::S3::Bucket"), "S3 bucket")
        self.assertEqual(digest.type_label("AWS::RDS::DBInstance"), "RDS db instance")

    def test_arn_split(self):
        self.assertEqual(digest.split_resource(RE_INDEX),
                         ("f4f465a2-c173-46c8-b365-9ef95c7686f5", "us-east-2"))
        self.assertEqual(digest.split_resource("i-123"), ("i-123", None))


ACCOUNT = {"account_id": DEV, "partner": "oeight", "client": "oeight",
           "app": "arc8", "environment": "dev", "kind": "tenant", "state": "active"}
START, END = at(2026, 10, 14, 12), at(2026, 10, 15, 12)
DATA = {"alarms_total": 5, "alarms_new": [], "alarms_in_alarm": [],
        "exceptions": [{"reason": "unrecognised-resource-type",
                        "resource_type": "AWS::ResourceExplorer2::Index",
                        "resource_id": RE_INDEX, "alarm_id": None,
                        "recorded_at": "2026-10-09T17:02:06Z"}]}


def doc(**over):
    d = {"window": {"start": "2026-10-14T12:00:00Z", "end": "2026-10-15T12:00:00Z"},
         "sources": {"router": "ok", "layer": "ok", "trail": "not-built"},
         "routed_findings": [], "layer_tag": {"tenant": {"total": 0, "items": []},
                                              "platform": {"total": 0, "items": []}}}
    d.update(over)
    return d


def render(d, status="ok", zone="America/Denver"):
    tz, name, note = digest.load_zone(zone)
    return digest.render(ACCOUNT, DATA, d, status, START, END, tz, name, note)


class Render(unittest.TestCase):

    def test_mountain_by_default_with_utc_beside_it(self):
        body = render(doc())
        self.assertIn("2026-10-15 06:00 MDT (12:00 UTC)", body)
        self.assertIn("Times are shown in America/Denver.", body)

    def test_bad_zone_falls_back_to_utc_and_says_so(self):
        body = render(doc(), zone="Mars/Olympus")
        self.assertIn("Times are UTC: the time zone on record (Mars/Olympus)", body)

    def test_resource_explorer_line(self):
        body = render(doc())
        self.assertIn("Resource Explorer index (AWS::ResourceExplorer2::Index), us-east-2", body)
        self.assertIn("f4f465a2-c173-46c8-b365-9ef95c7686f5", body)
        self.assertIn("Seen 2026-10-09 11:02 MDT", body)
        self.assertIn("no action is needed from you", body)
        self.assertNotIn("arn:aws:resource-explorer-2", body)

    def test_quiet_day_says_so(self):
        self.assertIn("Nothing needs your attention.", render(doc()))

    def test_missing_facts_still_render_monitoring(self):
        for status in ("missing", "unreadable", "stale", "unconfigured"):
            with self.subTest(status=status):
                body = render(None, status)
                self.assertIn("Not available for this window", body)
                self.assertIn("Total platform-managed alarms in this account: 5", body)
                self.assertNotIn("Nothing needs your attention.", body)

    def test_failed_source_is_not_a_quiet_day(self):
        body = render(doc(sources={"router": "error: x", "layer": "ok"}))
        self.assertIn("could not be read today", body)
        self.assertNotIn("Nothing needs your attention.", body)

    def test_findings_and_layer_lines(self):
        d = doc(routed_findings=[{
                    "source": "platform.eligibility",
                    "title": "HIPAA eligibility: usage — apprunner",
                    "reason": "`apprunner` used in arc8-dev (dev). Not HIPAA-eligible.",
                    "count": 1, "last_seen": "2026-10-15T11:30:00+00:00"}],
                layer_tag={"tenant": {"total": 1, "items": [{
                    "resource_type": "AWS::S3::Bucket", "resource_id": "arc8-uploads",
                    "name": "arc8-uploads", "region": "us-east-2", "problem": "missing"}]},
                    "platform": {"total": 0, "items": []}})
        body = render(d)
        self.assertIn("HIPAA eligibility (1):", body)
        self.assertIn("Not HIPAA-eligible.", body)
        self.assertIn("2026-10-15 05:30 MDT", body)
        self.assertIn("S3 bucket arc8-uploads, us-east-2 — tag missing", body)
        self.assertIn("page AltDigital rather than your team", body)

    def test_team_section_says_not_available_yet(self):
        self.assertIn("Not available yet.", render(doc()))


class StubS3:
    def __init__(self, objects=None, error=None):
        self.objects, self.error = objects or {}, error

    def get_object(self, Bucket, Key):
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "GetObject")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


class ReadFacts(unittest.TestCase):

    def setUp(self):
        self._saved = (digest.s3, digest.FACTS_BUCKET)
        digest.FACTS_BUCKET = "altdig-digest-facts-692869638531"

    def tearDown(self):
        digest.s3, digest.FACTS_BUCKET = self._saved

    def test_statuses(self):
        key = facts.facts_key(END, DEV)
        cases = [
            (StubS3({key: doc()}), "ok"),
            (StubS3({}), "missing"),
            (StubS3(error="AccessDenied"), "unreadable"),
            (StubS3({key: doc(window={"end": "2026-10-14T12:00:00Z"})}), "stale"),
        ]
        for stub, expected in cases:
            with self.subTest(expected=expected):
                digest.s3 = stub
                self.assertEqual(digest.read_facts(DEV, END)[1], expected)

    def test_unconfigured(self):
        digest.FACTS_BUCKET = ""
        self.assertEqual(digest.read_facts(DEV, END), (None, "unconfigured"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
