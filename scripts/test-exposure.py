#!/usr/bin/env python3
"""Tests for internet exposure and expiry (ADR-015, ADR-016, B-176).

    python scripts/test-exposure.py

What these guard:
  - Arc8 dev as it stood on 2026-10-28 renders as what a manager acts on:
    three distributions with no web ACL, an origin whose SSH is open to the
    world, and the SSH line first;
  - an origin whose web ports admit only CloudFront is not called "reachable
    directly" when SSH is the only thing open;
  - a section that cannot be read says so, and one blocked by an SCP counts
    as checked;
  - the thresholds: a managed certificate warns at 45 days, a domain with
    auto-renew at 21, one without at 45;
  - the digest shows the section on a quiet day, and says what is not checked;
  - the expiry warning's name is stable from day to day (one ticket, not one
    per run), and the router honours its routing only as a downgrade.

Stubs every AWS client; needs no AWS account. Needs zoneinfo with a time zone
database (on Windows: pip install tzdata).
"""

import datetime
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "reporting" / "src"))
sys.path.insert(0, str(ROOT / "alerting" / "src"))
sys.path.insert(0, str(ROOT / "instrumentation" / "src"))

import exposure  # noqa: E402
import facts  # noqa: E402
import digest  # noqa: E402
import expiry  # noqa: E402
import router  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 28, 12, tzinfo=UTC)
DEV = "624606539840"
TEST = "754280127660"
PROD = "111111111111"
AUDIT = "692869638531"
TIERS = {AUDIT: "prod", DEV: "dev", TEST: "test", PROD: "prod"}

CF_PL = "pl-b6a144df"
ORIGIN_DNS = "ec2-16-59-20-139.us-east-2.compute.amazonaws.com"


def arc8_dev():
    """Arc8 dev's public surface, 2026-10-28, as read_account returns it."""
    def dist(i):
        return {"id": f"E{i}", "domain": f"d{i}.cloudfront.net", "aliases": [],
                "enabled": True, "web_acl": None, "viewer_protocol": "redirect-to-https",
                "default_certificate": True, "origins": [ORIGIN_DNS]}

    def cf_rule(port):
        return {"protocol": "tcp", "from": port, "to": port, "prefix_list": CF_PL,
                "prefix_list_name": "com.amazonaws.global.cloudfront.origin-facing",
                "group_name": f"arc8-dev-ec2-cf-{port}"}

    return {
        "cloudfront": [dist(1), dist(2), dist(3)],
        "load_balancers": [], "databases": [], "apis": [], "hosted_zones": [],
        "domains": [], "domain_operations": [], "certificates": [],
        "instances": [{
            "region": "us-east-2", "id": "i-07ca33cf795ecab8d", "name": "arc8-dev-ubuntu",
            "public_ip": "16.59.20.139", "public_dns": ORIGIN_DNS,
            "rules": [cf_rule(80), cf_rule(443), cf_rule(7777), cf_rule(7778),
                      {"protocol": "tcp", "from": 22, "to": 22, "source": "0.0.0.0/0",
                       "group_name": "arc8-dev-ec2-ssh"}],
        }],
        "errors": {},
    }


