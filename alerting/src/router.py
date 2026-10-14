"""The single routing layer. Everything that wants attention arrives here.

Prompt 5.3, design/06 "Alerting and routing":

    CloudWatch alarm -+                        +-> PagerDuty (actionable now)
    Config finding    +-> EventBridge -> routing +-> Jira (owned, dated)
    Security Hub      |                        +-> Review queue (no page)
    GuardDuty        -+                        +-> Commercial (conversation)

Routing is by severity AND environment. Production canary failure pages; a dev
environment CPU alarm does not.

Where the decision comes from
-----------------------------
design/06a, which is authoritative and already carries the whole model: each
tier has a `default_routing`, each alarm may override it with
`severity_by_tier`, and `routing_destinations` says what each routing class
actually does:

    pagerduty            pages, ticket, digest
    jira_and_commercial  ticket, digest, commercial event   -- no page
    digest_and_jira      ticket, digest                     -- no page
    digest               digest                             -- no page
    review_queue         nothing but the record             -- no page

Nothing here re-derives that table. The instrumentation Lambda already computed
the routing class when it created the alarm and wrote it to the alarm's
`platform-routing` tag, so this reads the decision rather than making it a
second time and risking a different answer.

The fourth destination
----------------------
The prompt names three. design/06's own diagram has four, the fourth being
"defined by who needs to have a conversation, not by severity", and 06a's
`jira_and_commercial` depends on it. Built, because dropping it would silently
downgrade every alarm 06a routes that way.

Why paging is a direct POST rather than SNS
-------------------------------------------
The existing SNS subscription posts to PagerDuty's CloudWatch integration,
which parses an SNS-wrapped CloudWatch alarm and nothing else. The prompt
requires the page to carry the full dossier, which means arbitrary JSON in
custom_details, which means Events API v2.

It also means a dedup_key, and that turns out to be the load-bearing part. An
alarm fires and pages; minutes later the response engine finishes gathering and
sends the dossier. Without a dedup key those are two incidents about one
outage. With one, the second updates the first, so the page arrives immediately
and the evidence catches up.
"""
import datetime
import hashlib
import json
import os
import urllib.error
import urllib.request

import boto3

import eventspec

# No cloudwatch client. The router deliberately makes no cross-account AWS call
# on the alert path — everything it needs arrives in the event. See
# alarm_routing() for what that replaced and why.
secrets = boto3.client("secretsmanager")
sqs = boto3.client("sqs")
ssm = boto3.client("ssm")

PD_SECRET_NAME = os.environ.get("PAGERDUTY_SECRET_NAME", "")
PD_SECRET_NAME_LOW = os.environ.get("PAGERDUTY_SECRET_NAME_LOW", "")
JIRA_QUEUE_URL = os.environ.get("JIRA_QUEUE_URL", "")
REVIEW_QUEUE_URL = os.environ.get("REVIEW_QUEUE_URL", "")
COMMERCIAL_QUEUE_URL = os.environ.get("COMMERCIAL_QUEUE_URL", "")
# docs/hipaa-eligibility.md E8. A CRITICAL eligibility finding in a HIPAA
# tenant's dev or test reaches that tenant's named contact and a ticket in the
# tenant's own Jira, now rather than in the next digest. The topic's
# subscribers (eligibility/10-hipaa-eligibility.yaml) do the delivering; the
# DECISION that it goes there is made here, like every other routing decision.
TENANT_ALERT_TOPIC_ARN = os.environ.get("TENANT_ALERT_TOPIC_ARN", "")
ROUTING_LOG_GROUP = os.environ.get("ROUTING_LOG_GROUP", "/aws/platform/routing")

PD_ENQUEUE = "https://events.pagerduty.com/v2/enqueue"

# 06a's event alarms, baked in at packaging by
# scripts/lib/extract_event_specs.py from the same eventspec.py the
# instrumentation Lambda builds the rules from.
_SPEC_PATH = os.path.join(os.path.dirname(__file__), "event_specs.json")
try:
    with open(_SPEC_PATH, encoding="utf-8") as _fh:
        EVENT_SPECS = json.load(_fh)
except FileNotFoundError:
    # Not fatal. A router without the specs still routes alarms, findings and
    # dossiers; what it loses is the ability to recognise 06a's event alarms,
    # and those then land in the review queue rather than vanishing.
    EVENT_SPECS = []

# Account -> environment tier, read from SSM in this account.
#
# Without it the router assumed "prod" for anything with no tier of its own,
# which meant Security Hub and GuardDuty findings in a DEV sandbox were
# evaluated as production. Confirmed in the live log: findings from 754280127660
# recorded environment_tier null and "prod defaults to pagerduty". Only their
# MEDIUM severity stopped them paging; a CRITICAL finding in a sandbox would
# have woken somebody.
ACCOUNT_TIER_PARAM = os.environ.get(
    "ACCOUNT_TIER_PARAM", "/platform/routing/account-tiers")
_TIER_CACHE = {}

# B-119 / B-127. Which tenant an account belongs to, and which of that tenant's
# PagerDuty services an alarm should reach.
#
# Until this existed `routing_key` chose between exactly two secrets, both
# AltDigital's own services, and the word "tenant" did not appear in this file.
# Every tenant's alarm would have paged AltDigital, and the per-tenant services
# created by prompt 12.2 had no consumer at all.
TENANT_FACTS_PARAM = os.environ.get(
    "TENANT_FACTS_PARAM", "/platform/change/tenant-facts")
TENANT_KEY_PREFIX = os.environ.get(
    "TENANT_KEY_PREFIX", "platform/pagerduty/tenant")
# Matches local.infra_object in pagerduty/modules/tenant/main.tf. Two copies of
# one string, in two languages, with no way to derive either from the other --
# so it is named in both rather than spelled inline in either.
INFRA_OBJECT = "infra"
_FACTS_CACHE = {}
_KEY_CACHE = {}

