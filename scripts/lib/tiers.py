"""Resolve an environment tier's settings from its two axis positions.

Prompt 13.1, design/14-environment-tiers.md.

    python scripts/lib/tiers.py <tiers.yaml> resolve uat
    python scripts/lib/tiers.py <tiers.yaml> validate
    python scripts/lib/tiers.py <tiers.yaml> matrix

Why a resolver rather than a lookup table
-------------------------------------------
config/tiers.yaml stores each tier's POSITION on two axes and nothing else.
Every attribute that follows an axis is computed here. That is the whole
mechanism prompt 13.1 asks for: there is no per-tier compliance_scope field to
forget to change when UAT is added by copying test.

The validation half matters as much as the resolution. `validate` asserts that
the model still holds -- that every attribute belongs to exactly one axis, that
no tier overrides an axis attribute, and that UAT's two axes disagree. That
last check is the one that fails if somebody "simplifies" this back into a
single ordered scale, which is the thing design 14 exists to prevent.

No third-party YAML parser
----------------------------
Deliberately hand-parsed, in the same spirit as the rest of the repository's
scripts: this runs from Git Bash on a workstation and from a Lambda build step,
and adding a pip dependency to read one file the repository controls is a poor
trade. The parser accepts the subset this file uses and REFUSES anything else
rather than guessing -- an unrecognised line raises, because a silently skipped
line in a compliance-scope table is the worst possible failure.
"""
import json
import sys


def _coerce(raw):
    text = raw.strip()
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~", ""):
        return None
    if text.startswith(">-") or text.startswith("|"):
        return text
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        return text


