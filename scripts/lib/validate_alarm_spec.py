#!/usr/bin/env python3
"""Validate design/06a-alarm-specification.yaml and emit it as JSON.

    validate_alarm_spec.py <spec.yaml> <out.json>

Run by scripts/publish-instrumentation.sh before packaging. A malformed
specification would otherwise surface as every instrumentation invocation
erroring at cold start, in every member account, with a stack trace instead of
a reason.

The structural checks matter more than the field checks. An in-scope resource
type with no resolvable alarm set means resources of that type come up with no
alarms AND no exception record — the handler would look them up, find nothing,
and raise "no-alarm-set", which is at least visible. Worse is a type in neither
list: that produces an exception per resource forever. Both are caught here,
before anything is deployed.
"""
import json
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python -m pip install --user pyyaml")

VALID_TMD = {"missing", "ignore", "breaching", "notBreaching"}
REQUIRED_TOP = ("tiers", "routing_destinations", "defaults",
                "resource_types_in_scope", "resource_types_no_alarms", "alarm_sets")


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    for key in REQUIRED_TOP:
        if key not in cfg:
            sys.exit(f"06a is missing required key: {key}")

    problems = []
    tier_names = set(cfg["tiers"])
    valid_routing = set(cfg["routing_destinations"])
    sets = cfg["alarm_sets"]

    def resolve(rt, seen=None):
        """Flatten inherits. Returns None when the type has no entry at all."""
        seen = seen or set()
        if rt in seen:
            problems.append(f"inherits cycle at {rt}")
            return []
        seen.add(rt)
        c = sets.get(rt)
        if c is None:
            return None
        out = []
        if c.get("inherits"):
            parent = resolve(c["inherits"], seen)
            if parent is None:
                problems.append(f"{rt} inherits {c['inherits']}, which has no alarm set")
            else:
                out.extend(parent)
        out.extend(c.get("alarms") or [])
        out.extend(c.get("additional_alarms") or [])
        return out

    total = 0
    for rt in cfg["resource_types_in_scope"]:
        alarms = resolve(rt)
        if alarms is None:
            problems.append(f"{rt} is in resource_types_in_scope but alarm_sets has no entry")
            continue
        if not alarms:
            problems.append(f"{rt} is in scope but resolves to zero alarms")
        total += len(alarms)
        for a in alarms:
            aid = a.get("id", "?")

            # Two shapes, and conflating them was the first thing this
            # validator got wrong.
            #
            #   metric: <name>   a CloudWatch metric alarm. Needs a comparison
            #                    and a threshold per tier.
            #   metric: event    an EventBridge rule. 06a says so in as many
            #                    words: "EventBridge rule, not a metric alarm."
            #                    It has event_categories or event_names and a
            #                    source, and a threshold would be meaningless.
            #
            # Requiring `comparison` of an event entry rejected six perfectly
            # valid alarms in the authoritative spec.
            if a.get("metric") == "event":
                if not a.get("source"):
                    problems.append(f"{rt}/{aid}: event alarm without a 'source'")
                # Three different keys carry the event selector, one per
                # source: event_categories (RDS), event_names (CloudTrail),
                # event_types (Auto Scaling). Accepting only the first two
                # rejected asg-failed-scaling, which is valid.
                if not any(a.get(k) for k in
                           ("event_categories", "event_names", "event_types")):
                    problems.append(
                        f"{rt}/{aid}: event alarm with no selector — needs one of "
                        "event_categories, event_names or event_types")
                if not a.get("severity_by_tier"):
                    problems.append(
                        f"{rt}/{aid}: event alarm without severity_by_tier, so it routes nowhere")
                continue

            for field in ("metric", "comparison", "threshold_by_tier"):
                if field not in a:
                    problems.append(f"{rt}/{aid}: missing '{field}'")
            tmd = a.get("treat_missing_data")
            if tmd and tmd not in VALID_TMD:
                problems.append(f"{rt}/{aid}: treat_missing_data '{tmd}' is not valid")
            for t in (a.get("threshold_by_tier") or {}):
                if t not in tier_names:
                    problems.append(f"{rt}/{aid}: threshold_by_tier names unknown tier '{t}'")
            for t, r in (a.get("severity_by_tier") or {}).items():
                if r not in valid_routing:
                    problems.append(
                        f"{rt}/{aid}: severity_by_tier[{t}] = '{r}' is not a routing destination")

    overlap = set(cfg["resource_types_in_scope"]) & set(cfg["resource_types_no_alarms"])
    if overlap:
        problems.append(f"types in BOTH in_scope and no_alarms: {sorted(overlap)}")

    for tier, tcfg in cfg["tiers"].items():
        dr = tcfg.get("default_routing")
        if dr not in valid_routing:
            problems.append(f"tiers.{tier}.default_routing = '{dr}' is not a routing destination")

    if problems:
        sys.exit("06a is invalid:\n  " + "\n  ".join(problems))

    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)

    # Per-tier counts, printed because they are the quickest sanity check that
    # the tier gradation is doing anything: dev and prod should differ a lot.
    print(f"  {len(cfg['resource_types_in_scope'])} type(s) in scope, "
          f"{len(cfg['resource_types_no_alarms'])} known-no-alarms, "
          f"{total} resolved alarm spec(s)")
    for tier in cfg["tiers"]:
        creatable = nulls = 0
        for rt in cfg["resource_types_in_scope"]:
            for a in (resolve(rt) or []):
                if a.get("metric") == "event":
                    continue
                bt = a.get("threshold_by_tier") or {}
                if tier in bt:
                    if bt[tier] is None:
                        nulls += 1
                    else:
                        creatable += 1
        note = f"  ({nulls} [TUNE] unset -> exceptions)" if nulls else ""
        print(f"    {tier:5s} {creatable:3d} creatable{note}")


if __name__ == "__main__":
    main()
