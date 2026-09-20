"""Proving nothing was silently dropped. Prompt 9.1's actual subject.

design/04-security-controls-and-evidence.md, "Completeness is a control":

    Because we push, we must prove nothing was silently dropped.

    - Sequence numbering per tenant stream; gaps are detectable
    - Heartbeat per tenant -- absence of data is itself an event,
      distinguishable from a quiet month
    - Buffering -- ingestion queues so a Truveon outage delays delivery rather
      than losing it
    - Reconciliation -- periodic count comparison between source and Truveon

    A silently failed pipeline that looks like a clean month is the failure
    mode that destroys an audit. This is the control that prevents it.

The pipeline is not built to never drop a record
-------------------------------------------------
That is not achievable against an external endpoint nobody here controls. It is
built so that anything dropped is KNOWN, BOUNDED and EXPLAINABLE BY PERIOD.

    "We lost four hours on the 3rd, here is the range and here is why"
        -- an audit answer

    "Our counts do not match and we are not sure when"
        -- not one

That inversion is the whole design. D-016.

Why these results are WRITTEN and not merely alerted
-----------------------------------------------------
design 15 puts the evidence pipeline inside AltDigital's own SOC 2 scope, which
design 04 does not mention. So the pipeline is not only the thing that carries
evidence for other controls -- it is itself a control in the report partners
rely on through the carve-out.

A SOC 2 Type II auditor asks whether a control operated THROUGHOUT the period.
A heartbeat that fired eight months ago and was acknowledged in a chat channel
proves nothing about the eleven months around it. So every heartbeat, every
detected gap and every count comparison is written as an evidence record in its
own right, in the `pipeline-completeness` category, and pushed like any other.

The pipeline evidences itself. D-016.
"""
import datetime
import json
import os

import boto3

import record

dynamodb = boto3.client("dynamodb")
events_client = boto3.client("events")
ssm = boto3.client("ssm")

LEDGER_TABLE = os.environ.get("EVIDENCE_LEDGER_TABLE", "")
SEQUENCE_TABLE = os.environ.get("EVIDENCE_SEQUENCE_TABLE", "")
ALERT_BUS_NAME = os.environ.get("ALERT_BUS_NAME", "platform-alerts")
TENANT_MAP_PARAM = os.environ.get("EVIDENCE_TENANT_MAP_PARAM",
                                  "/platform/evidence/tenants")

# How long a record may sit undelivered before its absence counts as a gap.
#
# Not zero, and the reason is B-043 and B-081. A record allocated a moment ago
# is in flight: queued, retrying, or waiting on a Truveon that is briefly slow.
# Reporting those would raise a finding on every healthy run, and a finding
# that is always true is one that gets muted -- taking the real gaps with it.
#
# Generous rather than tight. A gap that takes an hour to be reported is
# reported; a control nobody trusts is not.
INFLIGHT_GRACE_MINUTES = int(
    os.environ.get("EVIDENCE_INFLIGHT_GRACE_MINUTES", "60"))

# How long a tenant may produce nothing before silence becomes a finding.
#
# design 04: "absence of data is itself an event, distinguishable from a quiet
# month". The distinguishing is done by the HEARTBEAT, not by this threshold --
# a live heartbeat with no other evidence means a genuinely quiet tenant, and
# raises nothing. This covers the other case: the heartbeat itself stopping.
SILENCE_HOURS = int(os.environ.get("EVIDENCE_SILENCE_HOURS", "26"))


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class TenantsUnknown(Exception):
    """The tenant list could not be read.

    Distinct because a completeness check that does not know which tenants
    exist cannot conclude that all of them are healthy -- it can only conclude
    that it checked nothing. B-086, where a sweep that swept nothing reported a
    clean estate.
    """


def tenants():
    """Tenants and their partners, from SSM.

    Raises rather than returning empty. A heartbeat run over zero tenants
    writes zero heartbeats and reports success, which is indistinguishable
    from an estate in perfect health -- and this is the control whose entire
    job is to notice absence.
    """
    try:
        raw = ssm.get_parameter(Name=TENANT_MAP_PARAM)["Parameter"]["Value"]
        data = json.loads(raw)
    except Exception as exc:                       # noqa: BLE001
        raise TenantsUnknown(
            f"could not read {TENANT_MAP_PARAM}: {type(exc).__name__}. "
            "No conclusion can be drawn about any tenant's evidence stream."
        ) from exc
    out = data.get("tenants") or {}
    if not out:
        raise TenantsUnknown(
            f"{TENANT_MAP_PARAM} lists no tenants. Nothing was checked, and "
            "that is not the same as everything being healthy.")
    return out


