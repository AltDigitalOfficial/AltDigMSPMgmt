#!/usr/bin/env python3
"""Tests for cost/src/report.py: which rate each tenant is billed at, and what
the report says when the billing data cannot answer.

    python scripts/test-cost-report.py

Stubs Athena and SSM, so it needs no AWS account. Every figure below is
synthetic and chosen to make the arithmetic obvious; none is a real rate.

What is being protected:
- until 2026-09-30 the report took the service model from its invocation
  event, once per run, so every tenant in a report was billed at the same
  model whatever its contract said. The model now comes from the ServiceModel
  Cost Category, per tenant;
- since 15.9 step 4 the rows come from the CUR 2.0 export, where an
  uncategorised account, an undelivered month and an absent net figure each
  have to be told apart from the ordinary case.
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

COLUMNS = ["tenant", "kind", "model", "account", "unblended", "net", "net_lines", "lines"]


def row(tenant, account, model="unset", kind="tenant", unblended=10.0, net=None, net_lines=0):
    return {"tenant": tenant, "kind": kind, "model": model, "account": account,
            "unblended": str(unblended), "net": str(unblended if net is None else net),
            "net_lines": str(net_lines), "lines": "5"}


class StubAthena:
    """Answers the report's one query with `rows`, or fails as told."""

    def __init__(self, rows, fail=None):
        self.rows, self.fail, self.sql = rows, fail, []

    def start_query_execution(self, QueryString, WorkGroup, QueryExecutionContext):
        self.sql.append(QueryString)
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, QueryExecutionId):
        if self.fail:
            return {"QueryExecution": {"Status": {"State": "FAILED", "StateChangeReason": self.fail}}}
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_paginator(self, name):
        stub = self

        class Pages:
            def paginate(self, QueryExecutionId):
                table = [COLUMNS] + [[r.get(c) for c in COLUMNS] for r in stub.rows]
                yield {"ResultSet": {"Rows": [{"Data": [{} if v is None else {"VarCharValue": v}
                                                        for v in vals]} for vals in table]}}
        return Pages()


class StubSSM:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": json.dumps(RATES)}}


def produce(rows, fail=None, month="2026-11"):
    report.athena, report.ssm = StubAthena(rows, fail), StubSSM()
    return report.report({"month": month})


def tenants_of(tenants):
    """tenants: name -> (accounts, models), one row per account and model."""
    rows = [row(n, a, m) for n, (accts, models) in tenants.items()
            for a in accts for m in models]
    return {line["tenant"]: line for line in produce(rows)["tenants"]}


class ServiceModelPerTenant(unittest.TestCase):

    def test_each_tenant_billed_at_its_own_model(self):
        out = tenants_of({"alpha": (["111111111111"], ["A"]),
                          "beta": (["222222222222", "333333333333"], ["B"])})
        self.assertEqual(out["alpha"]["service_model"], "a")
        self.assertEqual(out["beta"]["service_model"], "b")
        # Two accounts at Model B's synthetic flat rate of 3.
        self.assertEqual(out["beta"]["flat_fee"], 6.0)

    def test_unrecorded_model_uses_default(self):
        out = tenants_of({"gamma": (["444444444444"], ["unset"])})
        self.assertEqual(out["gamma"]["service_model"], "default")
        self.assertTrue(out["gamma"]["billable"])

    def test_mixed_models_within_a_tenant_are_not_billed(self):
        # Guessing which model applies would put a number on an invoice
        # nobody chose.
        out = tenants_of({"delta": (["555555555555", "666666666666"], ["A", "B"])})
        self.assertFalse(out["delta"]["billable"])
        self.assertIn("different service models", out["delta"]["problem"])

    def test_recorded_model_without_a_rate_is_named_not_defaulted(self):
        out = tenants_of({"eps": (["777777777777"], ["C"])})
        self.assertFalse(out["eps"]["billable"])
        self.assertIn("'c'", out["eps"]["problem"])

    def test_three_numbers_stay_separate(self):
        out = tenants_of({"alpha": (["111111111111"], ["A"])})["alpha"]
        self.assertEqual((out["raw_aws_cost"], out["flat_fee"], out["uplift"]), (10.0, 2.0, 0.2))
        self.assertNotIn("total", out)


