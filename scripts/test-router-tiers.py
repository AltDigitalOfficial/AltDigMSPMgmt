#!/usr/bin/env python3
"""Tests for the alert router's account -> tier lookup.

    python scripts/test-router-tiers.py

An account missing from the tier map is treated as prod, which pages. That is
the right way to fail for an account nobody registered, and the wrong way for
one vested a minute ago. On the first real vesting (Arc8, 2026-10-01) the map
was cached for the life of the Lambda container, so a Config.1 finding paged
as [prod] after onboarding had already published the accounts as dev and test.

Stubs SSM and the clock; needs no AWS account.
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

NEW = "624606539840"
OLD = "111122223333"


class StubSSM:
    def __init__(self, mapping):
        self.mapping, self.reads = mapping, 0

    def get_parameter(self, Name):
        self.reads += 1
        return {"Parameter": {"Value": json.dumps(self.mapping)}}


class TierMap(unittest.TestCase):

    def setUp(self):
        self._ssm, self._now = router.ssm, router._now
        self.clock = datetime.datetime(2026, 10, 1, 21, 0, tzinfo=datetime.timezone.utc)
        router._now = lambda: self.clock
        router._TIER_CACHE.clear()
        self.ssm = router.ssm = StubSSM({OLD: "prod"})

    def tearDown(self):
        router.ssm, router._now = self._ssm, self._now
        router._TIER_CACHE.clear()

    def advance(self, seconds):
        self.clock += datetime.timedelta(seconds=seconds)

    def test_newly_vested_account_is_picked_up_on_the_next_miss(self):
        self.assertEqual(router.account_tier(OLD)[0], "prod")       # warms the cache
        self.ssm.mapping[NEW] = "dev"                                # onboarding step 8
        self.advance(61)
        tier, note = router.account_tier(NEW)
        self.assertEqual(tier, "dev")
        self.assertIsNone(note)

    def test_misses_re_read_at_most_once_a_minute(self):
        router.account_tier(OLD)
        reads = self.ssm.reads
        for _ in range(50):                                          # a burst of findings
            router.account_tier("999999999999")
        self.assertEqual(self.ssm.reads, reads)
        self.advance(61)
        router.account_tier("999999999999")
        self.assertEqual(self.ssm.reads, reads + 1)

    def test_a_hit_never_reads(self):
        router.account_tier(OLD)
        reads = self.ssm.reads
        self.advance(3600)
        router.account_tier(OLD)
        self.assertEqual(self.ssm.reads, reads)

    def test_unknown_account_is_still_prod_with_a_note(self):
        tier, note = router.account_tier("999999999999")
        self.assertEqual(tier, "prod")
        self.assertIn("not in", note)


class ByName:
    """SSM stub answering per parameter: the tier map, and empty tenant facts
    (an AltDigital platform account has no tenant)."""

    def __init__(self, tiers):
        self.tiers = tiers

    def get_parameter(self, Name):
        value = self.tiers if Name == router.ACCOUNT_TIER_PARAM else {}
        return {"Parameter": {"Value": json.dumps(value)}}


class TemplateAlarms(unittest.TestCase):
    """D-028: alarms written into templates, routed by marker."""

    class StubSecrets:
        def get_secret_value(self, SecretId):
            return {"SecretString": json.dumps({"events_v2_routing_key": "key:" + SecretId})}

    def setUp(self):
        self._ssm, self._secrets, self._now = router.ssm, router.secrets, router._now
        self._high, self._low = router.PD_SECRET_NAME, router.PD_SECRET_NAME_LOW
        router.PD_SECRET_NAME, router.PD_SECRET_NAME_LOW = "pd-high", "pd-low"
        router.secrets = self.StubSecrets()
        self.clock = datetime.datetime(2026, 10, 2, 22, 0, tzinfo=datetime.timezone.utc)
        router._now = lambda: self.clock
        router._TIER_CACHE.clear()
        router._FACTS_CACHE.clear()
        router.ssm = ByName({"692869638531": "prod"})

    def tearDown(self):
        router.ssm, router.secrets, router._now = self._ssm, self._secrets, self._now
        router.PD_SECRET_NAME, router.PD_SECRET_NAME_LOW = self._high, self._low
        router._TIER_CACHE.clear()
        router._FACTS_CACHE.clear()

    def alarm(self, trailer):
        return {"kind": "alarm", "account_id": "692869638531",
                "description": "Backup copy job failed. [platform-routing] " + trailer}

    def test_marker_without_tier_looks_the_tier_up(self):
        norm = self.alarm("routing=pagerduty layer=infrastructure")
        routing, _ = router.decide(norm)
        self.assertEqual(routing, "pagerduty")
        self.assertEqual(norm["environment_tier"], "prod")

    def test_urgency_low_downgrades_a_prod_platform_page(self):
        norm = self.alarm("routing=pagerduty layer=infrastructure urgency=low")
        router.decide(norm)
        key, destination, _ = router.routing_key(norm["environment_tier"], norm.get("layer"),
                                                 norm["account_id"], low=norm.get("urgency") == "low")
        self.assertEqual(key, "key:pd-low")
        self.assertEqual(destination, "platform")

    def test_no_urgency_field_pages_high_in_prod(self):
        norm = self.alarm("routing=pagerduty layer=infrastructure")
        router.decide(norm)
        key, _, _ = router.routing_key(norm["environment_tier"], norm.get("layer"),
                                       norm["account_id"], low=norm.get("urgency") == "low")
        self.assertEqual(key, "key:pd-high")

    def test_an_emitter_cannot_escalate(self):
        # Only "low" means anything. "urgency=high" in a dev account is ignored.
        router.ssm = ByName({"692869638531": "dev"})
        norm = self.alarm("routing=pagerduty layer=infrastructure urgency=high")
        router.decide(norm)
        self.assertNotIn("urgency", norm)
        key, _, _ = router.routing_key(norm["environment_tier"], norm.get("layer"),
                                       norm["account_id"])
        self.assertEqual(key, "key:pd-low")


class Heartbeat(unittest.TestCase):
    """D-028: a heartbeat is recorded as a metric and routed nowhere."""

    class StubCloudWatch:
        def __init__(self):
            self.calls = []

        def put_metric_data(self, **kw):
            self.calls.append(kw)

    def setUp(self):
        self._cw, self._enqueue = router.cloudwatch, router.enqueue
        self.cw = router.cloudwatch = self.StubCloudWatch()
        self.enqueued = []
        router.enqueue = lambda *a, **k: self.enqueued.append(a)

    def tearDown(self):
        router.cloudwatch, router.enqueue = self._cw, self._enqueue

    def test_heartbeat_is_recorded_and_not_routed(self):
        out = router.handler({"source": "platform.heartbeat",
                              "detail-type": "Platform Router Heartbeat",
                              "account": "754280127660", "detail": {}}, None)
        self.assertEqual(out["routed"], "heartbeat")
        self.assertEqual(len(self.cw.calls), 1)
        self.assertEqual(self.cw.calls[0]["Namespace"], "Platform/Router")
        self.assertEqual(self.cw.calls[0]["MetricData"][0]["MetricName"], "RouterHeartbeat")
        self.assertEqual(self.enqueued, [])

    def test_lookalike_from_another_source_is_not_a_heartbeat(self):
        # Only the exact source and detail-type count; anything else takes the
        # normal path (here: unrecognised, so the review queue).
        router.handler({"source": "someone.else", "detail-type": "Platform Router Heartbeat",
                        "account": "754280127660", "detail": {}}, None)
        self.assertEqual(self.cw.calls, [])
        self.assertEqual(len(self.enqueued), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