class Judging(unittest.TestCase):

    def test_arc8_dev(self):
        found = exposure.assess(arc8_dev())
        dists = [f for f in found if f["kind"] == "CloudFront distribution"]
        self.assertEqual(len(dists), 3)
        for d in dists:
            self.assertIn("no web ACL (WAF) in front of it", d["risks"])
            self.assertTrue(any("is itself open to the whole internet on SSH (22)" in r
                                for r in d["risks"]), d["risks"])
        inst = [f for f in found if f["kind"] == "EC2 instance with a public IP"][0]
        self.assertEqual(inst["risks"][0], "SSH (22) is open to the whole internet")
        self.assertIn("com.amazonaws.global.cloudfront.origin-facing", inst["notes"][-1])

    def test_origin_limited_to_cloudfront_is_not_a_bypass(self):
        inv = arc8_dev()
        inv["instances"][0]["rules"] = [r for r in inv["instances"][0]["rules"]
                                        if r.get("prefix_list")]
        found = exposure.assess(inv)
        dist = [f for f in found if f["kind"] == "CloudFront distribution"][0]
        self.assertEqual(dist["risks"], ["no web ACL (WAF) in front of it"])
        inst = [f for f in found if f["kind"] == "EC2 instance with a public IP"][0]
        self.assertEqual(inst["risks"], [])

    def test_web_acl_and_https_only_is_quiet(self):
        inv = arc8_dev()
        inv["instances"] = []
        for d in inv["cloudfront"]:
            d["web_acl"] = "arn:aws:wafv2:us-east-1:1:global/webacl/x/y"
        self.assertTrue(all(not f["risks"] for f in exposure.assess(inv)))

    def test_plain_http_is_a_risk(self):
        inv = arc8_dev()
        inv["cloudfront"][0]["viewer_protocol"] = "allow-all"
        dist = exposure.assess(inv)[0]
        self.assertIn("serves plain HTTP as well as HTTPS", dist["risks"])

    def test_all_traffic_and_database_ports(self):
        inst = {"id": "i-1", "rules": [
            {"protocol": "all", "from": None, "to": None, "source": "0.0.0.0/0"},
            {"protocol": "tcp", "from": 5432, "to": 5432, "source": "::/0"},
            {"protocol": "tcp", "from": 8080, "to": 8080, "source": "0.0.0.0/0"}]}
        risks, notes = exposure._instance_findings(inst)
        self.assertIn("all traffic is open to the whole internet", risks)
        self.assertIn("PostgreSQL (5432) is open to the whole internet", risks)
        self.assertIn("port 8080 is open to the whole internet", notes)

    def test_nlb_cannot_carry_a_web_acl(self):
        inv = {"load_balancers": [{"region": "us-east-2", "name": "n", "type": "network"}]}
        f = exposure.assess(inv)[0]
        self.assertEqual(f["risks"], [])
        self.assertIn("cannot carry a web ACL", f["notes"][0])

    def test_alb_whose_web_acl_could_not_be_read_is_not_accused(self):
        inv = {"load_balancers": [{"region": "us-east-2", "name": "a", "type": "application",
                                   "web_acl_error": {"state": "error"}}]}
        f = exposure.assess(inv)[0]
        self.assertEqual(f["risks"], [])

    def test_failed_registration_is_a_risk_line(self):
        inv = {"domain_operations": [{"domain": "arc8-mvp.click", "type": "REGISTER_DOMAIN",
                                      "status": "FAILED", "message": "Domain not available"}]}
        f = exposure.assess(inv)[0]
        self.assertEqual(f["risks"], ["did not succeed: Domain not available"])

    def test_pending_registration_says_what_it_waits_on(self):
        inv = {"domain_operations": [{"domain": "arc8-mvp.click", "type": "REGISTER_DOMAIN",
                                      "status": "IN_PROGRESS",
                                      "flag": "PENDING_ACCEPTANCE"}]}
        f = exposure.assess(inv)[0]
        self.assertEqual(f["notes"], ["register domain: in_progress",
                                      "waiting on: pending acceptance"])


def cert(days, managed=True, in_use=True, status="ISSUED"):
    return {"region": "us-east-1", "arn": f"arn:aws:acm:us-east-1:1:certificate/{days}",
            "domain": "dev.example.com", "status": status,
            "type": "AMAZON_ISSUED" if managed else "IMPORTED", "in_use": in_use,
            "not_after": exposure._iso(NOW + datetime.timedelta(days=days, hours=1)),
            "renewal_eligibility": "ELIGIBLE" if managed else "INELIGIBLE"}


def domain(days, auto=True):
    return {"name": "arc8-mvp.click", "auto_renew": auto,
            "expiry": exposure._iso(NOW + datetime.timedelta(days=days, hours=1))}


class Expiry(unittest.TestCase):

    def test_certificate_threshold(self):
        self.assertEqual(exposure.expiring({"certificates": [cert(46)]}, NOW), [])
        e = exposure.expiring({"certificates": [cert(45)]}, NOW)[0]
        self.assertEqual(e["days"], 45)
        self.assertFalse(e["urgent"])
        self.assertIn("should have renewed", e["cause"])

    def test_imported_certificate_says_nobody_will_renew_it(self):
        e = exposure.expiring({"certificates": [cert(30, managed=False)]}, NOW)[0]
        self.assertIn("will not renew", e["cause"])

    def test_unused_certificate_says_so(self):
        e = exposure.expiring({"certificates": [cert(10, in_use=False)]}, NOW)[0]
        self.assertTrue(e["urgent"])
        self.assertIn("not attached to anything", e["cause"])

    def test_pending_certificate_is_not_an_expiry(self):
        self.assertEqual(exposure.expiring(
            {"certificates": [cert(5, status="PENDING_VALIDATION")]}, NOW), [])

    def test_domain_thresholds(self):
        self.assertEqual(exposure.expiring({"domains": [domain(22)]}, NOW), [])
        self.assertEqual(len(exposure.expiring({"domains": [domain(21)]}, NOW)), 1)
        self.assertEqual(exposure.expiring({"domains": [domain(46, auto=False)]}, NOW), [])
        e = exposure.expiring({"domains": [domain(45, auto=False)]}, NOW)[0]
        self.assertIn("Auto-renew is off", e["cause"])

    def test_soonest_first(self):
        out = exposure.expiring({"certificates": [cert(40), cert(3)],
                                 "domains": [domain(10)]}, NOW)
        self.assertEqual([e["days"] for e in out], [3, 10, 40])


