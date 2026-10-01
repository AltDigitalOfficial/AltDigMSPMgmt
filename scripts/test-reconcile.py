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

import json
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

    def test_web_identity_assumption_is_the_tenant_control_point(self):
        # The shape CloudTrail actually records, 2026-10-01: a WebIdentityUser
        # has userName (the OIDC subject) and NO arn. Classifying by the caller
        # made this 'unknown', so a deploy through the control point would have
        # been reported as unaccounted-for credentials.
        name = next(k for k, v in reconcile.DEPLOYMENT_EVENTS.items() if v == "appdeployer-assumption")
        ev = {"detail": {
            "eventName": name,
            "userIdentity": {
                "type": "WebIdentityUser",
                "userName": "repo:Org@1/app@2:ref:refs/heads/release",
                "identityProvider": "token.actions.githubusercontent.com"},
            "requestParameters": {
                "roleArn": f"arn:aws:iam::{ACCT}:role/TenantAppDeployer-arc8app",
                "roleSessionName": "GitHubActions"},
            "recipientAccountId": ACCT, "awsRegion": "us-east-2",
            "eventTime": "2026-10-01T01:26:57Z", "eventID": "e-2"}}
        out = reconcile.classify_event(ev)
        self.assertEqual(out["deployer"], "tenant")
        self.assertTrue(out["via_appdeployer"])

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


class InfrastructureApprovers(unittest.TestCase):
    """2026-10-01: two approvals from the infrastructure list, requester excluded.

    Jira's rule is any two members of the Approver group. These pin what the
    platform counts instead. Account ids are synthetic.
    """

    WAYNE, ART, TINA, JAMIE = "acc-w", "acc-a", "acc-t", "acc-j"

    class StubSSM:
        def __init__(self, value):
            self.value = value

        def get_parameter(self, Name):
            if self.value is None:
                raise RuntimeError("ParameterNotFound")
            return {"Parameter": {"Value": json.dumps(self.value)}}

    def setUp(self):
        self._ssm, self._approvals = reconcile.ssm, reconcile.jira.approvals
        reconcile._APPROVAL_CACHE.clear()
        reconcile._INFRA_APPROVERS.clear()
        reconcile.ssm = self.StubSSM(
            {"approvers": [self.WAYNE, self.ART, self.TINA], "minimum": 2})

    def tearDown(self):
        reconcile.ssm, reconcile.jira.approvals = self._ssm, self._approvals
        reconcile._APPROVAL_CACHE.clear()
        reconcile._INFRA_APPROVERS.clear()

    def verdict(self, approvers, reporter, layer="infrastructure", key="OPS-1"):
        record = [{"finalDecision": "approved",
                   "approvers": [{"approver": {"accountId": a},
                                  "approverDecision": "approved"} for a in approvers]}]
        reconcile.jira.approvals = lambda k: record
        change = {"key": key, "status": "Awaiting implementation",
                  "approved_by_status": True, "reporter_account_id": reporter}
        if layer:
            change["layer"] = layer
        return reconcile.is_approved(change)

    def test_two_authorised_approvers_pass(self):
        ok, evidence = self.verdict([self.WAYNE, self.ART], reporter=self.JAMIE)
        self.assertTrue(ok, evidence)

    def test_requester_does_not_count_towards_their_own_approval(self):
        ok, evidence = self.verdict([self.WAYNE, self.ART], reporter=self.WAYNE)
        self.assertFalse(ok)
        self.assertIn("other than the requester", evidence)

    def test_approver_off_the_list_does_not_count(self):
        ok, _ = self.verdict([self.WAYNE, self.JAMIE], reporter="acc-dev")
        self.assertFalse(ok)

    def test_tenant_changes_are_not_held_to_the_infrastructure_list(self):
        ok, _ = self.verdict([self.JAMIE], reporter="acc-dev", layer=None, key="ARC8-1")
        self.assertTrue(ok)

    def test_unreadable_list_does_not_accuse_but_says_so(self):
        reconcile.ssm = self.StubSSM(None)
        ok, evidence = self.verdict([self.WAYNE, self.ART], reporter=self.JAMIE)
        self.assertTrue(ok)
        self.assertIn("NOT checked", evidence)


class TenantJiraSite(unittest.TestCase):
    """A tenant's change records on its own Atlassian site (change.ticketing)."""

    def setUp(self):
        reconcile._APPROVAL_CACHE.clear()
        self._approvals = reconcile.jira.approvals

    def tearDown(self):
        reconcile.jira.approvals = self._approvals
        reconcile._APPROVAL_CACHE.clear()

    def test_using_switches_site_and_credential_and_restores_them(self):
        jira = reconcile.jira
        before_site, before_secret = jira._site(), jira._secret_name()
        with jira.using("https://tenant.atlassian.net", "platform/jira/tenant/p-c"):
            self.assertEqual(jira._site(), "https://tenant.atlassian.net")
            self.assertEqual(jira._secret_name(), "platform/jira/tenant/p-c")
        self.assertEqual((jira._site(), jira._secret_name()), (before_site, before_secret))

    def test_no_site_is_a_no_op(self):
        jira = reconcile.jira
        before = (jira._site(), jira._secret_name())
        with jira.using(None, None):
            self.assertEqual((jira._site(), jira._secret_name()), before)

    def test_a_site_without_a_credential_is_refused(self):
        with self.assertRaises(reconcile.jira.JiraUnavailable):
            with reconcile.jira.using("https://tenant.atlassian.net", None):
                pass

    def test_same_issue_key_on_two_sites_is_judged_separately(self):
        # Two Atlassian sites can both hold an ARC8-1. A cache keyed by issue
        # alone would hand one site's verdict to the other.
        verdicts = {"https://a.atlassian.net": "approved",
                    "https://b.atlassian.net": "pending"}
        reconcile.jira.approvals = lambda k: [{
            "finalDecision": verdicts[reconcile.jira.current_site()], "approvers": []}]
        change = {"key": "ARC8-1", "status": "x", "approved_by_status": False}
        with reconcile.jira.using("https://a.atlassian.net", "s-a"):
            a = reconcile.is_approved(dict(change))
        with reconcile.jira.using("https://b.atlassian.net", "s-b"):
            b = reconcile.is_approved(dict(change))
        self.assertTrue(a[0])
        self.assertFalse(b[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
