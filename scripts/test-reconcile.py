#!/usr/bin/env python3
"""Tests for change reconciliation's deployer classification.

    python scripts/test-reconcile.py

deployer_class decides whose change record a deployment is reconciled
against: the tenant's, AltDigital's, or neither (an unaccounted-for
credential, which is itself a finding). Renaming the tenant CI role
(B-136) changes what it has to recognise, and getting it wrong is silent:
every tenant deploy would read as an unknown credential.

Needs no AWS account. The module creates boto3 clients at import, which
needs a region and nothing else.
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "change" / "src"))

import reconcile  # noqa: E402

ACCT = "111122223333"


def assumed(role):
    return f"arn:aws:sts::{ACCT}:assumed-role/{role}/run-1"


class DeployerClass(unittest.TestCase):

    def test_new_tenant_deployer_is_tenant(self):
        self.assertEqual(reconcile.deployer_class(assumed("TenantAppDeployer-arc8app")), "tenant")

    def test_old_tenant_deployer_is_still_tenant(self):
        # Events from before the rename must not become 'unknown'. The old name
        # also contains "Platform", so the tenant check has to run first.
        self.assertEqual(reconcile.deployer_class(assumed("PlatformAppDeployer-arc8app")), "tenant")

    def test_lookalike_is_not_tenant(self):
        # Anchored on the role-name boundary. A role that merely contains the
        # marker is not the tenant's deployer.
        self.assertNotEqual(reconcile.deployer_class(assumed("EvilTenantAppDeployer-x")), "tenant")

    def test_platform_principals(self):
        for role in ("OrganizationAccountAccessRole", "stacksets-exec-abc123",
                     "PlatformInstrumentation"):
            self.assertEqual(reconcile.deployer_class(assumed(role)), "platform", role)

    def test_anything_else_is_unknown(self):
        self.assertEqual(reconcile.deployer_class(assumed("SomeDeveloperRole")), "unknown")
        self.assertEqual(reconcile.deployer_class(None), "unknown")


class ClassifyEvent(unittest.TestCase):

    def event(self, name, principal, role_arn=None):
        detail = {
            "eventName": name,
            "userIdentity": {"arn": principal, "type": "AssumedRole"},
            "recipientAccountId": ACCT,
            "awsRegion": "us-east-2",
            "eventTime": "2026-09-30T00:00:00Z",
            "eventID": "e-1",
        }
        if role_arn:
            detail["requestParameters"] = {"roleArn": role_arn}
        return {"detail": detail}

    def test_new_deployer_assumption_counts(self):
        name = next(k for k, v in reconcile.DEPLOYMENT_EVENTS.items() if v == "appdeployer-assumption")
        ev = self.event(name, "arn:aws:sts::000000000000:assumed-role/x/y",
                        role_arn=f"arn:aws:iam::{ACCT}:role/TenantAppDeployer-arc8app")
        self.assertIsNotNone(reconcile.classify_event(ev))

    def test_other_federation_is_ignored(self):
        name = next(k for k, v in reconcile.DEPLOYMENT_EVENTS.items() if v == "appdeployer-assumption")
        ev = self.event(name, "arn:aws:sts::000000000000:assumed-role/x/y",
                        role_arn=f"arn:aws:iam::{ACCT}:role/SomethingElse")
        self.assertIsNone(reconcile.classify_event(ev))

    def test_via_appdeployer_flag(self):
        kinds = [k for k, v in reconcile.DEPLOYMENT_EVENTS.items() if v != "appdeployer-assumption"]
        self.assertTrue(kinds, "no deployment event kinds to test with")
        out = reconcile.classify_event(self.event(kinds[0], assumed("TenantAppDeployer-arc8app")))
        self.assertTrue(out["via_appdeployer"])
        self.assertEqual(out["deployer"], "tenant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
