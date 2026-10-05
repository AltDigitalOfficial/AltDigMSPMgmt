#!/usr/bin/env python3
"""Tests for HIPAA eligibility: catalog rules, page diff, sweep decisions.

    python scripts/test-hipaa-eligibility.py

docs/hipaa-eligibility.md. Pure where the modules are pure; boto3 clients are
created at import but never called. Needs no AWS account and no network.

The properties guarded, and why each would fail quietly:

  - A carve-out can never be marked eligible. The two are different claims to
    an auditor, and merging them is the shortcut most likely to be taken.
  - "pending" is denied. An unconfirmed prefix that slipped into the allowlist
    would put PHI on a service the BAA may not cover, with no error anywhere.
  - A page that cannot be parsed is BLIND, never "no change".
  - A narrowed qualification is reported as such, not as remove-plus-add.
  - In an enforced account a usage hit is a denied attempt (LOW, digest); in a
    dev account the same hit is MEDIUM. Inverting that pages on a working SCP.
  - DataClassification=regulated on a non-eligible type is CRITICAL.
  - Unlisted is its own status, never folded into not-allowlisted: it is a gap
    in our catalog, not a finding against a developer.
"""

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eligibility" / "src"))

import catalog  # noqa: E402
import sweep  # noqa: E402
import upstream  # noqa: E402

CATALOG = ROOT / "eligibility" / "hipaa-eligible-services.tsv"
HEADER = "\t".join(catalog.COLUMNS)


