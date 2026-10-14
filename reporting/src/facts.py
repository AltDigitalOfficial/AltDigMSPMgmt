"""Digest facts — gathered once a day in Audit, read by the digest in Tooling.

docs/digest-design.md §6, ADR-004. B-171 stage A.

------------------------------------------------------------------------------
Why this exists apart from the digest
------------------------------------------------------------------------------
The digest (reporting/src/digest.py) runs in Infra Tooling, where the SES
identity and the registry are. Most of what the manager digest needs is not
there: the router's decisions and the organization Config aggregator are in
Audit, and the org trail is in Log Archive. Rather than give Tooling read access
to all three, this function gathers in Audit and writes one facts document per
account. The digest reads those documents and nothing else of Audit's.

It also means both digests -- the manager digest and the platform digest
(ADR-003) -- render from the same facts, so they cannot disagree about what
happened.

------------------------------------------------------------------------------
Sources, and what each one is for
------------------------------------------------------------------------------
  router     The routing decisions the router flagged `digest`, from its own
             log. HIPAA eligibility lines and telemetry gaps today. The digest
             shows what the ROUTER decided was digest-class and never decides
             that itself (W-010: an emitter may downgrade, never escalate; the
             reader is not an emitter at all).

  layer      Resources of the types 06a alarms on whose `platform-layer` tag is
             missing or outside the vocabulary (B-127, B-128). Those are the
             resources whose alarms route by that tag, so they are the ones
             where a missing tag sends a page to the wrong team. Split by
             `platform-managed`: an untagged platform resource is AltDigital's
             gap and goes to the platform digest, not the client's.

  trail      Not built. Recorded as such in every document, so the digest says
             "not available yet" rather than rendering an empty section that
             reads as a quiet day. Arrives with security/50 (B-171 stage B).

A source that fails is recorded in the document with the reason, and the other
sources are still written. "Could not read" and "nothing to report" must stay
distinguishable all the way to the email.

------------------------------------------------------------------------------
The window
------------------------------------------------------------------------------
The 24 hours ending at WINDOW_END_HOUR UTC (12:00) on the day of the run, not
the 24 hours ending now. CloudTrail delivers to S3 within about fifteen
minutes, so a window ending at run time would read a trail still arriving
(design §10). The router log has no such lag, but every source shares one
window so a document describes one period.

------------------------------------------------------------------------------
Personal data
------------------------------------------------------------------------------
Stage A carries resource identifiers and the router's own text, no people.
Stage B adds developers' session names (design §9). The bucket expires objects
at 30 days: these are inputs, and the evidence is what was sent (ADR-001).
"""

import datetime
import json
import os

import boto3

FACTS_BUCKET = os.environ.get("FACTS_BUCKET", "")
ACCOUNT_TIER_PARAM = os.environ.get("ACCOUNT_TIER_PARAM", "/platform/routing/account-tiers")
ROUTER_LOG_GROUP = os.environ.get("ROUTER_LOG_GROUP", "/aws/lambda/platform-router")
AGGREGATOR = os.environ.get("AGGREGATOR", "platform-organization")
SPEC_PATH = os.environ.get("SPEC_PATH", "alarm-spec.json")
WINDOW_END_HOUR = int(os.environ.get("WINDOW_END_HOUR", "12"))
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "24"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Platform/DigestFacts")

# Digest-class decisions the MANAGER digest renders, by router source. Others
# (Security Hub, GuardDuty, Config rule findings in dev) are counted, not
# listed: design 11's manager digest does not name them, and a dev account's
# Security Hub stream would bury the lines a manager acts on. The counts are
# for the platform digest.
FINDING_SOURCES = ("platform.eligibility", "platform.telemetry")

LAYER_TAG = "platform-layer"
LAYER_VALUES = ("infrastructure", "application")   # instrumentation/src/handler.py
LIST_CAP = 200
SCHEMA_VERSION = 1

ssm = boto3.client("ssm")
logs = boto3.client("logs")
config = boto3.client("config")
s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(ts):
    return ts.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def window(now=None):
    """(start, end) of the most recent completed window."""
    now = now or _now()
    end = now.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    if end > now:
        end -= datetime.timedelta(days=1)
    return end - datetime.timedelta(hours=WINDOW_HOURS), end


def facts_key(end, account_id):
    """One place that names a facts object. digest.py mirrors it."""
    return f"facts/{end.strftime('%Y-%m-%d')}/{account_id}.json"