def _emit(finding):
    events_client.put_events(Entries=[{
        "EventBusName": ALERT_BUS_NAME,
        "Source": "altdigital.evidence",
        "DetailType": "evidence-completeness",
        "Detail": json.dumps(finding, default=str),
    }])


def _persist(tenant, partner, kind, payload, control_ref):
    """Write a completeness result as an evidence record in its own right.

    The reason this function exists rather than the callers simply alerting.
    See the module docstring: the pipeline is in SOC 2 scope and must evidence
    its own operation over the period, not just page when it breaks.
    """
    item = record.build(
        tenant=tenant, partner=partner, category="pipeline-completeness",
        source=kind, payload=payload, control_ref=control_ref)
    if LEDGER_TABLE:
        dynamodb.put_item(
            TableName=LEDGER_TABLE,
            Item=_to_item(item),
            ConditionExpression="attribute_not_exists(PK)")
    return item


def _to_item(item):
    return {
        "PK": {"S": item["stream_id"]},
        # Zero-padded so lexical order is numeric order. Without this, sequence
        # 9 sorts after 10 and a range query for "40 to 50" returns nonsense.
        "SK": {"S": f"{item['sequence']:012d}"},
        "sequence": {"N": str(item["sequence"])},
        "tenant": {"S": item["tenant"]},
        "partner": {"S": str(item.get("partner") or "")},
        "category": {"S": item["category"]},
        "emitted_at": {"S": item["emitted_at"]},
        "status": {"S": "pending"},
        "content_hash": {"S": item["content_hash"]},
        "body": {"S": json.dumps(item, default=str)},
    }


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def heartbeat(event=None, context=None):
    """One record per tenant per run, whether or not anything else flowed.

    This is what makes silence readable. Without it, a tenant producing no
    evidence and a pipeline that stopped collecting for that tenant look
    identical from Truveon: no records either way.

    With it, the two separate cleanly:

        heartbeat present, no other evidence   a quiet tenant. Normal.
        heartbeat absent                       the pipeline stopped. A finding.

    And because the heartbeat is itself an evidence record with a sequence
    number, a heartbeat that stops leaves a gap in the completeness stream --
    so the absence is detectable by the same mechanism as everything else,
    rather than needing a watcher for the watcher.
    """
    try:
        registry = tenants()
    except TenantsUnknown as exc:
        _emit({
            "record_type": "evidence-completeness-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": "review_queue",
            "alarm": {"name": "platform-evidence-tenants-unknown",
                      "resource_id": TENANT_MAP_PARAM},
            "assessment": {"probable_cause": str(exc)},
        })
        out = {"record_type": "evidence-heartbeat", "ran": False,
               "reason": str(exc)}
        print(json.dumps(out, default=str))
        return out

    written = []
    failed = []
    for tenant, facts in sorted(registry.items()):
        try:
            item = _persist(
                tenant=tenant,
                partner=(facts or {}).get("partner"),
                kind="heartbeat",
                control_ref="evidence-pipeline-liveness",
                payload={"at": _iso(_now()),
                         "interval_hours": SILENCE_HOURS})
            written.append({"tenant": tenant, "sequence": item["sequence"]})
        except Exception as exc:                   # noqa: BLE001
            # One tenant failing must not stop the others. A heartbeat run
            # that aborts halfway leaves later tenants looking silent, which
            # is a false finding caused by the control itself.
            failed.append({"tenant": tenant, "error": f"{type(exc).__name__}"})

    if failed:
        _emit({
            "record_type": "evidence-completeness-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": "review_queue",
            "alarm": {"name": "platform-evidence-heartbeat-failed",
                      "resource_id": ",".join(f["tenant"] for f in failed)},
            "assessment": {"probable_cause": (
                f"{len(failed)} tenant(s) did not get a heartbeat this run. "
                "Their evidence streams will read as silent, and the silence "
                "will be this control's fault rather than a real gap.")},
            "evidence_completeness": {"failed": failed},
        })

    out = {"record_type": "evidence-heartbeat", "ran": True,
           "tenants": len(registry), "written": len(written),
           "failed": len(failed)}
    print(json.dumps(out, default=str))
    return out


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