# ---------------------------------------------------------------------------
# Reading, with stub clients
# ---------------------------------------------------------------------------


class Pages:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kw):
        return iter(self.pages)


class StubClient:
    def __init__(self, pages=None, raises=None):
        self._pages, self._raises = pages or {}, raises

    def get_paginator(self, name):
        if self._raises:
            raise self._raises
        return Pages(self._pages.get(name, [{}]))

    def get_apis(self, **kw):
        if self._raises:
            raise self._raises
        return {"Items": []}


def denied(message):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": message}}, "Op")


class Reading(unittest.TestCase):

    def test_one_failed_section_does_not_stop_the_rest(self):
        def make(service, region):
            if service == "route53domains":
                return StubClient(raises=denied("User is not authorized"))
            if service == "cloudfront":
                return StubClient({"list_distributions": [{"DistributionList": {"Items": [
                    {"Id": "E1", "DomainName": "d1.cloudfront.net", "Enabled": True,
                     "WebACLId": "", "DefaultCacheBehavior": {"ViewerProtocolPolicy": "allow-all"},
                     "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
                     "Origins": {"Items": [{"DomainName": "o.example.com"}]}}]}}]})
            return StubClient()
        inv = exposure.read_account(make, ["us-east-2"], since=NOW)
        self.assertEqual(len(inv["cloudfront"]), 1)
        self.assertIsNone(inv["cloudfront"][0]["web_acl"])
        self.assertEqual(exposure.unchecked(inv), ["domain_operations", "domains"])

    def test_scp_denial_counts_as_checked(self):
        def make(service, region):
            if service == "route53domains":
                return StubClient(raises=denied(
                    "... with an explicit deny in a service control policy"))
            return StubClient()
        inv = exposure.read_account(make, ["us-east-2"], since=NOW)
        self.assertEqual(inv["errors"]["domains"]["state"], "blocked")
        self.assertEqual(exposure.unchecked(inv), [])

    def test_regional_failure_is_named_by_region(self):
        def make(service, region):
            if service == "acm" and region == "us-east-1":
                return StubClient(raises=denied("throttled"))
            return StubClient()
        inv = exposure.read_account(make, ["us-east-1", "us-east-2"], since=NOW)
        self.assertEqual(exposure.unchecked(inv), ["certificates/us-east-1"])


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def facts_doc(inv, status="ok"):
    return {"window": {"end": "2026-10-28T12:00:00Z"}, "sources": {"router": "ok", "layer": "ok"},
            "routed_findings": [], "layer_tag": {"tenant": {"total": 0, "items": []}},
            "exposure": {"status": status, "unchecked": exposure.unchecked(inv) if inv else [],
                         "findings": facts._capped(exposure.assess(inv) if inv else []),
                         "expiring": exposure.expiring(inv, NOW) if inv else [],
                         "not_checked": list(exposure.NOT_CHECKED), "inventory": inv}}


def body(doc):
    tz, name, note = digest.load_zone("America/Denver")
    account = {"partner": "oeight", "client": "oeight", "app": "arc8",
               "environment": "dev", "account_id": DEV}
    data = {"alarms_new": [], "alarms_total": 0, "alarms_in_alarm": [], "exceptions": []}
    start, end = facts.window(NOW)
    return digest.render(account, data, doc, "ok", start, end, tz, name, note)


