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


if __name__ == "__main__":
    unittest.main(verbosity=2)
