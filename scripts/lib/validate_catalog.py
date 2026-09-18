#!/usr/bin/env python3
"""Validate runbooks/catalog.yaml against actions.py, and emit it as JSON.

    validate_catalog.py <catalog.yaml> <actions.py> <out.json>

Two failure modes this exists to prevent, both of which would otherwise appear
for the first time when an alarm fires on a customer's production resource:

  a catalog naming an action with no implementation
      the engine raises mid-incident, having already decided to act

  an implementation of a REFUSED action
      the platform quietly regains a capability somebody decided against, and
      nothing about the deployment looks unusual

The second is the reason the refused list is duplicated here rather than only
in the catalog's comments. A comment records a decision; this enforces it.
"""
import json
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python -m pip install --user pyyaml")

# Refused on 2026-09-18, D-012b. Implementing one of these is caught at
# packaging rather than discovered later.
REFUSED = {
    "expand_volume", "expand_storage", "flush_cache", "rds_failover",
    "drain_and_replace", "release_db_connections", "terminate_instance",
    "deregister_target", "promote_read_replica",
}

REQUIRED_TOP = ("schema_version", "default_gather", "failure_policy",
                "settle_seconds", "runbooks")


def alarm_ids_from_spec(spec_path):
    """Every alarm id 06a defines, following inherits.

    The catalog is keyed on these. A key that is not one of them silently finds
    no runbook — no error, no log line, just an incident nobody responded to.
    That happened once: `rds-storage-low` against 06a's `rds-free-storage-low`.
    """
    with open(spec_path, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    sets = spec.get("alarm_sets") or {}

    def resolve(rt, seen=None):
        seen = seen or set()
        if rt in seen:
            return []
        seen.add(rt)
        c = sets.get(rt)
        if c is None:
            return None
        out = []
        if c.get("inherits"):
            out.extend(resolve(c["inherits"], seen) or [])
        out.extend(c.get("alarms") or [])
        out.extend(c.get("additional_alarms") or [])
        return out

    return {rt: {a["id"] for a in (resolve(rt) or [])} for rt in sets}


def main():
    cat_path, actions_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    spec_path = sys.argv[4] if len(sys.argv) > 4 else None
    with open(cat_path, encoding="utf-8") as fh:
        cat = yaml.safe_load(fh)
    src = open(actions_path, encoding="utf-8").read()

    problems = [f"missing top-level key: {k}" for k in REQUIRED_TOP if k not in cat]
    if problems:
        sys.exit("catalog is invalid:\n  " + "\n  ".join(problems))

    implemented = set(re.findall(r'^\s+"([a-z_]+)": _', src, re.M))

    named_actions = set()
    named_gather = set(cat.get("default_gather") or [])
    stage3_unchecked = []
    scale_out_unchecked = []

    for rtype, rbs in (cat.get("runbooks") or {}).items():
        for aid, rb in (rbs or {}).items():
            named_gather |= set(rb.get("gather") or [])
            for spec in (rb.get("stage_3") or []):
                named_actions.add(spec["action"])
                if spec.get("requires_redundancy") is not True:
                    stage3_unchecked.append(f"{rtype}/{aid}/{spec['action']}")
            for spec in (rb.get("stage_2") or []):
                named_actions.add(spec["action"])
                if spec["action"] == "scale_out" and \
                        spec.get("requires_scale_in_policy") is not True:
                    scale_out_unchecked.append(f"{rtype}/{aid}")

    for a in sorted(named_actions - implemented):
        problems.append(f"catalog names action '{a}' with no implementation")
    for a in sorted(named_actions & REFUSED):
        problems.append(f"catalog names REFUSED action '{a}' (D-012b)")
    for a in sorted(implemented & REFUSED):
        problems.append(f"actions.py implements REFUSED action '{a}' (D-012b)")
    for g in sorted(named_gather - implemented):
        problems.append(f"catalog names gather step '{g}' with no entry")
    for s in stage3_unchecked:
        problems.append(f"stage 3 action without requires_redundancy: {s}")
    for s in scale_out_unchecked:
        problems.append(f"scale_out without requires_scale_in_policy: {s}")

    # Cross-check against 06a. Optional only so the validator still runs
    # without the design package present; when the path is supplied, a
    # mismatched id is fatal.
    if spec_path:
        by_type = alarm_ids_from_spec(spec_path)
        import difflib
        for rtype, rbs in (cat.get("runbooks") or {}).items():
            valid = by_type.get(rtype)
            if valid is None:
                problems.append(f"{rtype} is not a resource type in 06a")
                continue
            for aid in (rbs or {}):
                if aid not in valid:
                    near = difflib.get_close_matches(aid, sorted(valid), n=1, cutoff=0.6)
                    hint = f" (did you mean '{near[0]}'?)" if near else ""
                    problems.append(
                        f"{rtype}/'{aid}' is not a 06a alarm id{hint} — this runbook "
                        "would never be found")

    settle = cat.get("settle_seconds") or {}
    if "default" not in settle:
        for a in sorted(named_actions - set(settle)):
            problems.append(f"no settle period for '{a}' and no default")

    if problems:
        sys.exit("catalog is invalid:\n  " + "\n  ".join(problems))

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(cat, fh, indent=2)

    runbooks = sum(len(v) for v in cat["runbooks"].values())
    print(f"  {runbooks} runbook(s), {len(named_actions)} permitted action(s), "
          f"{len(named_gather)} gather step(s)")
    print(f"  {len(REFUSED)} refused action(s) blocked at packaging")


if __name__ == "__main__":
    main()
