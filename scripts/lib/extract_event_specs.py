#!/usr/bin/env python3
"""Bake 06a's event-alarm patterns into the router package.

    extract_event_specs.py <06a.yaml> <eventspec.py dir> <out.json>

The router has to recognise a raw AWS event — a CloudTrail API call, an RDS
event, an Auto Scaling failure — and work out which 06a alarm it belongs to, so
it can apply that alarm's routing class.

It cannot be told. An EventBridge rule cannot transform its input when the
target is an event bus; the API answers "Modifying the input for target central
is not supported". So the rule forwards the raw event with nothing attached and
the router identifies it by shape.

The patterns come from instrumentation/src/eventspec.py, the same module the
instrumentation Lambda builds the rules from. Imported rather than
reimplemented: a second copy would be a second chance to disagree, and the
disagreement would be silent — the rule forwards, the router does not recognise
it, the event goes to review, and an alarm 06a defines appears to work while
going nowhere.
"""
import json
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python -m pip install --user pyyaml")


def resolve_set(sets, rt, seen=None):
    """06a's inheritance, the same walk the instrumentation Lambda does."""
    seen = seen or set()
    if rt in seen:
        return []
    seen.add(rt)
    cfg = sets.get(rt)
    if cfg is None:
        return []
    out = []
    if cfg.get("inherits"):
        out.extend(resolve_set(sets, cfg["inherits"], seen))
    out.extend(cfg.get("alarms") or [])
    out.extend(cfg.get("additional_alarms") or [])
    return out


def main():
    spec_path, eventspec_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, eventspec_dir)
    from eventspec import event_pattern

    with open(spec_path, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    sets = spec.get("alarm_sets") or {}

    seen, out, problems = set(), [], []
    for rt in sorted(sets):
        for alarm in resolve_set(sets, rt):
            if alarm.get("metric") != "event":
                continue
            aid = alarm.get("id")
            # Deduplicated by id: RDS::DBCluster inherits RDS::DBInstance, so
            # the two RDS event alarms appear twice across the spec.
            if aid in seen:
                continue
            seen.add(aid)
            pattern, why = event_pattern(alarm, rt)
            if pattern is None:
                # Recorded, not skipped silently. An event alarm the
                # instrumentation Lambda cannot build a rule for is one the
                # router will never see — consistent, and worth knowing.
                problems.append(f"{aid}: {why}")
                continue
            out.append({
                "alarm_id": aid,
                "resource_type": rt,
                "severity_by_tier": alarm.get("severity_by_tier") or {},
                "note": alarm.get("note"),
                "pattern": pattern,
            })

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)

    print(f"  {len(out)} event alarm(s) baked in: "
          f"{', '.join(e['alarm_id'] for e in out)}")
    for p in problems:
        print(f"  unsupported (no rule is created for it either): {p}")


if __name__ == "__main__":
    main()
