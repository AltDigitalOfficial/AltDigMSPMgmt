#!/usr/bin/env python3
"""Tests for moving unattributed evidence to its tenant (B-156 item 8).

    python scripts/test-evidence-reattribute.py

Stubs DynamoDB, SQS, SSM and EventBridge in memory; needs no AWS account.
"""

import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ["EVIDENCE_LEDGER_TABLE"] = "ledger"
os.environ["EVIDENCE_SEQUENCE_TABLE"] = "sequence"
os.environ["EVIDENCE_QUEUE_URL"] = "queue"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evidence" / "src"))

import completeness  # noqa: E402
import record  # noqa: E402
import reattribute  # noqa: E402

DEV, TEST, STRAY = "624606539840", "904883674541", "111122223333"
UNATTRIBUTED = "altdigital-unattributed"


class ConditionalCheckFailed(Exception):
    pass


class StubDynamo:
    """Just enough of DynamoDB: one ledger, one sequence counter table."""

    class exceptions:
        ConditionalCheckFailedException = ConditionalCheckFailed

    def __init__(self):
        self.ledger, self.seq = {}, {}

    def update_item(self, TableName, Key, UpdateExpression, **kw):
        if TableName == "sequence":
            stream = Key["stream_id"]["S"]
            self.seq[stream] = self.seq.get(stream, 0) + 1
            return {"Attributes": {"seq": {"N": str(self.seq[stream])}}}
        row = self.ledger[(Key["PK"]["S"], Key["SK"]["S"])]
        vals = kw["ExpressionAttributeValues"]
        want = vals[":pending"] if ":pending" in vals else vals[":claimed"]
        if row["status"] != want:
            raise ConditionalCheckFailed()
        if ":claimed" in vals and ":pending" in vals:
            row["status"] = vals[":claimed"]
        else:
            row["status"] = vals[":done"]
            row["reattributed_to"] = vals[":to"]

    def put_item(self, TableName, Item, **kw):
        key = (Item["PK"]["S"], Item["SK"]["S"])
        if key in self.ledger:
            raise ConditionalCheckFailed()
        self.ledger[key] = {"PK": Item["PK"], "SK": Item["SK"],
                            "sequence": Item["sequence"], "body": Item["body"],
                            "status": Item["status"]}

    def scan(self, **kw):
        vals = kw["ExpressionAttributeValues"]
        wanted = {vals[":pending"]["S"], vals[":claimed"]["S"]}
        items = [dict(r, status=r["status"]) for (pk, _), r in sorted(self.ledger.items())
                 if pk.startswith(vals[":p"]["S"]) and r["status"]["S"] in wanted]
        return {"Items": items}


class Stub:
    def __init__(self):
        self.calls = []

    def send_message(self, **kw):
        self.calls.append(json.loads(kw["MessageBody"]))

    def put_events(self, Entries):
        self.calls.extend(json.loads(e["Detail"]) for e in Entries)

    def get_parameter(self, Name):
        return {"Parameter": {"Value": json.dumps({"tenants": {
            "oeight-oeight": {"partner": "oeight", "accounts": [DEV, TEST]}}})}}


def stranded_row(db, account, category="security-finding", status="pending"):
    rec = record.build(tenant=UNATTRIBUTED, partner=None, category=category,
                       source="aws.securityhub", payload={"finding": account},
                       control_ref="security-finding-triage",
                       occurred_at="2026-10-01T21:36:00Z", account_id=account)
    item = reattribute._ledger_item(rec)
    item["status"] = {"S": status}
    db.put_item("ledger", item)
    return rec


