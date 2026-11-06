#!/usr/bin/env python3
"""Tests for log custody's sources (evidence/src/custody.py, B-134).

    python scripts/test-custody.py

Stubs S3; needs no AWS account. Covers the flow-log source added on
2026-11-06 and the layouts it must not be confused with.
"""

import datetime
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evidence" / "src"))

import custody  # noqa: E402

UTC = datetime.timezone.utc
ACCOUNT = "624606539840"
HOUR = datetime.datetime(2026, 10, 31, 19, tzinfo=UTC)


def obj(key, size=100):
    return {"Key": key, "Size": size, "ETag": f'"{abs(hash(key)) % 10**8}"',
            "LastModified": HOUR + datetime.timedelta(minutes=20)}


class StubS3:
    """list_objects_v2 by exact prefix, as the paginator calls it."""

    def __init__(self, objects):
        self.objects, self.prefixes = objects, []

    def get_paginator(self, name):
        stub = self

        class Pages:
            def paginate(self, Bucket, Prefix):
                stub.prefixes.append((Bucket, Prefix))
                yield {"Contents": [o for o in stub.objects if o["Key"].startswith(Prefix)]}
        return Pages()


def flow_key(stamp, region="us-east-2", day="2026/10/31"):
    return (f"AWSLogs/{ACCOUNT}/vpcflowlogs/{region}/{day}/"
            f"{ACCOUNT}_vpcflowlogs_{region}_fl-01ec5286b7b198782_{stamp}_849acceb.log.gz")


class FlowLogPrefix(unittest.TestCase):

    def test_padded_day_and_no_organization_id(self):
        hour = datetime.datetime(2026, 3, 5, 7, tzinfo=UTC)
        self.assertEqual(custody._prefixes("flowlogs", ACCOUNT, "us-east-2", hour),
                         [f"AWSLogs/{ACCOUNT}/vpcflowlogs/us-east-2/2026/03/05/"])

    def test_config_layout_unchanged(self):
        hour = datetime.datetime(2026, 3, 5, 7, tzinfo=UTC)
        self.assertEqual(custody._prefixes("config", ACCOUNT, "us-east-2", hour),
                         [f"AWSLogs/{ACCOUNT}/Config/us-east-2/2026/3/5/"])

    def test_flow_log_name_gives_its_own_hour(self):
        key = flow_key("20261031T1955Z")
        self.assertEqual(custody._hour_of(key, None), HOUR)


class FlowLogObserve(unittest.TestCase):

    def setUp(self):
        self.saved = (custody.s3, custody.FLOW_LOG_BUCKET, custody.REGIONS)
        custody.FLOW_LOG_BUCKET = "altdig-flow-logs-test"
        custody.REGIONS = ["us-east-1", "us-east-2", "us-west-2"]

    def tearDown(self):
        custody.s3, custody.FLOW_LOG_BUCKET, custody.REGIONS = self.saved

    def test_counts_only_the_hour(self):
        custody.s3 = StubS3([
            obj(flow_key("20261031T1910Z"), 460),
            obj(flow_key("20261031T1955Z"), 1357),
            obj(flow_key("20261031T2005Z"), 999),     # next hour: excluded
        ])
        p = custody.observe("flowlogs", ACCOUNT, HOUR)
        self.assertEqual(p["objects"], 2)
        self.assertEqual(p["bytes"], 460 + 1357)
        self.assertEqual(p["regions"]["us-east-2"]["objects"], 2)
        self.assertEqual(p["regions"]["us-west-2"]["objects"], 0)
        self.assertEqual(p["bucket"], "altdig-flow-logs-test")
        self.assertIs(p["immutable"], False)

    def test_a_quiet_hour_is_recorded_not_raised(self):
        custody.s3 = StubS3([])
        p = custody.observe("flowlogs", ACCOUNT, HOUR)
        self.assertEqual(p["objects"], 0)
        # Only CloudTrail carries the digest test; no flow-log hour is a gap.
        self.assertNotIn("digest_regions_missing", p)

    def test_lists_the_flow_log_bucket_in_every_region(self):
        stub = StubS3([])
        custody.s3 = stub
        custody.observe("flowlogs", ACCOUNT, HOUR)
        self.assertEqual({b for b, _ in stub.prefixes}, {"altdig-flow-logs-test"})
        self.assertEqual(len(stub.prefixes), 3)


class Unconfigured(unittest.TestCase):

    def test_refuses_without_the_flow_log_bucket(self):
        saved = {n: getattr(custody, n) for n in (
            "LOG_ARCHIVE_BUCKET", "CONFIG_ARCHIVE_BUCKET", "FLOW_LOG_BUCKET", "ORGANIZATION_ID",
            "REGIONS", "LEDGER_TABLE", "SEQUENCE_TABLE", "_emit")}
        emitted = []
        try:
            custody.LOG_ARCHIVE_BUCKET = custody.CONFIG_ARCHIVE_BUCKET = "b"
            custody.ORGANIZATION_ID, custody.REGIONS = "o-test", ["us-east-2"]
            custody.LEDGER_TABLE = custody.SEQUENCE_TABLE = "t"
            custody.FLOW_LOG_BUCKET = ""
            custody._emit = lambda name, cause, detail, routing="review_queue": emitted.append(name)
            with self.assertRaises(RuntimeError) as ctx:
                custody.run()
            self.assertIn("FLOW_LOG_BUCKET", str(ctx.exception))
            self.assertEqual(emitted, ["platform-evidence-log-custody-unconfigured"])
        finally:
            for n, v in saved.items():
                setattr(custody, n, v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