def in_scope_types():
    """06a's alarmable types, from the spec shipped in this package.

    The same file the instrumentation Lambda reads, converted by the same
    validator (scripts/publish-digest.sh). Not a list in this file: a second
    copy of 06a is the two-copies problem 06a's own header describes.
    """
    with open(SPEC_PATH, encoding="utf-8") as fh:
        return list(json.load(fh).get("resource_types_in_scope") or [])


def accounts():
    """{account_id: tier} from the routing tier map.

    Raises on an empty map rather than gathering for nobody (B-086): a run
    over zero accounts writes no documents, and the digest would then report
    every account's facts as missing for a reason nowhere near the cause.
    """
    raw = ssm.get_parameter(Name=ACCOUNT_TIER_PARAM)["Parameter"]["Value"]
    mapping = {str(k): v for k, v in (json.loads(raw) or {}).items() if str(k).isdigit()}
    if not mapping:
        raise RuntimeError(f"{ACCOUNT_TIER_PARAM} lists no accounts.")
    return mapping


# ---------------------------------------------------------------------------
# Router decisions
# ---------------------------------------------------------------------------

ROUTER_FILTER = '{ ($.record_type = "routing-decision") && ($.digest IS TRUE) }'


def read_router(start, end):
    """Digest-flagged routing decisions in the window, as raw records."""
    out = []
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=ROUTER_LOG_GROUP,
        startTime=int(start.timestamp() * 1000),
        endTime=int(end.timestamp() * 1000) - 1,
        filterPattern=ROUTER_FILTER)
    for page in pages:
        for e in page.get("events") or []:
            try:
                out.append(json.loads(e["message"]))
            except (ValueError, KeyError):
                continue
    return out


def group_findings(records):
    """{account_id: {"findings": [...], "other_sources": {source: n}}}.

    Repeats collapse to one line with a count. The telemetry scan runs every
    fifteen minutes and re-emits a standing gap each time, so one stopped agent
    is up to 96 records a day and must be one line in an email.
    """
    by_account = {}
    for r in records:
        acct = str(r.get("account_id") or "")
        if not acct:
            continue
        slot = by_account.setdefault(acct, {"findings": {}, "other_sources": {}})
        source = r.get("source") or "unknown"
        if source not in FINDING_SOURCES:
            slot["other_sources"][source] = slot["other_sources"].get(source, 0) + 1
            continue
        key = (source, r.get("title"), r.get("resource"))
        seen = r.get("recorded_at")
        f = slot["findings"].get(key)
        if f is None:
            f = slot["findings"][key] = {
                "source": source, "title": r.get("title"), "resource": r.get("resource"),
                "count": 0, "first_seen": seen, "last_seen": seen}
        f["count"] += 1
        if seen and (not f["first_seen"] or seen < f["first_seen"]):
            f["first_seen"] = seen
        if seen and (not f["last_seen"] or seen >= f["last_seen"]):
            f["last_seen"] = seen
            # The latest record speaks for the line: a gap that started as a
            # digest and became a ticket should read as a ticket.
            f.update({"routing": r.get("routing"), "why": r.get("why"),
                      "reason": r.get("reason"), "severity": r.get("severity"),
                      "tier": r.get("environment_tier")})
    return {a: {"findings": sorted(s["findings"].values(),
                                   key=lambda f: (f["source"], f["title"] or "")),
                "other_sources": s["other_sources"]}
            for a, s in by_account.items()}


# ---------------------------------------------------------------------------
# Layer tag
# ---------------------------------------------------------------------------


def read_layer_tags(types):
    """Every current resource of the alarmable types, with its tags."""
    quoted = ", ".join(f"'{t}'" for t in types)
    expr = ("SELECT accountId, awsRegion, resourceType, resourceId, resourceName, "
            "resourceCreationTime, configurationItemStatus, tags "
            f"WHERE resourceType IN ({quoted})")
    token = None
    while True:
        kw = {"ConfigurationAggregatorName": AGGREGATOR, "Expression": expr, "Limit": 100}
        if token:
            kw["NextToken"] = token
        resp = config.select_aggregate_resource_config(**kw)
        for item in resp.get("Results") or []:
            yield json.loads(item)
        token = resp.get("NextToken")
        if not token:
            return


def _tags(row):
    out = {}
    for t in row.get("tags") or []:
        if isinstance(t, dict) and "key" in t:
            out[t["key"]] = t.get("value")
    return out