# design/06a routing_destinations, duplicated here for one specific reason: this
# Lambda runs in the Audit account and 06a ships with the instrumentation
# package into member accounts. Shipping the whole spec here to read five lines
# of it would be worse. The instrumentation Lambda is authoritative and computes
# the routing class; this only needs to know what each class DOES.
#
# If these ever disagree, the tag wins, because the tag is what the alarm was
# created with.
DESTINATIONS = {
    "pagerduty":           {"pages": True,  "ticket": True,  "digest": True},
    "jira_and_commercial": {"pages": False, "ticket": True,  "digest": True,
                            "commercial_event": True},
    "digest_and_jira":     {"pages": False, "ticket": True,  "digest": True},
    "digest":              {"pages": False, "ticket": False, "digest": True},
    "review_queue":        {"pages": False, "ticket": False, "digest": False},
}

# Tier defaults from 06a, used only when an event carries no routing decision of
# its own — a Security Hub or GuardDuty finding, which no alarm tag covers.
TIER_DEFAULT_ROUTING = {
    "dev": "digest", "test": "digest_and_jira",
    "uat": "jira_and_commercial", "prod": "pagerduty",
}

# Urgency order of the routing classes, for refusing an emitter's escalation.
# review_queue ranks lowest: it reaches nobody until a human looks.
ROUTING_RANK = {
    "review_queue": 0, "digest": 1, "digest_and_jira": 2,
    "jira_and_commercial": 3, "pagerduty": 4,
}