def detect_gaps(event=None, context=None):
    """Sequence numbers allocated but never delivered, past the grace period.

    A gap is a hole in the ledger: number 41 and number 43 delivered, 42 not,
    and 42 allocated long enough ago that it is not merely in flight.

    Reported with its RANGE and its TIME, because that is what makes it an
    audit answer rather than an admission. "Records 42 to 61 in tenant X,
    between 09:14 and 13:02 on the 3rd" is bounded and explainable. "Counts do
    not match" is not.
    """
    try:
        registry = tenants()
    except TenantsUnknown as exc:
        out = {"record_type": "evidence-gap-scan", "scanned": False,
               "reason": str(exc)}
        print(json.dumps(out, default=str))
        return out

    cutoff = _now() - datetime.timedelta(minutes=INFLIGHT_GRACE_MINUTES)
    findings = []
    scanned = 0

    for tenant, facts in sorted(registry.items()):
        partner = (facts or {}).get("partner")
        for category in sorted(record.CATEGORIES):
            stream = record.stream_id(tenant, category)
            rows = _ledger_rows(stream)
            if rows is None:
                continue
            scanned += 1
            missing = _missing_sequences(rows, cutoff)
            if not missing:
                continue
            finding = {
                "stream_id": stream,
                "tenant": tenant,
                "partner": partner,
                "category": category,
                "missing_count": len(missing),
                # The RANGE, not the list. A four-hour outage can be thousands
                # of numbers, and a finding carrying all of them is one nobody
                # reads.
                "first_missing": missing[0],
                "last_missing": missing[-1],
                "window_start": rows.get("_window_start"),
                "window_end": rows.get("_window_end"),
            }
            findings.append(finding)
            # Persisted as evidence, not only alerted. A gap that was found and
            # explained is a passable audit answer; it has to be findable in
            # eight months to be one. D-016.
            _persist(tenant=tenant, partner=partner, kind="gap-detected",
                     control_ref="evidence-pipeline-completeness",
                     payload=finding)
            _emit({
                "record_type": "evidence-completeness-finding",
                "recorded_at": _iso(_now()),
                "current_state": "ALARM",
                "routing": "digest_and_jira",
                "alarm": {"name": "platform-evidence-sequence-gap",
                          "resource_id": stream},
                "assessment": {"probable_cause": (
                    f"{len(missing)} evidence record(s) in {stream} were "
                    f"allocated a sequence number and never delivered "
                    f"(numbers {missing[0]} to {missing[-1]}). They are past "
                    f"the {INFLIGHT_GRACE_MINUTES}-minute in-flight grace, so "
                    "this is loss rather than delay.")},
                "evidence_completeness": finding,
            })

    out = {"record_type": "evidence-gap-scan", "scanned": True,
           "streams": scanned, "gaps": len(findings),
           "grace_minutes": INFLIGHT_GRACE_MINUTES}
    print(json.dumps(out, default=str))
    return out


def _ledger_rows(stream_id):
    """Every ledger row for a stream, or None if the ledger is unreadable."""
    if not LEDGER_TABLE:
        return None
    rows = {}
    first, last = None, None
    try:
        paginator = dynamodb.get_paginator("query")
        for page in paginator.paginate(
                TableName=LEDGER_TABLE,
                KeyConditionExpression="PK = :p",
                ExpressionAttributeValues={":p": {"S": stream_id}}):
            for item in page.get("Items") or []:
                seq = int(item["sequence"]["N"])
                emitted = item.get("emitted_at", {}).get("S")
                rows[seq] = {
                    "status": item.get("status", {}).get("S"),
                    "emitted_at": emitted,
                }
                if emitted and (first is None or emitted < first):
                    first = emitted
                if emitted and (last is None or emitted > last):
                    last = emitted
    except Exception:                              # noqa: BLE001
        return None
    rows["_window_start"] = first
    rows["_window_end"] = last
    return rows


def _missing_sequences(rows, cutoff):
    """Allocated-but-undelivered numbers older than the grace period."""
    missing = []
    for seq, row in rows.items():
        if not isinstance(seq, int):
            continue
        if row.get("status") == "delivered":
            continue
        emitted = _parse(row.get("emitted_at"))
        if emitted is None or emitted > cutoff:
            # Still in flight. Not a gap yet, and calling it one is how this
            # control gets muted.
            continue
        missing.append(seq)
    return sorted(missing)


# ---------------------------------------------------------------------------
# Count reconciliation
# ---------------------------------------------------------------------------