class Digest(unittest.TestCase):

    def test_arc8_dev_section(self):
        text = body(facts_doc(arc8_dev()))
        self.assertIn("== Reachable from the internet", text)
        self.assertIn("Reachable, with no protection in front of it (4):", text)
        self.assertIn("! SSH (22) is open to the whole internet", text)
        self.assertIn("What to do about it is your team's decision.", text)
        self.assertIn("Not checked at all: Lambda function URLs", text)
        # The section comes before the team section and the monitoring.
        self.assertLess(text.index("Reachable from the internet"), text.index("What your team did"))

    def test_quiet_account_says_what_was_checked(self):
        inv = {s: [] for s in exposure.SECTIONS}
        inv["errors"] = {}
        text = body(facts_doc(inv))
        self.assertIn("Nothing we check is reachable from the internet.", text)
        self.assertIn("Not checked at all:", text)

    def test_unreadable_account_is_not_a_quiet_one(self):
        text = body(facts_doc(None, status="error: AccessDenied"))
        self.assertIn("Could not be checked today", text)
        self.assertNotIn("Nothing we check", text)

    def test_older_facts_without_exposure_say_not_checked(self):
        doc = facts_doc(None)
        del doc["exposure"]
        self.assertIn("Not checked yet for this account.", body(doc))

    def test_expiring_lines_need_attention(self):
        inv = arc8_dev()
        inv["certificates"] = [cert(9)]
        text = body(facts_doc(inv))
        attention = text[text.index("Needs your attention"):text.index("Reachable from")]
        self.assertIn("Expiring soon (1):", attention)
        self.assertIn("certificate dev.example.com, us-east-1", attention)
        self.assertIn("in 9 days", attention)


# ---------------------------------------------------------------------------
# The expiry scan, and the router
# ---------------------------------------------------------------------------


class Warnings(unittest.TestCase):

    def test_ceilings(self):
        self.assertEqual(expiry.ceiling("dev", True), "digest")
        self.assertEqual(expiry.ceiling("test", True), "digest_and_jira")
        self.assertEqual(expiry.ceiling("uat", True), "digest_and_jira")
        self.assertEqual(expiry.ceiling("prod", False), "digest_and_jira")
        self.assertEqual(expiry.ceiling("prod", True), "pagerduty")

    def test_name_is_stable_across_days(self):
        a = exposure.expiring({"certificates": [cert(20)]}, NOW)[0]
        b = exposure.expiring({"certificates": [cert(20)]}, NOW + datetime.timedelta(days=1))[0]
        wa, wb = expiry.warning(DEV, "dev", a, NOW), expiry.warning(DEV, "dev", b, NOW)
        self.assertEqual(wa["alarm"], wb["alarm"])
        self.assertNotEqual(wa["assessment"], wb["assessment"])


class StubSSM:
    def get_parameter(self, Name):
        return {"Parameter": {"Value": json.dumps(TIERS)}}


class StubSQS:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, json.loads(MessageBody)))


def routed_event(account_id, tier, days):
    item = exposure.expiring({"certificates": [cert(days)]}, NOW)[0]
    return {"source": "platform.expiry", "detail-type": "expiry-warning",
            "account": AUDIT, "region": "us-east-2", "time": "2026-10-28T14:00:00Z",
            "detail": json.loads(json.dumps(expiry.warning(account_id, tier, item, NOW),
                                            default=str))}


class Router(unittest.TestCase):

    def setUp(self):
        self._saved = (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
                       router.JIRA_QUEUE_URL, router.page)
        router.ssm, router.sqs = StubSSM(), StubSQS()
        router._now = lambda: NOW
        router.REVIEW_QUEUE_URL, router.JIRA_QUEUE_URL = "review", "jira"
        router._TIER_CACHE.clear()
        self.paged = []
        router.page = lambda norm, routing, why: self.paged.append(routing) or {"status": 202}

    def tearDown(self):
        (router.ssm, router.sqs, router._now, router.REVIEW_QUEUE_URL,
         router.JIRA_QUEUE_URL, router.page) = self._saved
        router._TIER_CACHE.clear()

    def test_recognised_and_attributed_to_the_member_account(self):
        out = router.handler(routed_event(DEV, "dev", 9), None)
        self.assertEqual(out["record_type"], "routing-decision")
        self.assertEqual(out["account_id"], DEV)
        self.assertEqual(out["title"], "platform-expiry-certificate")
        self.assertEqual(out["resource"], "dev.example.com")
        self.assertEqual(out["routing"], "digest")
        self.assertEqual(self.paged, [])

    def test_an_emitter_that_says_page_in_test_is_refused(self):
        ev = routed_event(TEST, "test", 9)
        ev["detail"]["routing"] = "pagerduty"
        out = router.handler(ev, None)
        self.assertEqual(out["routing"], "digest_and_jira")
        self.assertIn("REFUSED", out["why"])
        self.assertEqual(self.paged, [])

    def test_urgent_in_prod_pages(self):
        out = router.handler(routed_event(PROD, "prod", 9), None)
        self.assertEqual(out["routing"], "pagerduty")
        self.assertEqual(self.paged, ["pagerduty"])

    def test_not_urgent_in_prod_tickets(self):
        out = router.handler(routed_event(PROD, "prod", 30), None)
        self.assertEqual(out["routing"], "digest_and_jira")
        self.assertEqual(self.paged, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