class FromTheExport(unittest.TestCase):

    def test_reads_cur_category_names_by_element_at(self):
        produce([row("t", "111111111111")])
        sql = report.athena.sql[0]
        for key in ("'tenant'", "'account_kind'", "'service_model'"):
            self.assertIn(f"element_at(cost_category, {key})", sql)
        self.assertIn("billing_period = '2026-11'", sql)

    def test_shared_is_reported_not_billed(self):
        out = produce([row("platform", "111111111111", kind="shared", unblended=7.0)])
        self.assertEqual(out["tenants"], [])
        self.assertEqual(out["shared_platform_cost"], 7.0)
        self.assertIn("none billable", out["coverage_note"])

    def test_uncategorised_account_is_named_and_not_billed(self):
        # The first version billed it as a tenant called "unregistered".
        out = produce([row("oeight", "111111111111", model="A"),
                       row(None, "962342695417", kind=None, model=None, unblended=3.0)])
        self.assertEqual([t["tenant"] for t in out["tenants"]], ["oeight"])
        self.assertEqual(out["uncategorized"], {"cost": 3.0, "accounts": ["962342695417"]})
        self.assertIn("sync-cost-categories", out["uncategorized_warning"])

    def test_an_undelivered_month_is_not_a_month_of_no_spend(self):
        out = produce([], month="2026-10")
        self.assertTrue(out["produced"])
        self.assertIn("not a month of no spend", out["coverage_warning"])

    def test_spend_without_categories_is_a_warning(self):
        out = produce([row(None, "111111111111", kind=None, model=None)])
        self.assertIn("no line item carries", out["coverage_warning"])

    def test_absent_net_figure_does_not_raise_the_basis_warning_but_is_counted(self):
        out = produce([row("t", "111111111111", model="A")])
        self.assertNotIn("basis_warning", out)
        self.assertEqual(out["net_basis"]["lines_with_net"], 0)

    def test_net_below_unblended_raises_the_basis_warning(self):
        out = produce([row("t", "111111111111", model="A", unblended=10.0, net=8.0, net_lines=5)])
        self.assertIn("negotiated discounts now exist", out["basis_warning"])

    def test_query_failure_blocks_the_report_and_says_why(self):
        out = produce([], fail="HIVE_CURSOR_ERROR")
        self.assertFalse(out["produced"])
        self.assertEqual(out["blocked_by"], "BillingDataUnreadable")
        self.assertIn("HIVE_CURSOR_ERROR", out["reason"])

    def test_month_is_validated_before_it_reaches_the_sql(self):
        report.athena, report.ssm = StubAthena([]), StubSSM()
        with self.assertRaises(ValueError):
            report.report({"month": "2026-11' OR '1'='1"})


class InternalAllocationNeverInvoiced(unittest.TestCase):
    """F-8 (docs/phase-15-plan.md §6.8), D-036.

    Shared-cost allocation exists as one saved query in the FinOps template,
    for AltDigital's own margin view. No invoice path may read it: allocated
    cost inside a tenant's raw AWS figure would also carry the uplift. The
    exclusion is tested, not trusted (B-126's idea).
    """

    NAME = "internal-shared-cost-allocation"
    ALLOWED = {Path("finops/10-cost-exports.yaml"), Path("scripts/test-cost-report.py")}

    def test_only_the_template_names_it(self):
        root = Path(__file__).resolve().parent.parent
        found = set()
        for path in root.rglob("*"):
            rel = path.relative_to(root)
            if (not path.is_file() or rel.parts[0] in (".git", "docs", "design")
                    or path.suffix not in (".py", ".sh", ".yaml", ".yml", ".json")):
                continue
            if self.NAME in path.read_text(encoding="utf-8", errors="ignore"):
                found.add(rel)
        self.assertEqual(found - self.ALLOWED, set(),
                         "something other than the FinOps template reads the internal allocation")

    def test_the_report_reads_only_the_export_table(self):
        sql = report.month_sql("2026-11")
        self.assertIn(f"FROM {report.TABLE} ", sql)
        self.assertNotIn("allocat", sql.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
