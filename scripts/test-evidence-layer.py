#!/usr/bin/env python3
"""Tests for the evidence layer rules (evidence/src/layer.py, D-029).

    python scripts/test-evidence-layer.py

Stubs the Config aggregator; needs no AWS account.
"""

import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evidence" / "src"))

import layer  # noqa: E402
import record  # noqa: E402


def hub(resource):
    return {"source": "aws.securityhub", "account": "904883674541",
            "detail": {"findings": [{"Resources": [resource]}]}}


class StubConfig:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.queries = rows or [], fail, []

    def select_aggregate_resource_config(self, Expression, ConfigurationAggregatorName, Limit):
        self.queries.append(Expression)
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"Results": [json.dumps(r) for r in self.rows]}


class Tags(unittest.TestCase):

    def test_both_shapes_and_case(self):
        self.assertEqual(layer.from_tags({"platform-layer": "Infrastructure"}),
                         ("infrastructure", "tag"))
        self.assertEqual(layer.from_tags([{"key": "platform-layer", "value": "application"}]),
                         ("application", "tag"))

    def test_absent_and_invalid_are_not_guessed(self):
        self.assertEqual(layer.from_tags({"Name": "x"}), (None, "untagged"))
        self.assertEqual(layer.from_tags(None), (None, "untagged"))
        self.assertEqual(layer.from_tags({"platform-layer": "infra"}), (None, "tag-invalid"))


class Sources(unittest.TestCase):

    def test_security_hub_reads_the_resource_tags(self):
        e = hub({"Type": "AwsEc2Vpc", "Tags": {"platform-layer": "infrastructure"}})
        self.assertEqual(layer.of(e, "security-finding"), ("infrastructure", "tag"))

    def test_security_hub_untagged_resource(self):
        e = hub({"Type": "AwsEc2SecurityGroup"})
        self.assertEqual(layer.of(e, "security-finding"), (None, "untagged"))

    def test_the_account_itself_is_infrastructure(self):
        self.assertEqual(layer.of(hub({"Type": "AwsAccount"}), "security-finding"),
                         ("infrastructure", "account"))

    def test_platform_events_are_operator_actions(self):
        e = {"source": "platform.change", "detail": {}}
        self.assertEqual(layer.of(e, "change-event"), ("infrastructure", "operator"))

    def test_guardduty_without_instance_tags_names_nothing(self):
        e = {"source": "aws.guardduty", "detail": {"resource": {"resourceType": "S3Bucket"}}}
        self.assertEqual(layer.of(e, "security-finding"), (None, "no-resource"))

    def test_guardduty_with_instance_tags(self):
        e = {"source": "aws.guardduty", "detail": {"resource": {"instanceDetails": {
            "tags": [{"key": "platform-layer", "value": "application"}]}}}}
        self.assertEqual(layer.of(e, "security-finding"), ("application", "tag"))


class ConfigLookup(unittest.TestCase):

    def setUp(self):
        self.saved = layer._config

    def tearDown(self):
        layer._config = self.saved

    def event(self, resource_id="sg-1", resource_type="AWS::EC2::SecurityGroup"):
        return {"source": "aws.config", "account": "904883674541", "region": "us-east-2",
                "detail": {"resourceId": resource_id, "resourceType": resource_type}}

    def test_tags_come_from_the_aggregator(self):
        layer._config = StubConfig(rows=[{"tags": [{"key": "platform-layer",
                                                    "value": "infrastructure"}]}])
        self.assertEqual(layer.of(self.event(), "config-compliance"), ("infrastructure", "tag"))
        q = layer._config.queries[0]
        for part in ("resourceId = 'sg-1'", "accountId = '904883674541'", "awsRegion = 'us-east-2'"):
            self.assertIn(part, q)

    def test_a_failed_lookup_is_said_not_guessed(self):
        layer._config = StubConfig(fail=True)
        self.assertEqual(layer.of(self.event(), "config-compliance"), (None, "lookup-failed"))

    def test_a_resource_the_aggregator_does_not_know(self):
        layer._config = StubConfig(rows=[])
        self.assertEqual(layer.of(self.event(), "config-compliance"), (None, "not-found"))

    def test_quotes_cannot_break_the_query(self):
        layer._config = StubConfig(rows=[])
        layer.of(self.event(resource_id="x' OR '1'='1"), "config-compliance")
        self.assertNotIn("' OR '", layer._config.queries[0])

    def test_account_level_config_needs_no_lookup(self):
        layer._config = StubConfig(fail=True)
        self.assertEqual(layer.of(self.event("904883674541", "AWS::::Account"),
                                  "config-compliance"), ("infrastructure", "account"))


class RecordCarriesIt(unittest.TestCase):

    def test_layer_is_on_the_record_and_inside_the_hash(self):
        class DB:
            def update_item(self, **kw):
                return {"Attributes": {"seq": {"N": "1"}}}
        saved = (record.dynamodb, record.SEQUENCE_TABLE)
        record.dynamodb, record.SEQUENCE_TABLE = DB(), "sequence"
        try:
            with_layer = record.build("t", None, "security-finding", "aws.securityhub", {},
                                      layer=None, layer_source="untagged")
            without = record.build("t", None, "security-finding", "aws.securityhub", {})
        finally:
            record.dynamodb, record.SEQUENCE_TABLE = saved
        self.assertEqual((with_layer["layer"], with_layer["layer_source"]), (None, "untagged"))
        self.assertNotIn("layer", without)


if __name__ == "__main__":
    unittest.main(verbosity=2)