# Finding severities that page in a production account. Below this they are
# tickets, because design/06 is explicit that nothing pages which a human
# cannot act on right now, and a MEDIUM GuardDuty finding at 3am is not that.
FINDING_PAGES_AT = {"CRITICAL", "HIGH"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


TIER_MAP_MISS_REFRESH_SECONDS = int(os.environ.get("TIER_MAP_MISS_REFRESH_SECONDS", "60"))


def _load_tier_map():
    try:
        raw = ssm.get_parameter(Name=ACCOUNT_TIER_PARAM)["Parameter"]["Value"]
        _TIER_CACHE["map"] = json.loads(raw)
        _TIER_CACHE.pop("error", None)
    except Exception as exc:
        _TIER_CACHE["map"] = _TIER_CACHE.get("map") or {}
        _TIER_CACHE["error"] = f"{type(exc).__name__}"
    _TIER_CACHE["loaded_at"] = _now()


def _tier_map_age():
    loaded = _TIER_CACHE.get("loaded_at")
    return (_now() - loaded).total_seconds() if loaded else float("inf")


def account_tier(account_id):
    """Environment tier for an account. Returns (tier, note).

    Cached for the life of the execution environment: the map changes when an
    account is vested, which is rarely, and a Parameter Store read per event
    would put a throttleable dependency on the path that pages people.

    An account that is not in the map returns "prod", which PAGES. That is the
    deliberate direction to fail: treating an unknown account as dev would
    silence a real production incident, while treating it as prod produces a
    page that says the tier was unknown. The note travels into the routing
    record so the gap is visible rather than inferred.
    """
    if not account_id:
        return "prod", "no account id on the event; treated as prod"
    if "map" not in _TIER_CACHE:
        _load_tier_map()
    mapping = _TIER_CACHE["map"]
    if account_id not in mapping and _tier_map_age() >= TIER_MAP_MISS_REFRESH_SECONDS:
        # A MISS re-reads the map before concluding the account is unknown.
        #
        # The cache used to live for the whole execution environment, so an
        # account vested after the container warmed stayed "unknown" -- and
        # therefore prod, and therefore paging -- until AWS happened to recycle
        # it. On the first real vesting (Arc8 dev and test, 2026-10-01) a
        # Config.1 finding paged as [prod] at 22:40, after onboarding had
        # published both accounts as dev and test. Hits keep the cheap path;
        # only a miss pays for a read, at most once a minute, so a burst of
        # findings from a genuinely unknown account cannot hammer SSM.
        _load_tier_map()
        mapping = _TIER_CACHE["map"]
    if account_id in mapping:
        return mapping[account_id], None
    why = _TIER_CACHE.get("error")
    return "prod", (
        f"account {account_id} is not in {ACCOUNT_TIER_PARAM}"
        + (f" ({why} reading it)" if why else "")
        + ". Treated as prod, which pages — an unknown account is more likely "
          "to be an unregistered production account than a forgotten sandbox. "
          "Run scripts/sync-account-tiers.sh.")


# ---------------------------------------------------------------------------
# Normalising the sources
# ---------------------------------------------------------------------------
# Four event shapes arrive and every one puts the account, the resource and the
# severity somewhere different. Everything downstream works on the normalised
# form, so adding a fifth source is a function here and nothing else.


def from_alarm(event):
    d = event.get("detail") or {}
    name = d.get("alarmName") or ""
    state = (d.get("state") or {}).get("value")
    return {
        "kind": "alarm",
        "source": "cloudwatch",
        "account_id": event.get("account"),
        "region": event.get("region"),
        "title": name,
        "state": state,
        # The alarm's own description, which carries the routing trailer. The
        # RESOURCE comes out of that trailer rather than out of the alarm name:
        # the name is platform-auto-RESOURCEID-ALARMID and both halves may
        # contain dashes, so parsing it is guesswork. B-031.
        "description": (d.get("configuration") or {}).get("description"),
        "resource": name,
        # No alarm_arn. It was here for the tag lookup that turned out to be
        # impossible, and it crashed on the way out: event.get("resources",
        # [None])[0] raises IndexError when `resources` is PRESENT and empty,
        # because a default only applies to a missing key. EventBridge supplies
        # an empty list, so the router died on an event it was built to handle
        # and the whole invocation dead-lettered.
        "reason": (d.get("state") or {}).get("reason"),
        "occurred_at": event.get("time"),
        # Resolved from the alarm's tags below. An alarm the platform did not
        # create has none, which is itself a finding — see route().
        "routing": None,
    }


def from_security_hub(event):
    d = event.get("detail") or {}
    findings = d.get("findings") or [{}]
    f = findings[0]
    res = (f.get("Resources") or [{}])[0]
    return {
        "kind": "finding",
        "source": "securityhub",
        "account_id": f.get("AwsAccountId") or event.get("account"),
        "region": event.get("region"),
        "title": f.get("Title") or "Security Hub finding",
        "severity": ((f.get("Severity") or {}).get("Label") or "").upper(),
        "resource": res.get("Id") or "unknown",
        "reason": f.get("Description"),
        "occurred_at": f.get("UpdatedAt") or event.get("time"),
        "generator": f.get("GeneratorId"),
        "routing": None,
    }


def from_guardduty(event):
    d = event.get("detail") or {}
    # GuardDuty severity is numeric 1-8.9. The published bands are
    # low 1-3.9, medium 4-6.9, high 7-8.9; 9+ is documented as reserved and has
    # never been observed, so it is folded into CRITICAL rather than dropped.
    sev = d.get("severity") or 0
    label = ("CRITICAL" if sev >= 9 else "HIGH" if sev >= 7
             else "MEDIUM" if sev >= 4 else "LOW")
    res = d.get("resource") or {}
    return {
        "kind": "finding",
        "source": "guardduty",
        "account_id": d.get("accountId") or event.get("account"),
        "region": event.get("region"),
        "title": d.get("title") or "GuardDuty finding",
        "severity": label,
        "resource": (res.get("instanceDetails") or {}).get("instanceId")
                    or (res.get("accessKeyDetails") or {}).get("userName")
                    or res.get("resourceType") or "unknown",
        "reason": d.get("description"),
        "occurred_at": d.get("updatedAt") or event.get("time"),
        "routing": None,
    }


def from_config(event):
    d = event.get("detail") or {}
    ni = (d.get("newEvaluationResult") or {})
    return {
        "kind": "finding",
        "source": "config",
        "account_id": event.get("account"),
        "region": event.get("region"),
        "title": f"Config rule {d.get('configRuleName')} NON_COMPLIANT",
        # Config has no severity. Treated as MEDIUM so it tickets rather than
        # pages: a drifted control is real and dated work, and waking somebody
        # for it is how the rotation learns to ignore the pager.
        "severity": "MEDIUM",
        "resource": (d.get("resourceId") or "unknown"),
        "reason": ni.get("annotation") or d.get("configRuleName"),
        "occurred_at": event.get("time"),
        "routing": None,
    }


def from_dossier(event):
    """The response engine's incident readout, after it has tried and failed."""
    d = event.get("detail") or {}
    alarm = d.get("alarm") or {}
    return {
        "kind": "dossier",
        # The emitting source, not a hardcoded name. There are two dossier
        # emitters now — platform.response and platform.recovery — and the
        # routing log is what answers "why was I not paged about this". A log
        # line attributing a restore test to the response engine sends whoever
        # is reading it to the wrong component.
        "source": event.get("source") or "response-engine",
        "account_id": alarm.get("account_id") or event.get("account"),
        "region": event.get("region"),
        "title": alarm.get("name") or "response engine",
        "resource": alarm.get("resource_id") or "unknown",
        "environment_tier": alarm.get("environment_tier"),
        "reason": (d.get("assessment") or {}).get("probable_cause"),
        "occurred_at": d.get("recorded_at") or event.get("time"),
        "state": d.get("current_state"),
        "dossier": d,
        # The emitter's own routing class, if it stated one. Honoured only as a
        # DOWNGRADE -- see decide(). The response engine states none and is
        # unaffected; recovery/src/worker.py states one for a restore test that
        # was merely slow.
        "routing": d.get("routing"),
    }


def from_platform_event(event, spec):
    """A raw AWS event that matches one of 06a's `metric: event` alarms.

    The event arrives exactly as AWS emitted it — the forwarding rule cannot add
    anything, because an EventBridge rule cannot transform its input when the
    target is an event bus. So the 06a alarm is identified by matching the same
    pattern the rule was built from, and everything else is read out of the
    event itself.
    """
    d = event.get("detail") or {}

    # Two shapes, because the sources disagree. An RDS or Auto Scaling event
    # puts ARN STRINGS in the top-level `resources`; a CloudTrail event leaves
    # that empty and puts a list of DICTS under detail.resources
    # ({"ARN": ..., "type": ...}). Assuming strings crashed the router on the
    # very event class this was written to handle.
    candidates = list(event.get("resources") or []) + list(d.get("resources") or [])
    resource = None
    for c in candidates:
        arn = c.get("ARN") if isinstance(c, dict) else c
        if isinstance(arn, str) and arn:
            resource = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            break
    if not resource:
        # CloudTrail puts the target under requestParameters, and what it is
        # called depends on the service. These two cover the only cloudtrail
        # source 06a defines today (S3); an unmatched one falls back to the
        # event name, which at least says what happened.
        rp = d.get("requestParameters") or {}
        resource = (rp.get("bucketName") or d.get("SourceIdentifier")
                    or d.get("AutoScalingGroupName") or d.get("eventName")
                    or "unknown")
    return {
        "kind": "platform-event",
        "source": (event.get("source") or "aws").replace("aws.", ""),
        "account_id": event.get("account"),
        "region": event.get("region"),
        "title": f"{spec['alarm_id']} — {d.get('eventName') or d.get('Message') or event.get('detail-type')}",
        "resource": resource,
        "alarm_id": spec["alarm_id"],
        "resource_type": spec["resource_type"],
        "severity_by_tier": spec.get("severity_by_tier") or {},
        "reason": spec.get("note"),
        "occurred_at": event.get("time"),
        "actor": ((d.get("userIdentity") or {}).get("arn")
                  or (d.get("userIdentity") or {}).get("type")),
        "raw_detail": d,
        "routing": None,
    }


def match_event_spec(event):
    """Which 06a event alarm, if any, this raw AWS event belongs to."""
    for spec in EVENT_SPECS:
        if eventspec.matches(spec["pattern"], event):
            return spec
    return None


def from_hipaa_eligibility(event):
    """A HIPAA eligibility detection, upstream change or attachment check.

    docs/hipaa-eligibility.md §4.4. The emitters (eligibility/src/sweep.py,
    upstream.py) state a SEVERITY, which is a fact about the event, and never a
    destination. The account the detection is about is in the detail, not in
    the envelope: the sweep runs in Audit, and routing by the envelope account
    would treat every dev-account detection as production.
    """
    d = event.get("detail") or {}
    return {
        "kind": "eligibility",
        "source": event.get("source") or "platform.eligibility",
        "account_id": d.get("account_id") or event.get("account"),
        "region": event.get("region"),
        # Stable per (account, check, subject), so a detection repeated by the
        # next day's sweep updates one incident or ticket instead of opening a
        # second. dedup_key() hashes account, resource and title.
        "title": f"HIPAA eligibility: {d.get('check') or 'detection'} — {d.get('subject') or 'unknown'}",
        "resource": d.get("subject") or "unknown",
        "severity": d.get("severity"),
        "reason": d.get("summary"),
        "occurred_at": d.get("detected_at") or event.get("time"),
        "dossier": d,
        "routing": d.get("routing"),
    }


NORMALISERS = {
    "CloudWatch Alarm State Change": from_alarm,
    "Security Hub Findings - Imported": from_security_hub,
    "GuardDuty Finding": from_guardduty,
    "Config Rules Compliance Change": from_config,
    "Platform Incident Dossier": from_dossier,
    # The evidence pipeline's own findings: collect, sender, completeness and
    # custody all emit this, already in dossier shape (alarm, assessment,
    # current_state, routing). It had no entry, so every one fell to the
    # review queue as "unrecognised event shape" -- including "this tenant's
    # evidence is reaching no Truveon tenant", which sat there unread from
    # Arc8's vesting until a runaway page found it (B-156).
    #
    # Every emitter declares its routing, and none declares one that pages.
    # The events come from Audit, a prod-tier account, so the dossier default
    # WOULD page; the declared class is honoured only as a downgrade, as for
    # any dossier. An emitter that forgets to declare one pages -- loud, and
    # the right way to fail for a completeness control.
    "evidence-completeness": from_dossier,
    # docs/hipaa-eligibility.md. Only HIPAA tenants' accounts produce these;
    # tenants without HIPAA exposure are never swept, so nothing arrives here
    # about them.
    "hipaa-eligibility": from_hipaa_eligibility,
    # telemetry/src/gap.py. Already dossier-shaped (alarm, assessment,
    # current_state, routing), and with no entry here every one fell to the
    # review queue as "unrecognised event shape" from the day the scan was
    # deployed (B-148).
    #
    # The scan declares its own class: digest in dev, digest_and_jira when a
    # gap has not yet outlived the association reinstall, pagerduty only when
    # it has. The dossier rule honours the first two as downgrades and refuses
    # pagerduty wherever the tier would not page, so a persistent gap in test
    # tickets rather than pages. The scan's tier is in the detail, but the
    # router looks the tier up from alarm.account_id itself: the emitter's
    # copy of a routing fact is not the one that decides.
    "telemetry-gap": from_dossier,
}


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


# Written into every AlarmDescription by the instrumentation Lambda. Must match
# ROUTING_MARKER there.
ROUTING_MARKER = "[platform-routing]"


def alarm_routing(description):
    """Read the routing class the instrumentation Lambda already decided.

    Out of the alarm's DESCRIPTION, which arrives inside the event as
    detail.configuration.description — not out of its tags.

    The tags carry the same facts and were the obvious source, and they are
    unreachable. CloudWatch alarms have no resource-based policy, so there is no
    IAM grant that lets the Audit account read another account's alarm tags; the
    call fails and every alarm falls through to the review queue. Found on the
    first live event, where the log read
    "could not read alarm tags: ClientError".

    Reading the description is better than fixing that would have been. The
    alternative was assuming a role per member account per alarm, which puts a
    cross-account round trip on the path that pages people, at the moment an
    incident is starting and the account may itself be unhealthy.

    Returns (routing, tier, note).
    """
    if not description or ROUTING_MARKER not in description:
        return None, None, ("no routing marker in the alarm description — this "
                            "alarm was not created by the instrumentation "
                            "Lambda, or predates the marker")
    trailer = description.split(ROUTING_MARKER, 1)[1]
    fields = {}
    for token in trailer.split():
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v
    return fields.get("routing"), fields.get("tier"), None


def decide(norm):
    """Turn a normalised event into a routing class.

    Every path ends in a named class, and an unrecognised one is treated as
    pagerduty rather than dropped — the same else rule the response engine
    follows. An event nobody anticipated is the one most likely to matter.
    """
    if norm["kind"] == "alarm":
        routing, tier, note = alarm_routing(norm.get("description"))
        norm["environment_tier"] = tier
        if norm.get("description") and ROUTING_MARKER in norm["description"]:
            trailer = norm["description"].split(ROUTING_MARKER, 1)[1]
            for token in trailer.split():
                if token.startswith("resource-id="):
                    # The real resource, so the page names the database rather
                    # than the alarm about the database.
                    norm["resource"] = token.split("=", 1)[1]
                # B-127. Which team owns the resource, and whether the resource
                # said so or the instrumentation Lambda defaulted it. Both are
                # carried: they route the same way today, and a reader that
                # could not tell them apart would make the default permanent by
                # making it invisible.
                elif token.startswith("layer-source="):
                    norm["layer_source"] = token.split("=", 1)[1]
                elif token.startswith("layer="):
                    norm["layer"] = token.split("=", 1)[1]
                # D-028. An emitter may DOWNGRADE its own event, never escalate
                # it, so only "low" means anything; any other value is ignored.
                elif token == "urgency=low":
                    norm["urgency"] = "low"
        # D-028. Alarms written into templates (backup, containment, the log
        # archive) cannot know their account's tier, so their marker carries
        # none: look it up, exactly as findings and dossiers do, rather than
        # page() defaulting the absence to prod.
        if routing and not norm.get("environment_tier"):
            tier, tier_note = account_tier(norm.get("account_id"))
            norm["environment_tier"] = tier
            if tier_note:
                norm["tier_note"] = tier_note
        if routing:
            return routing, f"alarm tag platform-routing={routing}"
        # No tag. Either an alarm the platform did not create, or one created
        # before tagging existed. Not silently dropped and not silently paged:
        # it goes to the review queue WITH the reason, because the finding is
        # that an untagged alarm exists rather than whatever the alarm says.
        return "review_queue", (
            f"no platform-routing tag ({note or 'alarm not created by the platform'}). "
            "Routed to review rather than guessed at. An untagged alarm is a gap "
            "in instrumentation, not an incident.")

    if norm["kind"] == "platform-event":
        tier, note = account_tier(norm.get("account_id"))
        norm["environment_tier"] = tier
        sev = norm.get("severity_by_tier") or {}
        if tier not in sev:
            # 06a omits a tier to mean "not watched here". The rule should not
            # have been created in that account at all, so this is a mismatch
            # between what is deployed and what the spec says — reviewable, not
            # pageable.
            return "review_queue", (
                f"06a does not watch {norm.get('alarm_id')} at tier {tier}, yet an "
                "event rule forwarded one. The rule is stale relative to the "
                "specification." + (f" {note}" if note else ""))
        return sev[tier], (f"06a {norm.get('alarm_id')} at tier {tier}"
                           + (f". {note}" if note else ""))

    if norm["kind"] == "dossier":
        # The response engine only emits a dossier when it has decided a human
        # is needed, so the routing class is not in question — what varies is
        # urgency, and that is the tier's business below.
        # LOOK THE TIER UP before falling back to prod.
        #
        # This defaulted straight to "prod" when a dossier carried no tier of
        # its own, and the consequence arrived the first time an emitter other
        # than the response engine sent one: a change-reconciliation finding
        # about a DEV sandbox deployment raised a real PagerDuty incident,
        # because the dossier had an account_id and no environment_tier.
        #
        # The response engine always sets the tier, so the gap was invisible
        # for two phases. Every later emitter has to remember to, and
        # "remember to" is what the shared account-tier map exists to remove --
        # the finding path below has looked the tier up all along.
        #
        # account_tier() still answers "prod" for an account genuinely absent
        # from the map, which pages, and that remains the right way to fail.
        tier = norm.get("environment_tier")
        if not tier:
            tier, note = account_tier(norm.get("account_id"))
            if note:
                norm["tier_note"] = note
            # Written back so the routing record says which tier decided it.
            # It did not until 2026-10-14, so every dossier's record logged
            # environment_tier null; found when the digest began reading them.
            norm["environment_tier"] = tier
        default = "pagerduty" if tier in ("uat", "prod") else "digest_and_jira"

        # An emitter may DOWNGRADE its own dossier, and may never escalate one.
        #
        # The asymmetry is the point. Downgrading is a statement about the
        # emitter's own event that only the emitter can make — a restore test
        # that merely ran slow knows it is not an outage, and nothing here
        # could work that out from the tier. Escalating is a statement about
        # who gets woken up, and that decision belongs in one place: design/06a
        # and the tier, read here.
        #
        # B-041 is the reason it is written as a rule rather than left to each
        # emitter's judgement. Two components reduced 06a's five routing
        # classes to "pages" and "does not page", picked the wrong half, and
        # sent non-paging alarms to a PagerDuty-subscribed topic — five false
        # pages, and a full day to trace, because a wrong routing decision
        # looks exactly like a working one.
        declared = norm.get("routing")
        if declared:
            spec = DESTINATIONS.get(declared)
            if spec is None:
                return default, (
                    f"emitter declared routing class '{declared}', which is not "
                    f"one of 06a's. Ignored and routed by tier {tier} instead — "
                    "an unrecognised class is a bug in the emitter, not a "
                    "reason to route somewhere undefined")
            if spec["pages"] and not DESTINATIONS[default]["pages"]:
                return default, (
                    f"emitter declared '{declared}', which pages, but tier "
                    f"{tier} does not. REFUSED: an emitter may lower its own "
                    "urgency and may not raise it — escalation is the tier's "
                    "decision (06a). See B-041")
            return declared, (
                f"emitter declared '{declared}'; tier {tier} would have given "
                f"'{default}'. Honoured as a downgrade")

        # Named by SOURCE. There are three dossier emitters now, and this
        # string is the answer to "why was I not paged about this" — attributing
        # a security containment to the response engine sends whoever is reading
        # it to the wrong component.
        return default, f"{norm.get('source') or 'dossier'} escalation, tier {tier}"

    if norm["kind"] == "eligibility":
        # Routed exactly as a finding is — tier default, and production pages
        # only at HIGH or CRITICAL — with one addition: the emitter may
        # DOWNGRADE. The upstream-list check runs in Platform Tooling, a prod
        # account, and an AWS page edit is a review item, not a 3am page; only
        # the emitter knows that, so it says so. It may never escalate (B-041,
        # B-117): a declared class that pages where the tier default would not
        # is refused.
        tier, note = account_tier(norm.get("account_id"))
        norm["environment_tier"] = tier
        default = TIER_DEFAULT_ROUTING.get(tier, "digest")
        sev = (norm.get("severity") or "").upper()
        suffix = f". {note}" if note else ""
        why = f"hipaa-eligibility, tier default for {tier}, severity {sev or 'UNRATED'}"
        # E8 (Jamie, 2026-10-05). The one place this router raises an
        # eligibility event above its tier default, and it is the router doing
        # it, not the emitter. A regulated-tagged resource on a non-eligible
        # service in dev means a design that will put patient data where the
        # BAA does not reach: a certain go-live blocker that should not wait
        # for a digest. It is ticketed and sent to the tenant's contact. It
        # never pages: dev is not production.
        if sev == "CRITICAL" and tier in ("dev", "test"):
            default = "digest_and_jira"
            norm["tenant_alert"] = True
            why = (f"hipaa-eligibility CRITICAL in {tier}: ticketed and sent to the tenant "
                   "contact immediately (E8), not left for the digest")
        if default == "pagerduty" and sev not in FINDING_PAGES_AT:
            default, why = "digest_and_jira", (
                f"{tier} defaults to pagerduty, but a {sev or 'UNRATED'} eligibility "
                "detection is not actionable right now — ticketed instead")
        declared = norm.get("routing")
        if declared and declared != default:
            spec = DESTINATIONS.get(declared)
            if spec is None:
                return default, (f"{why}. Emitter declared unknown class '{declared}'; "
                                  "ignored" + suffix)
            # Stricter than the dossier rule above, which only refuses a class
            # that pages. Here any class ranked above the default is refused,
            # so a digest line cannot become a commercial ticket either.
            if ROUTING_RANK.get(declared, 99) > ROUTING_RANK.get(default, 0):
                return default, (f"{why}. Emitter declared '{declared}', above the tier "
                                 "default; REFUSED — an emitter may not escalate (B-041)" + suffix)
            return declared, f"{why}. Emitter declared '{declared}'; honoured as a downgrade{suffix}"
        return default, why + suffix

    # Findings. No routing decision travels with them, so the tier default
    # applies — and the tier is now LOOKED UP rather than assumed. It used to
    # default to prod for everything, which meant findings in a dev sandbox were
    # evaluated as production.
    tier, note = account_tier(norm.get("account_id"))
    norm["environment_tier"] = tier
    default = TIER_DEFAULT_ROUTING.get(tier, "digest")
    sev = (norm.get("severity") or "").upper()
    suffix = f". {note}" if note else ""
    # Floored by the finding's own severity: a LOW GuardDuty finding in
    # production is not a page just because production pages by default.
    if default == "pagerduty" and sev not in FINDING_PAGES_AT:
        return "digest_and_jira", (
            f"{tier} defaults to pagerduty, but a {sev or 'UNRATED'} finding is "
            "not actionable right now — ticketed instead" + suffix)
    return default, f"tier default for {tier}, severity {sev or 'UNRATED'}{suffix}"


# ---------------------------------------------------------------------------
# Delivering
# ---------------------------------------------------------------------------


def dedup_key(norm):
    """One incident per (account, resource, alarm), not one per event.

    This is what merges the raw alarm with the dossier that arrives minutes
    later. Both derive the same key from the same three facts, so PagerDuty
    treats the second as an update to the first — the page goes out
    immediately and the evidence catches up, instead of two pages about one
    outage.
    """
    raw = "|".join([
        norm.get("account_id") or "", norm.get("resource") or "",
        norm.get("title") or "",
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _load_tenant_facts():
    try:
        raw = ssm.get_parameter(Name=TENANT_FACTS_PARAM)["Parameter"]["Value"]
        _FACTS_CACHE["map"] = json.loads(raw)
        _FACTS_CACHE.pop("error", None)
    except Exception as exc:                          # noqa: BLE001
        _FACTS_CACHE["map"] = _FACTS_CACHE.get("map") or {}
        _FACTS_CACHE["error"] = type(exc).__name__
    _FACTS_CACHE["loaded_at"] = _now()


def tenant_facts(account_id):
    """The tenant record for an account, or None.

    A miss re-reads the map, at most once a minute, before concluding the
    account has no tenant -- the same fix as account_tier(). Cached for the
    container, a tenant vested after the container warmed was invisible until
    it recycled, and its alarms paged AltDigital's own service instead.
    """
    if not account_id:
        return None
    if "map" not in _FACTS_CACHE:
        _load_tenant_facts()
    if account_id not in (_FACTS_CACHE["map"] or {}):
        loaded = _FACTS_CACHE.get("loaded_at")
        if not loaded or (_now() - loaded).total_seconds() >= TIER_MAP_MISS_REFRESH_SECONDS:
            _load_tenant_facts()
    return (_FACTS_CACHE["map"] or {}).get(account_id)


def platform_key(tier, low=False):
    """AltDigital's own routing key for this tier.

    The destination for AltDigital's own accounts, and the fallback for every
    tenant lookup that cannot complete. `low` is the emitter's own downgrade
    (urgency=low in the routing marker, D-028): honoured, never the reverse.
    Tenant services keep their own urgency rules; this applies only here.
    """
    name = PD_SECRET_NAME_LOW if (low or tier in ("dev", "test")) and PD_SECRET_NAME_LOW \
        else PD_SECRET_NAME
    if not name:
        return None
    raw = secrets.get_secret_value(SecretId=name)["SecretString"]
    return json.loads(raw).get("events_v2_routing_key")


def routing_key(tier, layer=None, account_id=None, low=False):
    """(key, destination, note) for one alarm.

    Three dimensions, each from a different place so none can be quietly
    re-derived here:

      tenant   the account id, through the tenant facts map in SSM
      tier     the alarm's own platform-tier, parsed from the description
      layer    the alarm's platform-layer, ditto -- B-127

    The LAYER decides who, and it does so independently of the tier. AltDigital
    owns the infrastructure in a tenant's account at every tier, so a dev-tier
    VPC fault is still ours; the application at that same tier belongs to
    whoever owns the application, which for Arc8 is Wayne's team and for a later
    client is the client's own developers.

    Every failure falls back to AltDigital's key WITH A NOTE rather than
    raising. A routing lookup that fails during an incident must not turn a page
    into no page, and the wrong page is recoverable in a way silence is not. The
    note is printed so the fallback is visible rather than inferred.
    """
    # Infrastructure at any tier is AltDigital's to answer for, but it still
    # belongs on the TENANT's infra service rather than the platform's own, so
    # the tenant's own console shows their estate.
    #
    # ONLY an exact "application" routes to the application team. Written this
    # way round deliberately, and the first version was written the other way:
    # `INFRA_OBJECT if (not layer or layer == "infrastructure") else None` sends
    # every value it does not recognise -- a typo, a future third layer, a
    # truncated trailer -- to the APPLICATION team, which is the one direction
    # that wakes a client at 3am for something that may well be ours.
    #
    # The instrumentation Lambda already maps an unrecognised value to
    # infrastructure before it reaches the trailer, so this should be
    # unreachable. It is written to be safe anyway: this function also receives
    # alarms that predate the field, and a routing default should not depend on
    # another component having been correct.
    object_name = None if layer == "application" else INFRA_OBJECT

    facts = tenant_facts(account_id)
    if not facts:
        return platform_key(tier, low), "platform", (
            "no tenant facts for account " + str(account_id) + " in "
            + TENANT_FACTS_PARAM
            + ((" (" + _FACTS_CACHE["error"] + " reading it)")
               if _FACTS_CACHE.get("error") else "")
            + "; paged AltDigital's own service. Correct for AltDigital's own "
              "accounts, a gap for anything else.")

    client = facts.get("client")
    if not client:
        return platform_key(tier, low), "platform", (
            "account " + str(account_id) + " has tenant facts with no client; "
            "paged AltDigital's own service.")

    if object_name is None:
        # Application layer. The object is the account's app, which is the same
        # granularity the PagerDuty services were built at.
        object_name = facts.get("app")
        if not object_name:
            return platform_key(tier, low), "platform", (
                "account " + str(account_id) + " is a tenant account with no "
                "`app` recorded, so its application service cannot be named. "
                "Paged AltDigital rather than guessing which of a tenant's "
                "services should wake somebody.")

    secret_name = "/".join(
        [TENANT_KEY_PREFIX, client, tier or "prod", object_name])
    if secret_name in _KEY_CACHE:
        return _KEY_CACHE[secret_name], "tenant:" + secret_name, None
    try:
        raw = secrets.get_secret_value(SecretId=secret_name)["SecretString"]
        key = json.loads(raw).get("events_v2_routing_key")
    except Exception as exc:                          # noqa: BLE001
        return platform_key(tier, low), "platform", (
            "could not read " + secret_name + " (" + type(exc).__name__
            + "), so this tenant's service could not be paged. Fell back to "
              "AltDigital's own: a page to the wrong rotation is recoverable, "
              "silence is not. Run scripts/sync-pagerduty-tenants.sh.")
    if not key:
        return platform_key(tier, low), "platform", (
            secret_name + " holds no events_v2_routing_key.")
    _KEY_CACHE[secret_name] = key
    return key, "tenant:" + secret_name, None


def page(norm, routing, why):
    tier = norm.get("environment_tier") or "prod"
    key, destination, key_note = routing_key(
        tier, norm.get("layer"), norm.get("account_id"),
        low=(norm.get("urgency") == "low"))
    if key_note:
        # Printed, never swallowed. A fallback nobody can see becomes permanent,
        # and this one routes a tenant's alarm to AltDigital -- which looks
        # exactly like working until the tenant asks why they never hear from us.
        print(json.dumps({"record_type": "routing-key-fallback",
                          "account_id": norm.get("account_id"),
                          "tier": tier, "layer": norm.get("layer"),
                          "layer_source": norm.get("layer_source"),
                          "destination": destination, "why": key_note}))
    if not key:
        raise RuntimeError(
            "No Events API v2 routing key. Refusing to silently not page: the "
            "caller records this and the DLQ retains the event.")

    body = {
        "routing_key": key,
        "event_action": "resolve" if norm.get("state") == "OK" else "trigger",
        "dedup_key": dedup_key(norm),
        "payload": {
            # B-027 will make this readable at a glance. Until then the account
            # and resource are in the summary, because the alarm name alone
            # answers roughly one of the four questions design/07 asks.
            "summary": (f"[{tier}] {norm.get('title')} — "
                        f"{norm.get('resource')} in {norm.get('account_id')}")[:1024],
            "source": f"{norm.get('source')}/{norm.get('account_id')}",
            "severity": "critical" if tier == "prod" else "warning",
            "component": norm.get("resource"),
            "group": norm.get("account_id"),
            "class": norm.get("kind"),
            "timestamp": norm.get("occurred_at"),
            # The whole dossier, which is the point of prompt 5.3. When the
            # response engine has not run there is none, and the payload says
            # so rather than arriving looking complete.
            "custom_details": norm.get("dossier") or {
                "note": "No dossier. Either no runbook covers this alarm, or the "
                        "response engine has not finished gathering — if it has, "
                        "a second event will update this incident in place.",
                "reason": norm.get("reason"),
                "routing_decision": why,
            },
        },
    }

    # ensure_ascii and an explicit charset, both.
    #
    # The first live page rendered an em dash as mojibake in PagerDuty. Dossier
    # text is full of them, so every real page would have carried garbled
    # characters through to whoever was reading it at 3am. ensure_ascii=True
    # escapes non-ASCII to \uXXXX so nothing depends on the receiver guessing an
    # encoding, and the charset header says so regardless.
    req = urllib.request.Request(
        PD_ENQUEUE, data=json.dumps(body, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return {"status": resp.status, "dedup_key": body["dedup_key"],
                "event_action": body["event_action"]}


_sns = None


def tenant_alert(norm, why):
    """Publish to the tenant alert topic, addressed by tenant. E8.

    The tenant is an SNS message attribute, and each contact's subscription
    filters on it, so one topic serves every HIPAA tenant and nobody receives
    another tenant's findings. An account with no tenant record is not
    guessed at: it is reported as not sent, and the Jira queue and the digest
    still carry it.
    """
    global _sns
    if not TENANT_ALERT_TOPIC_ARN:
        return {"sent": False, "reason": "tenant alert topic not configured"}
    facts = tenant_facts(norm.get("account_id")) or {}
    if not facts.get("partner") or not facts.get("client"):
        return {"sent": False, "reason": f"no tenant record for {norm.get('account_id')}"}
    tenant = f"{facts['partner']}-{facts['client']}"
    d = norm.get("dossier") or {}
    text = "\n\n".join(x for x in [
        norm.get("reason") or norm.get("title"),
        f"Account {norm.get('account_id')} ({norm.get('environment_tier')}). "
        f"Resource: {norm.get('resource')}.",
        "A resource tagged DataClassification=regulated is on an AWS service that is "
        "not HIPAA-eligible. In dev or test this is not an incident: it means the "
        "design would put patient data where AltDigital's agreement with AWS does not "
        "cover it, and it cannot go to uat or prod as built. Change the service, or "
        "the classification if the resource will never hold patient data.",
        "Developer guide section 4.16 has the alternatives. A Jira ticket has been "
        "raised in your project.",
    ] if x)
    payload = {"tenant": tenant, "routing_why": why, "norm": norm, "detail": d}
    if _sns is None:
        _sns = boto3.client("sns")
    _sns.publish(
        TopicArn=TENANT_ALERT_TOPIC_ARN,
        Subject=f"HIPAA eligibility: action needed in {d.get('environment') or norm.get('environment_tier')}"[:100],
        MessageStructure="json",
        Message=json.dumps({"default": text, "email": text,
                            "lambda": json.dumps(payload, default=str)}),
        MessageAttributes={"tenant": {"DataType": "String", "StringValue": tenant}})
    return {"sent": True, "tenant": tenant}


def enqueue(url, norm, routing, why, destination):
    if not url:
        return {"sent": False, "reason": f"{destination} queue not configured"}
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({
        "destination": destination,
        "routing": routing,
        "why": why,
        "recorded_at": _now().isoformat(),
        "event": norm,
    }, default=str))
    return {"sent": True}


HEARTBEAT_SOURCE = "platform.heartbeat"
HEARTBEAT_DETAIL_TYPE = "Platform Router Heartbeat"
HEARTBEAT_NAMESPACE = os.environ.get("HEARTBEAT_NAMESPACE", "Platform/Router")
cloudwatch = boto3.client("cloudwatch")


def heartbeat(event):
    """Record that a synthetic heartbeat made it all the way here. D-028.

    A member account emits one every 15 minutes; it crosses that account's
    forwarding rule, the organization bus and the route rule to arrive here. An
    alarm in this account fires on the ABSENCE of this metric, which is the
    only way to notice events no longer reaching the router: every other
    router alarm treats silence as health.

    Routed nowhere -- no queue, no page. If the metric cannot be written this
    raises, so the router's own error alarm fires rather than the heartbeat
    quietly failing.
    """
    cloudwatch.put_metric_data(Namespace=HEARTBEAT_NAMESPACE, MetricData=[{
        "MetricName": "RouterHeartbeat", "Value": 1, "Unit": "Count"}])
    out = {"routed": "heartbeat", "source_account": event.get("account")}
    print(json.dumps(out))
    return out


def handler(event, context):
    if (event.get("source") == HEARTBEAT_SOURCE
            and event.get("detail-type") == HEARTBEAT_DETAIL_TYPE):
        return heartbeat(event)

    detail_type = event.get("detail-type") or ""
    normalise = NORMALISERS.get(detail_type)

    if normalise is None:
        # Before giving up: is this a raw AWS event belonging to one of 06a's
        # event alarms? Those arrive with the service's own detail-type — "AWS
        # API Call via CloudTrail", "RDS DB Instance Event" — and carry no
        # routing decision, because the rule that forwarded them could not
        # attach one.
        spec = match_event_spec(event)
        if spec is not None:
            norm = from_platform_event(event, spec)
            return deliver(norm, detail_type)

    if normalise is None:
        # The else. An event shape nobody anticipated is not dropped — it goes
        # to review with the raw event attached, because a router that silently
        # discards what it does not recognise is indistinguishable from one that
        # is working.
        enqueue(REVIEW_QUEUE_URL,
                {"kind": "unknown", "source": event.get("source"),
                 "title": detail_type or "(no detail-type)",
                 "account_id": event.get("account"), "raw": event},
                "review_queue",
                f"no normaliser for detail-type '{detail_type}'", "review")
        out = {"routed": "review_queue", "reason": "unrecognised event shape",
               "detail_type": detail_type}
        print(json.dumps(out))
        return out

    norm = normalise(event)

    # An alarm returning to OK is only interesting as a PagerDuty resolve. It is
    # not a ticket and not a review item; filing one per recovery would bury the
    # queues in work nobody asked for.
    if norm["kind"] == "alarm" and norm.get("state") not in ("ALARM", "OK"):
        out = {"routed": "ignored", "reason": f"alarm state {norm.get('state')}"}
        print(json.dumps(out))
        return out

    return deliver(norm, detail_type)


def deliver(norm, detail_type):
    """Decide, deliver, and record. One implementation for every event kind.

    Separate from handler() so the raw-AWS-event path and the normalised paths
    cannot diverge: the thing that decides where something goes, and the thing
    that records why, are the same code regardless of what arrived.
    """
    routing, why = decide(norm)
    spec = DESTINATIONS.get(routing)
    if spec is None:
        # Unrecognised routing class. Paged, deliberately: this means 06a named
        # a class this router does not implement, and the alarm it belongs to is
        # currently going nowhere. Under-routing is the failure that hides.
        spec = DESTINATIONS["pagerduty"]
        why = (f"routing class '{routing}' is not implemented by the router, so "
               "the event was escalated rather than dropped. Fix the router or "
               "06a — until then this alarm has no correct destination.")
        routing = "pagerduty"

    recovery = norm["kind"] == "alarm" and norm.get("state") == "OK"

    delivered = {}
    if spec["pages"]:
        delivered["pagerduty"] = page(norm, routing, why)
    elif recovery:
        # A recovery on a non-paging alarm has nowhere useful to go.
        out = {"routed": "ignored", "reason": "recovery on a non-paging alarm"}
        print(json.dumps(out))
        return out

    if recovery:
        # Resolve the incident and stop. A recovery is not work: filing a
        # ticket for it would mean every alarm that clears on its own produces
        # something for a human to close, and a queue of those is how the real
        # ones get closed unread.
        record = {"record_type": "routing-decision",
                  "recorded_at": _now().isoformat(),
                  "detail_type": detail_type, "source": norm.get("source"),
                  "account_id": norm.get("account_id"),
                  "title": norm.get("title"), "routing": routing,
                  "why": "recovery — incident resolved, no ticket raised",
                  "delivered": delivered}
        print(json.dumps(record, default=str))
        return record

    if spec.get("ticket"):
        delivered["jira"] = enqueue(JIRA_QUEUE_URL, norm, routing, why, "jira")
    if spec.get("commercial_event"):
        delivered["commercial"] = enqueue(
            COMMERCIAL_QUEUE_URL, norm, routing, why, "commercial")
    if not spec["pages"] and not spec.get("ticket"):
        delivered["review"] = enqueue(
            REVIEW_QUEUE_URL, norm, routing, why, "review")
    if norm.get("tenant_alert"):
        delivered["tenant_alert"] = tenant_alert(norm, why)

    # Every decision is recorded, including the ones that deliberately did
    # nothing. "Why was I not paged about this" is the question this log exists
    # to answer, and it is asked after the fact.
    record = {
        "record_type": "routing-decision",
        "recorded_at": _now().isoformat(),
        "detail_type": detail_type,
        "source": norm.get("source"),
        "account_id": norm.get("account_id"),
        "environment_tier": norm.get("environment_tier"),
        "title": norm.get("title"),
        "resource": norm.get("resource"),
        # What the event SAID, as well as where it went. The digest reads these
        # records to build its lines (reporting/src/facts.py), and a title
        # alone -- "HIPAA eligibility: usage — apprunner" -- omits the part the
        # reader acts on, such as what to use instead. Bounded, because a
        # dossier's probable cause can run to paragraphs and this is a log
        # line.
        "reason": (str(norm.get("reason"))[:600] if norm.get("reason") else None),
        "severity": norm.get("severity"),
        "routing": routing,
        "why": why,
        "delivered": delivered,
        # digest is a FLAG on the destination, not a delivery: 4.4 builds the
        # digest by reading records like this one, rather than by being pushed
        # to. Recorded so that reading is possible.
        "digest": spec.get("digest", False),
    }
    print(json.dumps(record, default=str))
    return record