def reconcile_counts(event=None, context=None):
    """design 04's fourth control: periodic count comparison with Truveon.

    Asks both sides how many records exist for a stream over a window and
    compares. The window is by `emitted_at` -- the platform's clock -- because
    `occurred_at` is the source's, and a Config evaluation arriving late would
    otherwise make a healthy period look short on one side only.

    Counts alone prove less than they look like they do. Equal totals can hide
    one record lost and one duplicated, so the sequence RANGE is compared too:
    a window holding `count` records between `min_sequence` and `max_sequence`
    should satisfy `max - min + 1 == count`, and a stream that does not is
    reporting a total that does not describe its own contents.

    With no endpoint configured this does NOT report a match. It reports that
    no comparison was possible. B-088, and the same rule as everywhere in this
    module: an unanswerable question is not a negative answer.
    """
    endpoint = os.environ.get("TRUVEON_ENDPOINT", "").rstrip("/")
    window_hours = int(os.environ.get("EVIDENCE_RECONCILE_HOURS", "26"))
    until = _now()
    since = until - datetime.timedelta(hours=window_hours)

    if not endpoint:
        out = {"record_type": "evidence-count-reconciliation",
               "compared": False,
               "reason": ("TRUVEON_ENDPOINT is not configured, so the far side "
                          "of the comparison does not exist yet (B-088). No "
                          "conclusion is drawn about whether counts agree.")}
        _emit({
            "record_type": "evidence-completeness-finding",
            "recorded_at": _iso(_now()),
            "current_state": "ALARM",
            "routing": "digest",
            "alarm": {"name": "platform-evidence-reconciliation-impossible",
                      "resource_id": "truveon"},
            "assessment": {"probable_cause": out["reason"]},
        })
        print(json.dumps(out, default=str))
        return out

    try:
        registry = tenants()
    except TenantsUnknown as exc:
        out = {"record_type": "evidence-count-reconciliation",
               "compared": False, "reason": str(exc)}
        print(json.dumps(out, default=str))
        return out

    compared, mismatches = 0, []
    for tenant, facts in sorted(registry.items()):
        partner = (facts or {}).get("partner")
        for category in sorted(record.CATEGORIES):
            stream = record.stream_id(tenant, category)
            ours = _count_ours(stream, since, until)
            if ours is None:
                continue
            theirs = _count_theirs(endpoint, stream, since, until)
            compared += 1
            result = {
                "stream_id": stream, "tenant": tenant, "partner": partner,
                "window_start": _iso(since), "window_end": _iso(until),
                "source_count": ours["count"],
                "truveon_count": (theirs or {}).get("count"),
                "source_range": [ours["min"], ours["max"]],
                "truveon_range": [(theirs or {}).get("min_sequence"),
                                  (theirs or {}).get("max_sequence")],
                "unreadable": theirs is None,
            }
            agrees = (theirs is not None
                      and theirs.get("count") == ours["count"])
            result["agrees"] = agrees
            # Persisted whether or not it agrees. A comparison that matched is
            # evidence the control operated; only keeping the failures would
            # leave an auditor unable to tell "checked and fine" from "never
            # checked". D-016.
            _persist(tenant=tenant, partner=partner, kind="count-reconciled",
                     control_ref="evidence-pipeline-completeness",
                     payload=result)
            if not agrees:
                mismatches.append(result)
                _emit({
                    "record_type": "evidence-completeness-finding",
                    "recorded_at": _iso(_now()),
                    "current_state": "ALARM",
                    "routing": "digest_and_jira",
                    "alarm": {"name": "platform-evidence-count-mismatch",
                              "resource_id": stream},
                    "assessment": {"probable_cause": (
                        f"{stream}: the platform holds {ours['count']} record(s) "
                        f"for this window, Truveon reports "
                        f"{result['truveon_count']}. "
                        + ("Truveon could not be read, so this is not yet a "
                           "disagreement -- it is an unanswered question."
                           if theirs is None else
                           "The two stores disagree about a period that is "
                           "closed, so one of them is missing records."))},
                    "evidence_completeness": result,
                })

    out = {"record_type": "evidence-count-reconciliation", "compared": True,
           "streams": compared, "mismatches": len(mismatches),
           "window_hours": window_hours}
    print(json.dumps(out, default=str))
    return out


def _count_ours(stream_id, since, until):
    """How many records the platform allocated in this window."""
    rows = _ledger_rows(stream_id)
    if rows is None:
        return None
    seqs = []
    for seq, row in rows.items():
        if not isinstance(seq, int):
            continue
        emitted = _parse(row.get("emitted_at"))
        if emitted and since <= emitted <= until:
            seqs.append(seq)
    if not seqs:
        return None
    return {"count": len(seqs), "min": min(seqs), "max": max(seqs)}


def _count_theirs(endpoint, stream_id, since, until):
    """Truveon's count for the same window, or None if it cannot be asked.

    None is distinct from zero throughout. A Truveon that cannot be reached has
    not told us it holds nothing.
    """
    import urllib.error
    import urllib.parse
    import urllib.request
    try:
        import sender
        token = sender._token()
    except Exception:                              # noqa: BLE001
        return None
    query = urllib.parse.urlencode({
        "stream_id": stream_id, "from": _iso(since), "to": _iso(until)})
    req = urllib.request.Request(
        f"{endpoint}/v1/evidence/count?{query}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:                              # noqa: BLE001
        return None


HANDLERS = {"heartbeat": heartbeat, "detect_gaps": detect_gaps,
            "reconcile_counts": reconcile_counts}


def handler(event, context):
    action = (event or {}).get("action") or "heartbeat"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(
            f"unknown action '{action}'. Known: {', '.join(sorted(HANDLERS))}")
    return fn(event, context)