def classify_layer(rows):
    """{account_id: {"tenant": [...], "platform": [...], "examined": n}}."""
    out = {}
    for row in rows:
        if row.get("configurationItemStatus") in ("ResourceDeleted",
                                                  "ResourceDeletedNotRecorded"):
            continue
        acct = str(row.get("accountId") or "")
        slot = out.setdefault(acct, {"tenant": [], "platform": [], "examined": 0})
        slot["examined"] += 1
        tags = _tags(row)
        raw = str(tags.get(LAYER_TAG) or "").strip().lower()
        if raw in LAYER_VALUES:
            continue
        entry = {"resource_type": row.get("resourceType"),
                 "resource_id": row.get("resourceId"),
                 "name": row.get("resourceName"),
                 "region": row.get("awsRegion"),
                 "created": row.get("resourceCreationTime"),
                 "problem": "invalid" if raw else "missing",
                 "value": tags.get(LAYER_TAG) if raw else None}
        owner = "platform" if str(tags.get("platform-managed")).lower() == "true" else "tenant"
        slot[owner].append(entry)
    for slot in out.values():
        for owner in ("tenant", "platform"):
            slot[owner].sort(key=lambda e: (e["resource_type"] or "", e["resource_id"] or ""))
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _put(key, doc):
    s3.put_object(Bucket=FACTS_BUCKET, Key=key,
                  Body=json.dumps(doc, indent=2, default=str).encode("utf-8"),
                  ContentType="application/json")


def _capped(items):
    return {"total": len(items), "items": items[:LIST_CAP]}


def gather(now=None):
    start, end = window(now)
    generated = _now()
    tiers = accounts()
    status = {}

    try:
        routed = group_findings(read_router(start, end))
        status["router"] = "ok"
    except Exception as exc:                          # noqa: BLE001
        routed, status["router"] = {}, f"error: {type(exc).__name__}: {exc}"

    try:
        layer = classify_layer(read_layer_tags(in_scope_types()))
        status["layer"] = "ok"
    except Exception as exc:                          # noqa: BLE001
        layer, status["layer"] = {}, f"error: {type(exc).__name__}: {exc}"

    status["trail"] = "not-built: needs security/50 (B-171 stage B)"

    written, failed = [], []
    for acct, tier in sorted(tiers.items()):
        r = routed.get(acct) or {"findings": [], "other_sources": {}}
        lay = layer.get(acct) or {"tenant": [], "platform": [], "examined": 0}
        doc = {
            "schema": SCHEMA_VERSION,
            "account_id": acct,
            "tier": tier,
            "window": {"start": _iso(start), "end": _iso(end)},
            "generated_at": _iso(generated),
            "sources": status,
            "routed_findings": r["findings"],
            "other_digest_decisions": r["other_sources"],
            "layer_tag": {"examined": lay["examined"],
                          "tenant": _capped(lay["tenant"]),
                          "platform": _capped(lay["platform"])},
        }
        try:
            _put(facts_key(end, acct), doc)
            written.append(acct)
        except Exception as exc:                      # noqa: BLE001
            failed.append({"account": acct, "error": f"{type(exc).__name__}: {exc}"})

    # Accounts the router or aggregator mentioned that the tier map does not.
    # Not written per account -- nothing would read them -- but named, because
    # an account missing from the tier map is also routed as prod (B-073).
    unmapped = sorted((set(routed) | set(layer)) - set(tiers))

    estate = {
        "schema": SCHEMA_VERSION,
        "window": {"start": _iso(start), "end": _iso(end)},
        "generated_at": _iso(generated),
        "sources": status,
        "accounts_in_tier_map": sorted(tiers),
        "accounts_written": written,
        "accounts_failed": failed,
        "accounts_not_in_tier_map": unmapped,
        "layer_tag_counts": {a: {"tenant": len(v["tenant"]), "platform": len(v["platform"]),
                                 "examined": v["examined"]}
                             for a, v in sorted(layer.items()) if a in tiers},
        "other_digest_decisions": {a: v["other_sources"] for a, v in sorted(routed.items())
                                   if v["other_sources"]},
    }
    _put(f"facts/{end.strftime('%Y-%m-%d')}/estate.json", estate)

    clean = (status["router"] == "ok" and status["layer"] == "ok" and not failed)
    return {"window_end": _iso(end), "written": len(written), "failed": failed,
            "sources": status, "unmapped": unmapped, "clean": clean}


def handler(event, context):
    result = gather()
    print(json.dumps({"record_type": "digest-facts-run", **result}, default=str))
    # Only a clean run counts. platform-digest-facts-incomplete alarms on the
    # metric's absence, so a run that could not read a source, or a function
    # that stopped running, both reach a person (alerting/60 forwards it).
    if result["clean"]:
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[
            {"MetricName": "FactsCompleted", "Value": 1, "Unit": "Count"}])
    return result