def write_catalog(rows):
    """rows: tuples of (service, on_list, status, prefixes, config, deny, alt)."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8")
    fh.write("list_version: test/1\n" + HEADER + "\n")
    for r in rows:
        fh.write("\t".join(list(r) + ["2026-10-04", "pending", ""]) + "\n")
    fh.close()
    return fh.name


def page_html(names, date="September 3, 2026"):
    items = "".join(f"<li>{n}</li>" for n in names)
    return (f"<p>Last Updated: {date}</p><ul>{items}</ul><ul>{items}</ul>"
            "<ul><li>What Is AWS?</li><li>Blogs</li></ul>")


class TheRealCatalog(unittest.TestCase):

    def test_it_validates(self):
        _, rows = catalog.load(CATALOG)
        self.assertEqual(catalog.validate(rows), [])

    def test_no_carve_out_is_on_the_aws_list(self):
        _, rows = catalog.load(CATALOG)
        for r in rows:
            if r["status"] == "carve-out":
                self.assertEqual(r["on_aws_list"], "no", r["service"])

    def test_pending_prefixes_are_denied_not_allowed(self):
        _, rows = catalog.load(CATALOG)
        allow, _, denied = catalog.prefix_sets(rows)
        for p in ("synthetics", "application-signals", "aoss", "dax"):
            self.assertNotIn(p, allow)
            self.assertIn(p, denied)

    def test_no_wildcard_and_no_ssh_path(self):
        _, rows = catalog.load(CATALOG)
        allow, _, denied = catalog.prefix_sets(rows)
        self.assertFalse([p for p in allow if "*" in p])
        self.assertIn("ec2-instance-connect", denied)


class Rules(unittest.TestCase):

    def test_eligible_off_the_list_is_refused(self):
        path = write_catalog([("IAM", "no", "eligible", "iam", "", "", "")])
        _, rows = catalog.load(path)
        self.assertTrue(any("carve-out is not eligibility" in p for p in catalog.validate(rows)))

    def test_prefix_allowed_and_denied_is_refused(self):
        path = write_catalog([
            ("A", "yes", "eligible", "foo", "", "", ""),
            ("B", "no", "not-allowlisted", "foo", "", "", "use A")])
        _, rows = catalog.load(path)
        self.assertTrue(any("both allowlisted and denied" in p for p in catalog.validate(rows)))

    def test_unknown_prefix_is_refused_when_reference_given(self):
        path = write_catalog([("A", "yes", "eligible", "nosuchprefix", "", "", "")])
        _, rows = catalog.load(path)
        self.assertTrue(catalog.validate(rows, known_prefixes={"s3"}))

    def test_reviewer_must_be_opaque(self):
        path = write_catalog([("A", "yes", "eligible", "s3", "", "", "")])
        _, rows = catalog.load(path)
        rows[0]["reviewed_by"] = "someone@altdigital.ai"
        self.assertTrue(any("opaque" in p.lower() for p in catalog.validate(rows)))

    def test_conditional_deny_parses(self):
        d = catalog.parse_deny("sagemaker:CreateLabelingJob@sagemaker:WorkteamType=public-crowd,vendor-crowd")
        self.assertEqual(d[0]["key"], "sagemaker:WorkteamType")
        self.assertEqual(d[0]["values"], ["public-crowd", "vendor-crowd"])

    def test_most_specific_config_type_wins(self):
        path = write_catalog([
            ("ECR", "yes", "eligible", "ecr", "AWS::ECR", "", ""),
            ("ECR Public", "no", "not-allowlisted", "ecr-public", "AWS::ECR::PublicRepository", "", "ECR")])
        _, rows = catalog.load(path)
        self.assertEqual(catalog.classify_config_type(rows, "AWS::ECR::Repository")[0], "eligible")
        self.assertEqual(catalog.classify_config_type(rows, "AWS::ECR::PublicRepository")[0],
                         "not-allowlisted")
        self.assertEqual(catalog.classify_config_type(rows, "AWS::Nope::Thing")[0], "unlisted")


class Page(unittest.TestCase):

    def setUp(self):
        names = [f"Service {i}" for i in range(150)] + ["Amazon Thing [excludes X]"]
        self.path = write_catalog([(n, "yes", "eligible", "", "", "", "") for n in names])
        self.version, self.rows = catalog.load(self.path)
        self.names = names

    def test_unchanged_page_reports_nothing(self):
        self.assertEqual(upstream.check(page_html(self.names), self.rows, self.version), [])

    def test_mangled_dash_and_literal_newline_are_not_changes(self):
        self.assertEqual(catalog.normalise_service("A � B\\n  "), "A - B")

    def test_no_footer_is_blind(self):
        html = page_html(self.names).replace("What Is AWS?", "Something Else")
        out = upstream.check(html, self.rows, self.version)
        self.assertEqual([o[0] for o in out], ["page-unreadable"])

    def test_large_swing_is_blind_not_forty_removals(self):
        out = upstream.check(page_html(self.names[:100]), self.rows, self.version)
        self.assertEqual([o[0] for o in out], ["page-unreadable"])

    def test_requalification_reads_as_such(self):
        names = self.names[:-1] + ["Amazon Thing [excludes X and Y]"]
        out = upstream.check(page_html(names), self.rows, self.version)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0][0].startswith("requalified"))
        self.assertEqual(out[0][1], "HIGH")

    def test_addition_is_low_removal_is_high(self):
        names = self.names[1:] + ["Brand New Service"]
        sev = {o[0].split(":")[0]: o[1] for o in upstream.check(page_html(names), self.rows, self.version)}
        self.assertEqual(sev, {"removed": "HIGH", "added": "LOW"})


class SweepDecisions(unittest.TestCase):

    def setUp(self):
        _, self.rows = catalog.load(CATALOG)
        self.dev = {"environment": "dev", "enforced": False, "alias": "arc8-dev"}
        self.prod = {"environment": "prod", "enforced": True, "alias": "arc8-prod"}

    def test_eligible_and_carve_out_are_silent(self):
        for ns in ("s3", "lambda", "iam", "sts", "ce"):
            self.assertIsNone(sweep.classify_usage(self.rows, self.dev, "r", ns), ns)

    def test_non_eligible_in_dev_is_medium(self):
        self.assertEqual(sweep.classify_usage(self.rows, self.dev, "r", "apprunner")[:2],
                         ("MEDIUM", "not-allowlisted"))

    def test_non_eligible_in_enforced_account_is_a_denied_attempt(self):
        self.assertEqual(sweep.classify_usage(self.rows, self.prod, "r", "apprunner"),
                         ("LOW", "not-allowlisted", "digest"))

    def test_pending_is_low_everywhere(self):
        self.assertEqual(sweep.classify_usage(self.rows, self.dev, "r", "synthetics")[0], "LOW")

    def test_unlisted_is_its_own_thing(self):
        self.assertEqual(sweep.classify_usage(self.rows, self.dev, "r", "brandnewsvc")[1], "unlisted")

    def test_regulated_on_non_eligible_type_is_critical(self):
        res = {"resourceType": "AWS::AppRunner::Service", "resourceId": "x",
               "tags": [{"key": "DataClassification", "value": "regulated"}]}
        self.assertEqual(sweep.classify_resource(self.rows, self.dev, res)[0], "CRITICAL")

    def test_non_eligible_resource_is_high_when_enforced(self):
        res = {"resourceType": "AWS::AppRunner::Service", "resourceId": "x", "tags": []}
        self.assertEqual(sweep.classify_resource(self.rows, self.dev, res)[0], "MEDIUM")
        self.assertEqual(sweep.classify_resource(self.rows, self.prod, res)[0], "HIGH")

    def test_eligible_resource_is_silent(self):
        res = {"resourceType": "AWS::S3::Bucket", "resourceId": "x",
               "tags": [{"key": "DataClassification", "value": "regulated"}]}
        self.assertIsNone(sweep.classify_resource(self.rows, self.prod, res))

    def test_message_names_the_alternative_and_the_consequence(self):
        _, row = catalog.classify_prefix(self.rows, "apprunner")
        msg = sweep.message(row, "not-allowlisted", "`apprunner`", self.dev, "used")
        self.assertIn("ECS on Fargate", msg)
        self.assertIn("will be denied in uat and prod", msg)
        self.assertIn("§4.16", msg)


if __name__ == "__main__":
    unittest.main(verbosity=1)
