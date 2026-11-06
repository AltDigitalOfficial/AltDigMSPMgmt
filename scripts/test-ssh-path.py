"""Tests for the SSH-path detector (security/55-ssh-path.yaml, B-101).

    python scripts/test-ssh-path.py

The function is inline in the template, so this loads that exact text and runs
it against stub Athena and EventBridge clients. What is tested is what deploys.
"""
import datetime
import json
import os
import pathlib
import sys
import types
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "security" / "55-ssh-path.yaml"
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 6, 0, 5, tzinfo=UTC)


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def load():
    t = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)
    code = t["Resources"]["DetectorFunction"]["Properties"]["Code"]["ZipFile"]
    sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda name: None))
    mod = types.ModuleType("detector")
    exec(compile(code, str(TEMPLATE), "exec"), mod.__dict__)
    return mod


class StubAthena:
    """Answers the 'seen' query with a count and the SSH query with rows."""

    def __init__(self, records=100, rows=(), fail=None):
        self.records, self.rows, self.fail, self.sql = records, list(rows), fail, []

    def start_query_execution(self, QueryString, WorkGroup, QueryExecutionContext):
        self.sql.append(QueryString)
        return {"QueryExecutionId": f"q{len(self.sql)}"}

    def get_query_execution(self, QueryExecutionId):
        if self.fail:
            return {"QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": self.fail}}}
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_paginator(self, name):
        stub = self

        class Pages:
            def paginate(self, QueryExecutionId):
                sql = stub.sql[int(QueryExecutionId[1:]) - 1]
                if "count(*) AS records" in sql:
                    table = [["records"], [str(stub.records)]]
                else:
                    keys = ["account", "region", "interface_id", "target", "vpc_id", "flows",
                            "packets", "sources", "sample_sources", "first_seen", "last_seen"]
                    table = [keys] + [[r.get(k) for k in keys] for r in stub.rows]
                yield {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": v} for v in row]}
                                              for row in table]}}
        return Pages()


class StubEvents:
    def __init__(self):
        self.sent = []

    def put_events(self, Entries):
        self.sent.extend(Entries)
        return {"FailedEntryCount": 0}


ROW = {"account": "624606539840", "region": "us-east-2", "interface_id": "eni-0abc",
       "target": "i-0123", "vpc_id": "vpc-1", "flows": "42", "packets": "300",
       "sources": "7", "sample_sources": "1.2.3.4,5.6.7.8", "first_seen": "1793923200",
       "last_seen": "1793926800"}


class Base(unittest.TestCase):

    def setUp(self):
        os.environ["ALERT_BUS_ARN"] = "arn:aws:events:us-east-2:111111111111:event-bus/platform-alerts"
        os.environ.pop("LOOKBACK_MINUTES", None)
        os.environ.pop("LAG_MINUTES", None)
        self.m = load()
        self.events = StubEvents()

    def use(self, athena):
        self.athena = athena
        self.m._clients.update({"athena": athena, "events": self.events})

    def details(self):
        return [json.loads(e["Detail"]) for e in self.events.sent]


class Window(Base):

    def test_window_overlaps_the_hourly_schedule(self):
        start, end = self.m.window(NOW)
        self.assertEqual(end, NOW - datetime.timedelta(minutes=10))
        self.assertEqual(end - start, datetime.timedelta(minutes=70))

    def test_window_across_midnight_reads_both_days(self):
        # 00:30 ends the window at 00:20 and starts it at 23:10.
        start, end = self.m.window(NOW.replace(minute=30))
        sql = self.m.ssh_sql(start, end)
        self.assertIn("dt IN ('2026/11/05', '2026/11/06')", sql)

    def test_signal_is_accepted_inbound_tcp_22_only(self):
        sql = self.m.ssh_sql(*self.m.window(NOW))
        for clause in ("dstport = 22", "protocol = 6", "action = 'ACCEPT'",
                       "flow_direction = 'ingress'"):
            self.assertIn(clause, sql)
        self.assertNotIn("REJECT", sql)


class Findings(Base):

    def test_one_dossier_per_target_with_no_routing_declared(self):
        self.use(StubAthena(rows=[ROW, dict(ROW, target="i-0456")]))
        out = self.m.run(now=NOW, account="868150784436")
        self.assertEqual(out["found"], 2)
        ds = self.details()
        self.assertEqual(len(ds), 2)
        for d in ds:
            # The router decides who hears it, from the subject account's tier.
            self.assertNotIn("routing", d)
            self.assertEqual(d["alarm"]["account_id"], "624606539840")
            self.assertEqual(d["alarm"]["name"], self.m.TITLE)
        self.assertEqual({e["Source"] for e in self.events.sent}, {"platform.network"})
        self.assertEqual({e["DetailType"] for e in self.events.sent}, {"Platform Incident Dossier"})
        self.assertEqual(ds[0]["ssh_path"]["sample_sources"], ["1.2.3.4", "5.6.7.8"])

    def test_title_and_resource_are_stable_for_the_routers_dedup(self):
        self.use(StubAthena(rows=[ROW]))
        self.m.run(now=NOW)
        self.m.run(now=NOW + datetime.timedelta(hours=1))
        a, b = self.details()
        self.assertEqual((a["alarm"]["name"], a["alarm"]["resource_id"]),
                         (b["alarm"]["name"], b["alarm"]["resource_id"]))

    def test_nothing_found_sends_nothing(self):
        self.use(StubAthena(rows=[]))
        out = self.m.run(now=NOW)
        self.assertEqual(out["found"], 0)
        self.assertEqual(self.events.sent, [])


class Blind(Base):

    def test_no_records_at_all_is_a_finding_not_a_quiet_hour(self):
        self.use(StubAthena(records=0))
        out = self.m.run(now=NOW, account="868150784436")
        self.assertTrue(out["blind"])
        (d,) = self.details()
        self.assertEqual(d["routing"], "digest_and_jira")
        self.assertEqual(d["alarm"]["account_id"], "868150784436")
        # The SSH query is not run against a table that saw nothing.
        self.assertEqual(len(self.athena.sql), 1)

    def test_failure_is_raised_and_reported(self):
        self.use(StubAthena(fail="HIVE_BAD_DATA"))
        ctx = types.SimpleNamespace(
            invoked_function_arn="arn:aws:lambda:us-east-2:868150784436:function:platform-ssh-path",
            get_remaining_time_in_millis=lambda: 300000)
        with self.assertRaises(RuntimeError):
            self.m.handler({}, ctx)
        (d,) = self.details()
        self.assertEqual(d["alarm"]["name"], "SSH-path detector failed")
        self.assertEqual(d["routing"], "digest_and_jira")
        self.assertIn("HIVE_BAD_DATA", d["assessment"]["probable_cause"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