class Reattribute(unittest.TestCase):

    def setUp(self):
        self.db, self.sqs, self.events, self.ssm = StubDynamo(), Stub(), Stub(), Stub()
        self.saved = (record.dynamodb, reattribute.dynamodb, reattribute.sqs,
                      reattribute.events_client, reattribute.ssm, record.SEQUENCE_TABLE,
                      reattribute.LEDGER_TABLE, reattribute.QUEUE_URL)
        record.dynamodb = reattribute.dynamodb = self.db
        reattribute.sqs, reattribute.events_client, reattribute.ssm = self.sqs, self.events, self.ssm
        record.SEQUENCE_TABLE = "sequence"
        reattribute.LEDGER_TABLE, reattribute.QUEUE_URL = "ledger", "queue"

    def tearDown(self):
        (record.dynamodb, reattribute.dynamodb, reattribute.sqs,
         reattribute.events_client, reattribute.ssm, record.SEQUENCE_TABLE,
         reattribute.LEDGER_TABLE, reattribute.QUEUE_URL) = self.saved

    def row(self, stream, seq):
        return self.db.ledger[(stream, f"{seq:012d}")]

    def test_a_mapped_record_moves_with_pointers_both_ways(self):
        orig = stranded_row(self.db, TEST)
        out = reattribute.reattribute({}, None)
        self.assertEqual(out["moved"], 1)

        old = self.row(f"{UNATTRIBUTED}#security-finding", 1)
        self.assertEqual(old["status"]["S"], "reattributed")
        self.assertEqual(old["reattributed_to"]["S"],
                         "oeight-oeight#security-finding#000000000001")

        new = self.row("oeight-oeight#security-finding", 1)
        self.assertEqual(new["status"]["S"], "pending")
        body = json.loads(new["body"]["S"])
        self.assertEqual(body["reattributed_from"],
                         {"stream_id": f"{UNATTRIBUTED}#security-finding",
                          "sequence": 1, "content_hash": orig["content_hash"],
                          "emitted_at": orig["emitted_at"]})
        # The evidence itself is carried unchanged; only filing changes.
        for field in ("occurred_at", "payload", "control_ref", "source", "account_id"):
            self.assertEqual(body[field], orig[field])
        self.assertEqual(body["partner"], "oeight")

        (queued,) = self.sqs.calls
        self.assertEqual(queued["stream_id"], "oeight-oeight#security-finding")
        (emitted,) = self.events.calls
        self.assertEqual(emitted["routing"], "digest")
        self.assertEqual(emitted["alarm"]["name"], "platform-evidence-reattributed")

    def test_an_unmapped_account_stays_pending_and_is_reported(self):
        stranded_row(self.db, STRAY)
        out = reattribute.reattribute({}, None)
        self.assertEqual(out["moved"], 0)
        self.assertEqual(out["unmapped_by_account"], {STRAY: 1})
        self.assertEqual(self.row(f"{UNATTRIBUTED}#security-finding", 1)["status"]["S"],
                         "pending")
        self.assertEqual(self.sqs.calls, [])

    def test_dry_run_writes_nothing(self):
        stranded_row(self.db, DEV)
        stranded_row(self.db, TEST, category="config-compliance")
        before = json.dumps({str(k): v for k, v in self.db.ledger.items()}, sort_keys=True)
        out = reattribute.reattribute({"dry_run": True}, None)
        self.assertEqual(out["would_move"], 2)
        self.assertEqual(out["moved"], 0)
        self.assertEqual(json.dumps({str(k): v for k, v in self.db.ledger.items()},
                                    sort_keys=True), before)
        self.assertEqual((self.sqs.calls, self.events.calls), ([], []))

    def test_a_half_finished_move_is_reported_never_retried(self):
        stranded_row(self.db, TEST, status="reattributing")
        out = reattribute.reattribute({}, None)
        self.assertEqual(out["moved"], 0)
        self.assertEqual(out["stuck"], [{"stream_id": f"{UNATTRIBUTED}#security-finding",
                                         "sequence": 1}])
        self.assertNotIn(("oeight-oeight#security-finding", f"{1:012d}"), self.db.ledger)
        (emitted,) = self.events.calls
        self.assertEqual(emitted["routing"], "digest_and_jira")

    def test_delivered_rows_are_not_touched(self):
        stranded_row(self.db, TEST, status="delivered")
        out = reattribute.reattribute({}, None)
        self.assertEqual((out["moved"], out["would_move"]), (0, 0))

    def test_a_second_run_moves_nothing(self):
        stranded_row(self.db, TEST)
        reattribute.reattribute({}, None)
        out = reattribute.reattribute({}, None)
        self.assertEqual(out["moved"], 0)
        self.assertEqual(self.db.seq["oeight-oeight#security-finding"], 1)


class CompletenessAcceptsTheMove(unittest.TestCase):

    def test_reattributed_is_not_a_gap_but_reattributing_is(self):
        old = "2026-10-01T21:36:00+00:00"
        rows = {1: {"status": "reattributed", "emitted_at": old},
                2: {"status": "reattributing", "emitted_at": old},
                3: {"status": "delivered", "emitted_at": old},
                4: {"status": "pending", "emitted_at": old}}
        cutoff = completeness._parse("2026-10-04T00:00:00+00:00")
        self.assertEqual(completeness._missing_sequences(rows, cutoff), [2, 4])

    def test_reattributed_is_not_counted_against_truveon(self):
        rows = {1: {"status": "reattributed", "emitted_at": "2026-10-02T00:00:00+00:00"},
                2: {"status": "delivered", "emitted_at": "2026-10-02T00:01:00+00:00"},
                "_window_start": None, "_window_end": None}
        saved = completeness._ledger_rows
        completeness._ledger_rows = lambda stream: rows
        try:
            since = completeness._parse("2026-10-01T00:00:00+00:00")
            until = completeness._parse("2026-10-03T00:00:00+00:00")
            self.assertEqual(completeness._count_ours("s", since, until),
                             {"count": 1, "min": 2, "max": 2})
        finally:
            completeness._ledger_rows = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