def parse(path):
    """The subset of YAML config/tiers.yaml uses, and nothing more.

    Supports nested mappings by indentation, `- item` lists, block scalars
    introduced by `>-`, and scalars. Anything else raises: this file decides
    whether an environment is in compliance scope, and a parser that skipped a
    line it did not understand would answer that question wrongly and quietly.
    """
    root = {}
    # (indent, container) stack. A container is a dict or a list.
    stack = [(-1, root)]
    pending_block = None       # (container, key, indent)
    block_lines = []

    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    for number, line in enumerate(lines, 1):
        if pending_block is not None:
            container, key, block_indent = pending_block
            if line.strip() == "" or (len(line) - len(line.lstrip())) > block_indent:
                block_lines.append(line.strip())
                continue
            container[key] = " ".join(x for x in block_lines if x)
            pending_block, block_lines = None, []

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            raise ValueError(f"{path}:{number}: indentation below the document")
        container = stack[-1][1]

        if stripped.startswith("- "):
            if not isinstance(container, list):
                raise ValueError(
                    f"{path}:{number}: list item where a mapping was expected")
            container.append(_coerce(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ValueError(
                f"{path}:{number}: cannot parse '{stripped}'. This parser "
                "accepts only the subset config/tiers.yaml uses, and refuses "
                "rather than guessing -- a skipped line here would answer a "
                "compliance-scope question wrongly and silently.")

        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()

        if not isinstance(container, dict):
            raise ValueError(f"{path}:{number}: mapping inside a list")

        if value in (">-", ">", "|", "|-"):
            pending_block = (container, key, indent)
            block_lines = []
            continue

        if value == "":
            # Look ahead: a list follows if the next meaningful line is `- `.
            child = {}
            for following in lines[number:]:
                if not following.strip() or following.strip().startswith("#"):
                    continue
                if (len(following) - len(following.lstrip())) <= indent:
                    break
                child = [] if following.strip().startswith("- ") else {}
                break
            container[key] = child
            stack.append((indent, child))
            continue

        container[key] = _coerce(value)

    if pending_block is not None:
        container, key, _ = pending_block
        container[key] = " ".join(x for x in block_lines if x)

    return root


def resolve(spec, tier):
    """Every effective setting for one tier.

    Axis attributes are computed from the tier's position. tier_specific
    entries are overlaid afterwards, and each carries the axis it came from so
    a caller can tell a derived value from a declared one.
    """
    tiers = spec.get("tiers") or {}
    if tier not in tiers:
        raise KeyError(
            f"unknown tier '{tier}'. Known: {', '.join(sorted(tiers))}.")

    position = tiers[tier]
    out = {"tier": tier,
           "purpose": position.get("purpose"),
           "provisioned_by_default": position.get("provisioned_by_default"),
           "data_handling": position.get("data_handling"),
           "resilience": position.get("resilience"),
           "settings": {},
           "source": {}}

    for axis_name, axis in (spec.get("axes") or {}).items():
        chosen = position.get(axis_name)
        if chosen is None:
            raise ValueError(
                f"tier '{tier}' declares no position on the '{axis_name}' "
                "axis. Every tier must state both, because the two are what a "
                "tier IS -- see design 14.")
        values = (axis.get("values") or {}).get(chosen)
        if values is None:
            raise ValueError(
                f"tier '{tier}' claims {axis_name}='{chosen}', which the axis "
                "does not define.")
        for key, value in values.items():
            out["settings"][key] = value
            out["source"][key] = f"axis:{axis_name}={chosen}"

    for key, entry in (spec.get("tier_specific") or {}).items():
        if tier in entry:
            out["settings"][key] = entry[tier]
            out["source"][key] = "tier_specific"

    return out


def validate(spec):
    """Assert the model still holds. Returns a list of problems."""
    problems = []
    axes = spec.get("axes") or {}
    tiers = spec.get("tiers") or {}
    specific = spec.get("tier_specific") or {}

    # 1. Every axis attribute is defined by every value of that axis. A value
    #    missing an attribute would resolve to absent, and absent reads as
    #    false for a boolean like regulated_data_is_finding.
    for axis_name, axis in axes.items():
        declared = set(axis.get("attributes") or [])
        for value_name, values in (axis.get("values") or {}).items():
            missing = declared - set(values)
            extra = set(values) - declared
            if missing:
                problems.append(
                    f"axis {axis_name}.{value_name} does not define "
                    f"{sorted(missing)}; an absent setting reads as false.")
            if extra:
                problems.append(
                    f"axis {axis_name}.{value_name} defines {sorted(extra)}, "
                    "which the axis does not declare.")

    # 2. No attribute belongs to two axes, and none is also tier_specific.
    #    Either would make the effective value depend on evaluation order.
    seen = {}
    for axis_name, axis in axes.items():
        for attribute in axis.get("attributes") or []:
            if attribute in seen:
                problems.append(
                    f"'{attribute}' belongs to both {seen[attribute]} and "
                    f"{axis_name}; the resolved value would depend on the "
                    "order the axes happen to be iterated in.")
            seen[attribute] = axis_name
    for attribute in specific:
        if attribute in seen:
            problems.append(
                f"'{attribute}' is both an axis attribute ({seen[attribute]}) "
                "and tier_specific; tier_specific silently wins, which hides "
                "the axis it was supposed to follow.")

    # 3. No tier sets an axis attribute directly. This is the check that
    #    catches somebody adding `compliance_scope: in` to uat because it was
    #    quicker than reading the axis.
    for tier, position in tiers.items():
        for attribute in position:
            if attribute in seen:
                problems.append(
                    f"tier '{tier}' sets '{attribute}' directly. Axis "
                    "attributes are derived from the tier's position and must "
                    "not be overridden -- an override is how the two axes "
                    "quietly become one again.")

    # 4. Every tier_specific entry covers every tier, and carries a reason.
    for attribute, entry in specific.items():
        if not entry.get("reason"):
            problems.append(
                f"tier_specific '{attribute}' has no reason. Every entry here "
                "asserts that a value depends on the tier's NAME rather than "
                "on what the tier is, and that assertion has to be arguable.")
        for tier in tiers:
            if tier not in entry:
                problems.append(
                    f"tier_specific '{attribute}' does not cover '{tier}'.")

    # 5. THE LOAD-BEARING ONE. At least one tier's axes must disagree.
    #
    #    If every tier sits at the same position on both axes, the two axes
    #    are indistinguishable from a single ordered scale, and the whole file
    #    is an elaborate way of writing four columns. design 14 exists because
    #    UAT breaks that alignment. A change that re-aligns it has undone the
    #    design without touching the design document.
    divergent = [
        name for name, position in tiers.items()
        if position.get("data_handling") != position.get("resilience")
        and not (position.get("data_handling") == "production_equivalent"
                 and position.get("resilience") == "production")
    ]
    if not divergent:
        problems.append(
            "no tier's two axes disagree. design 14's entire point is that "
            "UAT's data is production-equivalent while its resilience is not; "
            "if every tier aligns, this is a single scale wearing two hats "
            "and UAT will be implemented by copying test.")

    return problems


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    spec = parse(argv[1])
    command = argv[2]

    if command == "validate":
        problems = validate(spec)
        if problems:
            for problem in problems:
                print(f"  {problem}")
            return 1
        tiers = sorted(spec.get("tiers") or {})
        print(f"ok: {len(tiers)} tiers, axes hold: {', '.join(tiers)}")
        return 0

    if command == "resolve":
        print(json.dumps(resolve(spec, argv[3]), indent=2, sort_keys=True))
        return 0

    if command == "matrix":
        tiers = sorted(spec.get("tiers") or {})
        resolved = {t: resolve(spec, t) for t in tiers}
        keys = sorted({k for r in resolved.values() for k in r["settings"]})
        width = max(len(k) for k in keys)
        print(f"{'':<{width}}  " + "  ".join(f"{t:>12}" for t in tiers))
        for key in keys:
            row = "  ".join(
                f"{str(resolved[t]['settings'].get(key)):>12}" for t in tiers)
            print(f"{key:<{width}}  {row}")
        return 0

    print(f"unknown command '{command}'")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
