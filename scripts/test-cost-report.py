#!/usr/bin/env python3
"""Tests for cost/src/report.py: which rate each tenant is billed at.

    python scripts/test-cost-report.py

Stubs Cost Explorer and SSM, so it needs no AWS account. Every figure below is
synthetic and chosen to make the arithmetic obvious; none is a real rate.

What is being protected: until 2026-09-30 the report took the service model
from its invocation event, once per run, so every tenant in a report was
billed at the same model whatever its contract said. The model now comes from
the ServiceModel Cost Category, per tenant.
"""

import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cost" / "src"))

import report  # noqa: E402

RATES = {"service_models": {
    "default": {"flat_fee_per_account_month": 1, "uplift_percent": 1},
    "a": {"flat_fee_per_account_month": 2, "uplift_percent": 2},
    "b": {"flat_fee_per_account_month": 3, "uplift_percent": 3},
}}


def group(keys, amount=10.0):
    return {"Keys": keys, "Metrics": {"UnblendedCost": {"Amount": str(amount)},
                                      "NetUnblendedCost": {"Amount": str(amount)}}}


class StubCE:
    """Answers the three grouped queries the report makes, from `tenants`."""

    def __init__(self, tenants):
        self.tenants = tenants

    def get_cost_and_usage(self, TimePeriod, Granularity, Metrics, GroupBy=None):
        keys = [g["Key"] for g in (GroupBy or [])]
        t = self.tenants
        if keys == [report.TENANT_CATEGORY, report.KIND_CATEGORY]:
            groups = [group([f"Tenant${n}", "AccountKind$tenant"]) for n in t]
        elif keys == ["LINKED_ACCOUNT", report.TENANT_CATEGORY]:
            groups = [group([a, f"Tenant${n}"]) for n, (accts, _) in t.items() for a in accts]
        elif keys == [report.TENANT_CATEGORY, report.MODEL_CATEGORY]:
            groups = [group([f"Tenant${n}", f"ServiceModel${m}"])
                      for n, (_, models) in t.items() for m in models]
        else:
            groups = []
        return {"ResultsByTime": [{"Groups": groups,
                                   "Total": {"UnblendedCost": {"Amount": "0"}}}]}


class StubSSM:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": json.dumps(RATES)}}


def run(tenants):
    report.ce, report.ssm = StubCE(tenants), StubSSM()
    return {line["tenant"]: line for line in report.report({"month": "2026-08"})["tenants"]}


class ServiceModelPerTenant(unittest.TestCase):

    def test_each_tenant_billed_at_its_own_model(self):
        out = run({"alpha": (["111111111111"], ["A"]),
                   "beta": (["222222222222", "333333333333"], ["B"])})
        self.assertEqual(out["alpha"]["service_model"], "a")
        self.assertEqual(out["beta"]["service_model"], "b")
        # Two accounts at Model B's synthetic flat rate of 3.
        self.assertEqual(out["beta"]["flat_fee"], 6.0)

    def test_unrecorded_model_uses_default(self):
        out = run({"gamma": (["444444444444"], ["unset"])})
        self.assertEqual(out["gamma"]["service_model"], "default")
        self.assertTrue(out["gamma"]["billable"])

    def test_mixed_models_within_a_tenant_are_not_billed(self):
        # Guessing which model applies would put a number on an invoice
        # nobody chose.
        out = run({"delta": (["555555555555", "666666666666"], ["A", "B"])})
        self.assertFalse(out["delta"]["billable"])
        self.assertIn("different service models", out["delta"]["problem"])

    def test_recorded_model_without_a_rate_is_named_not_defaulted(self):
        out = run({"eps": (["777777777777"], ["C"])})
        self.assertFalse(out["eps"]["billable"])
        self.assertIn("'c'", out["eps"]["problem"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
