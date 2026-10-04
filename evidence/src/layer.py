"""Which layer a piece of evidence belongs to. D-029, B-157 phase C.

    infrastructure  AltDigital's: platform-built resources, the account itself,
                    and what AltDigital does in the account. Goes to
                    AltDigital's Truveon tenant.
    application     the client's: their resources and their application. Goes
                    to the client's declared destination.
    None            not known. Held and reported, never guessed (D-029 item 3).

The answer travels with its SOURCE, because "the resource said so" and "we
could not tell" are different facts. The alarm path learned this first
(instrumentation's layer-source, B-127): a reader that cannot tell a real
attribution from a fallback makes the fallback permanent by making it
invisible.

STAGE ONE: this is recorded on the record and nothing routes on it yet. A day
of real records shows how often each source resolves before any of it decides
where evidence goes.

Sources
-------
    tag             the resource's own platform-layer tag
    tag-invalid     a platform-layer tag outside the vocabulary (layer None)
    untagged        the resource was found and has no platform-layer tag
    account         the finding is about the account itself, which is the
                    baseline's and therefore AltDigital's
    operator        a platform.* event: AltDigital acting in the account
    no-resource     the event names no resource to look up
    lookup-failed   the aggregator could not answer (layer None)
    not-found       the aggregator has no such resource yet (layer None)
"""
import json
import os

import boto3

LAYERS = ("infrastructure", "application")
TAG_KEY = "platform-layer"

AGGREGATOR = os.environ.get("EVIDENCE_CONFIG_AGGREGATOR", "platform-organization")

_config = None


def _config_client():
    # Created on first use: a collector with no Config evidence never needs it,
    # and the module stays importable in tests with no AWS configuration.
    global _config
    if _config is None:
        _config = boto3.client("config")
    return _config


def from_tags(tags):
    """(layer, source) from a tag mapping, a list of {key,value}, or None."""
    if isinstance(tags, list):
        tags = {(t.get("key") or t.get("Key")): (t.get("value") or t.get("Value"))
                for t in tags if isinstance(t, dict)}
    tags = tags or {}
    raw = tags.get(TAG_KEY)
    if raw is None:
        return None, "untagged"
    value = str(raw).strip().lower()
    if value in LAYERS:
        return value, "tag"
    return None, "tag-invalid"


def security_hub(detail):
    finding = ((detail or {}).get("findings") or [{}])[0]
    resource = (finding.get("Resources") or [{}])[0]
    if resource.get("Type") == "AwsAccount":
        return "infrastructure", "account"
    if not resource:
        return None, "no-resource"
    return from_tags(resource.get("Tags"))


def guardduty(detail):
    res = (detail or {}).get("resource") or {}
    tags = (res.get("instanceDetails") or {}).get("tags")
    if tags is None:
        # GuardDuty carries tags for EC2 findings only. Anything else names no
        # taggable resource here, and is not guessed at.
        return None, "no-resource"
    return from_tags(tags)


def config(detail, account_id, region):
    """Config compliance events carry no tags: ask the organization aggregator."""
    d = detail or {}
    resource_id = d.get("resourceId")
    resource_type = d.get("resourceType")
    if not resource_id:
        return None, "no-resource"
    if resource_type == "AWS::::Account":
        return "infrastructure", "account"
    query = ("SELECT tags WHERE resourceId = '{}' AND accountId = '{}' "
             "AND awsRegion = '{}'").format(
                 resource_id.replace("'", ""), str(account_id).replace("'", ""),
                 str(region).replace("'", ""))
    try:
        rows = _config_client().select_aggregate_resource_config(
            Expression=query, ConfigurationAggregatorName=AGGREGATOR,
            Limit=1)["Results"]
    except Exception:                              # noqa: BLE001
        # Not fatal, and not guessed: the record is still numbered and kept.
        return None, "lookup-failed"
    if not rows:
        return None, "not-found"
    return from_tags(json.loads(rows[0]).get("tags"))


def of(event, category):
    """(layer, source) for an alert-bus event the collector has classified."""
    source = event.get("source") or ""
    detail = event.get("detail") or {}
    if source.startswith("platform."):
        return "infrastructure", "operator"
    if source == "aws.securityhub":
        return security_hub(detail)
    if source == "aws.guardduty":
        return guardduty(detail)
    if source == "aws.config":
        return config(detail, event.get("account") or detail.get("awsAccountId"),
                      event.get("region") or detail.get("awsRegion"))
    return None, "no-resource"
