"""EventBridge patterns for 06a's `metric: event` alarms.

Pure: no boto3, no environment, no I/O. That matters because there are two
consumers in two places and they must agree exactly.

    instrumentation/src/drift.py     creates the rules in each member account
    scripts/lib/extract_event_specs.py
                                     bakes the same patterns into the router
                                     package, so the router can recognise a raw
                                     AWS event and find the 06a alarm it belongs
                                     to

The router needs that because an EventBridge rule cannot transform its input
when the target is an event bus — verified against the API, which answers
"Modifying the input for target central is not supported". So the rule forwards
the raw AWS event and nothing can be attached to it on the way. The router has
to identify the event by its shape, which means matching the same patterns the
rules were built from.

Two copies of these patterns would be two chances to disagree, and the
disagreement would be invisible: the rule would forward an event the router did
not recognise, the router would send it to the review queue, and the alarm 06a
defines would appear to be working while going nowhere.
"""

AUTOSCALING_DETAIL_TYPES = {
    "EC2_INSTANCE_LAUNCH_ERROR": "EC2 Instance Launch Unsuccessful",
    "EC2_INSTANCE_TERMINATE_ERROR": "EC2 Instance Terminate Unsuccessful",
}

CLOUDTRAIL_SOURCES = {
    "AWS::S3::Bucket": "aws.s3",
}


def event_pattern(spec, resource_type):
    """Build an EventBridge pattern. Returns (pattern, None) or (None, reason)."""
    source = spec.get("source")

    if source == "rds_event_category":
        cats = spec.get("event_categories") or []
        return {
            "source": ["aws.rds"],
            "detail-type": ["RDS DB Instance Event", "RDS DB Cluster Event"],
            "detail": {"EventCategories": cats},
        }, None

    if source == "cloudtrail":
        aws_source = CLOUDTRAIL_SOURCES.get(resource_type)
        if not aws_source:
            return None, (
                f"cloudtrail event alarm on {resource_type}, which has no entry in "
                "CLOUDTRAIL_SOURCES. Add the aws.* event source for that service."
            )
        return {
            "source": [aws_source],
            "detail-type": ["AWS API Call via CloudTrail"],
            "detail": {"eventName": spec.get("event_names") or []},
        }, None

    if source == "autoscaling_event":
        types = spec.get("event_types") or []
        unknown = [t for t in types if t not in AUTOSCALING_DETAIL_TYPES]
        if unknown:
            return None, f"unmapped Auto Scaling event types: {unknown}"
        return {
            "source": ["aws.autoscaling"],
            "detail-type": [AUTOSCALING_DETAIL_TYPES[t] for t in types],
        }, None

    return None, f"unknown event source '{source}'"


def matches(pattern, event):
    """Does this event satisfy this EventBridge pattern?

    A deliberately small subset of EventBridge's matching language — exact
    values in a list, nested one level under `detail` — because that is all
    event_pattern() above produces. A pattern using anything richer would match
    in EventBridge and not here, so anything unrecognised returns False rather
    than guessing, and the caller treats no-match as a reason to escalate to
    review rather than to drop.
    """
    for key, expected in pattern.items():
        if key == "detail":
            detail = event.get("detail") or {}
            for dkey, dexpected in expected.items():
                actual = detail.get(dkey)
                if isinstance(actual, list):
                    # RDS EventCategories is a list on the event and a list in
                    # the pattern; EventBridge treats that as "any overlap".
                    if not set(actual) & set(dexpected):
                        return False
                elif actual not in dexpected:
                    return False
            continue
        if event.get(key) not in expected:
            return False
    return True
