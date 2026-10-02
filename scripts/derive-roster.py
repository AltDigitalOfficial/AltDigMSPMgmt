#!/usr/bin/env python3
"""Validate a developer roster against its questionnaire and derive the grants.

    scripts/derive-roster.py --roster r.yaml --questionnaire q.yaml
    scripts/derive-roster.py --roster r.yaml --questionnaire q.yaml --json
    scripts/derive-roster.py --roster r.yaml --questionnaire q.yaml --members dev

The sibling of scripts/derive-vesting.py, and split out for the same three
reasons: the derivation is testable without an AWS account, the grants can be
reviewed and argued about before anybody has access, and the review happens
against a document rather than against a terminal transcript.

------------------------------------------------------------------------------
Why the roster is a separate file from the questionnaire
------------------------------------------------------------------------------
vesting/questionnaire.schema.json sets additionalProperties:false at every
level, so section 3b has nowhere to live in the manifest without a schema
change -- and the questionnaire has a committed example copy, which is exactly
the file a roster must never be able to reach.

The lifetimes differ too, which is the better argument. A questionnaire is
answered once and is largely irreversible. A roster changes every time someone
joins or leaves, and treating those as one document means re-opening the
irreversible answers to add a contractor.

------------------------------------------------------------------------------
The tenant cross-check is the point of this file
------------------------------------------------------------------------------
Everything else here is tidiness. The check that matters is that the roster's
`tenant` block matches the questionnaire's `identity` block, because the
failure it prevents is silent: a roster copied from the last client and edited
carelessly grants real people access to another client's accounts, and nothing
downstream has any way to notice. The account IDs resolve, the groups are
created, the assignments apply, and the run reports success.

So identity is checked before anything else and blocks rather than warns.
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python -m pip install --user pyyaml")

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("jsonschema is required: python -m pip install --user jsonschema")


# The Windows console defaults to cp1252 and renders every em-dash below as a
# replacement character. Same reasoning as derive-vesting.py: these messages
# are read by somebody deciding who gets access to production.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
ROSTER_SCHEMA = REPO_ROOT / "vesting" / "roster.schema.json"
QUESTIONNAIRE_SCHEMA = REPO_ROOT / "vesting" / "questionnaire.schema.json"
RULES_PATH = REPO_ROOT / "vesting" / "derivation-rules.yaml"

# design/11-developer-access.md's gradient, as a mapping from environment to
# the permission set that expresses it.
#
# A constant and not a line in derivation-rules.yaml, deliberately. That file
# is documented as "configuration, not code -- it is meant to be edited", and
# the test for belonging in it is whether a human deriving the value by hand
# could legitimately disagree. Nobody may legitimately decide that production
# gets the dev grant. Changing this mapping is a security decision and should
# cost a code review, not a config edit.
#
# UAT takes the PROD set. design 11: "UAT is treated as production for access
# purposes, because it holds real data." derive-vesting.py reaches the same
# conclusion independently for data handling (uat_compliance_posture); this is
# the access half of the same rule.
PERMISSION_SET = {
    "dev": "developer-dev",
    "test": "developer-test",
    "uat": "developer-prod",
    "prod": "developer-prod",
}

# The oversight grant. Not in PERMISSION_SET because it is not a point on the
# gradient: one group, every environment, the same read-only set in each.
READONLY_PERMISSION_SET = "tenant-readonly"

# Environments where the grant reaches real data and a person's presence is
# worth a second look. Used for the `exception` requirement and the review
# summary, not for the permission set -- that comes from PERMISSION_SET above.
SENSITIVE = ("uat", "prod")

# Identity Center userName. The identity store accepts a good deal more than
# this, but config/identity.env's record format uses pipe and comma as
# delimiters and scripts/grant-developer-access.sh emits that format, so a name
# containing either would be unparseable one step later.
USERNAME_RE = re.compile(r"^[^|,\s]+$")


class Halt(Exception):
    """The roster is internally coherent and describes something unsafe.

    Same distinction derive-vesting.py draws: a validation error means the file
    is wrong and can be corrected, a Halt means the file is right and the
    answer is no.
    """


def load(path, loader):
    if not path.exists():
        sys.exit(f"Not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return loader(fh)


def vested_environments(q, rules):
    """The environments this tenant actually has, from the UAT election.

    Derived the same way derive-vesting.py derives it rather than imported from
    it, because importing would make this file unusable without the vesting
    module on the path and the rule is one line. If the two ever disagree the
    roster grants access to an account that does not exist, which fails loudly
    at assignment time rather than quietly.
    """
    envs = list(rules["environments_always"])
    if q["environments"]["uat_required"]:
        envs += rules["environments_optional"]
    return envs


def name_of(person):
    """For messages. Never the email on its own -- see the note in report()."""
    return f"{person['given_name']} {person['family_name']}"


def user_name_of(person):
    """The sign-in name: the override if present, otherwise the email.

    Identical semantics to config/identity.env's optional fourth field, and for
    the identical reason. See scripts/deploy-commercial-access.sh's
    record_username.
    """
    return (person.get("user_name") or "").strip() or person["email"].strip()


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
# Each returns a list of error strings rather than raising, so a reviewer sees
# every problem at once instead of one per round trip. The exception is
# check_tenant_matches, which raises Halt -- there is no value in reporting
# further problems about a roster that is pointed at the wrong client.


def check_tenant_matches(roster, q):
    """The one that matters. See the module docstring."""
    t, i = roster["tenant"], q["identity"]
    mismatches = [
        (field, t[field], i[qfield])
        for field, qfield in (("partner", "partner"),
                              ("client_slug", "client_slug"),
                              ("application_code", "application_code"))
        if t[field] != i[qfield]
    ]
    if mismatches:
        lines = "\n".join(
            f"      {f:18s} roster: {r!r}   questionnaire: {qv!r}"
            for f, r, qv in mismatches)
        raise Halt(
            "The roster does not name the same tenant as the questionnaire.\n\n"
            f"{lines}\n\n"
            "    Nothing is derived. This is almost always a roster copied from\n"
            "    another client and edited incompletely, and it is the one error\n"
            "    here with no downstream symptom: the accounts resolve, the groups\n"
            "    are created, the assignments apply, and real people end up with\n"
            "    access to a client they do not work for.")
    return []


def check_environments_exist(roster, envs):
    errs = []
    for p in roster["developers"]:
        for e in p["environments"]:
            if e not in envs:
                extra = ""
                if e == "uat":
                    extra = ("\n    The questionnaire has uat_required false, so no UAT account is "
                             "vested.\n    Electing UAT is question 5.1 and is marked Locked -- it cannot be "
                             "added\n    by listing it here.")
                errs.append(
                    f"developers[{name_of(p)}].environments: '{e}' is not one of this "
                    f"tenant's environments ({', '.join(envs)}).{extra}")
    return errs


def check_no_duplicates(roster):
    """Two records for one human, or two humans claiming one sign-in name.

    Both are silent in different ways. Duplicate emails usually mean somebody
    was added twice with different environment lists, and only one of the two
    takes effect -- whichever the reconciliation happens to apply last. A
    duplicate userName means two people share an identity, which defeats the
    per-person accountability the whole access model rests on.
    """
    errs = []
    for label, extract in (
            ("an email address", lambda p: p["email"].strip().lower()),
            ("a sign-in name", lambda p: user_name_of(p).lower())):
        seen = {}
        for p in roster["developers"]:
            value = extract(p)
            if value in seen:
                errs.append(
                    f"Two developer records share {label} ({value}): "
                    f"{seen[value]} and {name_of(p)}.\n"
                    "    Merge them into one record with the union of their "
                    "environments. Two records for one identity means only one "
                    "of them takes effect, and which one is an accident of "
                    "ordering.")
            seen[value] = name_of(p)
    return errs


def check_usernames_parseable(roster):
    errs = []
    for p in roster["developers"]:
        u = user_name_of(p)
        if not USERNAME_RE.match(u):
            errs.append(
                f"developers[{name_of(p)}]: sign-in name {u!r} contains a pipe, "
                "comma or whitespace.\n"
                "    Those are the delimiters config/identity.env's record format "
                "uses, so the name would be unparseable one step later.")
    return errs


def check_prod_has_exception(roster):
    """Questionnaire 3b: 'tell us who and why rather than working around it.'

    Blocks rather than warns. The grant itself is read-and-observe and cannot
    change anything, so this is not about risk of damage -- it is about being
    able to answer an auditor's "who approved this" from the file instead of
    from memory, months later, when the person who approved it has left.
    """
    errs = []
    for p in roster["developers"]:
        reaching = [e for e in p["environments"] if e in SENSITIVE]
        if reaching and not (p.get("exception") or "").strip():
            errs.append(
                f"developers[{name_of(p)}] reaches {', '.join(reaching)} but has no "
                "`exception` recorded.\n"
                "    Questionnaire 3b asks for who and why. The grant is "
                "read-and-observe and changes nothing, so this is not about damage "
                "-- it is so that 'who approved this' has an answer in the file "
                "rather than in somebody's memory.")
    return errs


def warnings_for(roster, envs):
    """Things that do not block but that a reviewer should see before applying."""
    w = []
    people = roster["developers"]

    if not people:
        readers = len(roster.get("readonly") or [])
        w.append(
            "No developers on the roster. Legal -- section 3b is marked Dev, not "
            "Vest, so a\n    tenant can be vested before its roster is known -- "
            "and it means nobody can\n    build in these accounts when this run "
            f"finishes. {readers} read-only member(s) can see\n    them.")
        return w

    for e in envs:
        if not any(e in p["environments"] for p in people):
            w.append(
                f"No developer reaches {e}. Expected while {e} is not yet vested, "
                "or for a\n    pipeline-only environment. Otherwise, once the "
                "account exists, no developer\n    can sign in to it -- worth "
                "confirming.")

    for p in people:
        if "dev" in envs and "dev" not in p["environments"]:
            w.append(
                f"{name_of(p)} reaches {', '.join(p['environments'])} but not dev.\n"
                "    Unusual. Access normally widens downward, and a person who can "
                "see\n    production but has nowhere to reproduce a problem tends to "
                "acquire dev\n    access informally later.")
        if p.get("console_access") is False:
            w.append(
                f"{name_of(p)} is marked pipeline-only (console_access false), and "
                "the\n    permission sets are NOT split on that yet -- they will "
                "receive the same\n    grant as everyone else in "
                f"{', '.join(p['environments'])}. Recorded, not enforced. B-131.")

    sensitive = [p for p in people if set(p["environments"]) & set(SENSITIVE)]
    if sensitive:
        w.append(
            f"{len(sensitive)} of {len(people)} people reach uat or prod:\n" +
            "\n".join(f"      {name_of(p):28s} {', '.join(p['environments'])}"
                      for p in sensitive) +
            "\n    Read-and-observe only (design 11), and still the list an auditor "
            "asks\n    about. Each carries a recorded exception; confirm they are "
            "still true.")
    return w


def _is_manager(person, manager_email):
    return person["email"].strip().lower() == manager_email


def check_readonly(roster, q):
    """Questionnaire 3.6 and 3.6a, held against the roster.

    The development manager is the one person always owed the read-only view,
    whoever they are -- this time a partner, next time the client's own staff
    with no other access at all. So the manager must be on the read-only list,
    and 3.6a decides whether they may ALSO be on the developer list.

    Both directions block. An `oversight` manager who turns up among the
    developers would be granted build access the questionnaire says they do not
    have, and a `contributor` who is missing would be refused access they were
    promised. Either one is the roster and the questionnaire disagreeing, and
    the fix is to make them agree, not to pick a winner here.
    """
    errs = []
    readonly = roster.get("readonly") or []
    mgr = q["security"]["developer_manager"]
    mgr_email = mgr["email"].strip().lower()
    role = q["security"]["developer_manager_role"]

    if not any(_is_manager(p, mgr_email) for p in readonly):
        errs.append(
            f"The development manager ({mgr['name']}, questionnaire 3.6) is not "
            "on the read-only list.\n"
            "    Add them under `readonly`, matched by the same email as 3.6. The "
            "manager always\n    receives read-only access to every environment, "
            "whatever 3.6a says.")

    on_dev = any(_is_manager(p, mgr_email) for p in roster["developers"])
    if role == "oversight" and on_dev:
        errs.append(
            f"{mgr['name']} is the development manager, 3.6a says `oversight`, "
            "and they are on\n    the developer roster. Either the answer is out "
            "of date -- change 3.6a to\n    `contributor` -- or the roster entry "
            "is a mistake. Not resolved here: one of\n    the two documents is "
            "wrong and this cannot tell which.")
    if role == "contributor" and not on_dev:
        errs.append(
            f"{mgr['name']} is the development manager and 3.6a says "
            "`contributor`, but they are\n    not on the developer roster. Add "
            "them under `developers` with the environments\n    they build in, "
            "or change 3.6a to `oversight`.")

    for p in readonly:
        if not _is_manager(p, mgr_email) and not (p.get("reason") or "").strip():
            errs.append(
                f"readonly[{name_of(p)}] has no `reason`. Read-only access "
                "reaches every environment,\n    production logs included -- "
                "'why does this person see prod' should have an\n    answer in "
                "the file.")

    for label, extract in (
            ("an email address", lambda p: p["email"].strip().lower()),
            ("a sign-in name", lambda p: user_name_of(p).lower())):
        seen = {}
        for p in readonly:
            value = extract(p)
            if value in seen:
                errs.append(
                    f"Two read-only records share {label} ({value}): "
                    f"{seen[value]} and {name_of(p)}. Merge them.")
            seen[value] = name_of(p)

    for p in readonly:
        if not USERNAME_RE.match(user_name_of(p)):
            errs.append(
                f"readonly[{name_of(p)}]: sign-in name {user_name_of(p)!r} "
                "contains a pipe, comma or whitespace.")
    return errs


CHECKS = (check_environments_exist, check_no_duplicates,
          check_usernames_parseable, check_prod_has_exception, check_readonly)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive(roster, envs):
    """Group names, permission sets and per-environment membership.

    One group per ACCOUNT, not per person and not per role. That 1:1 mapping is
    what makes the assignment stack reviewable -- a reader checks that each
    group reaches exactly one account, and the access gradient is expressed by
    which permission set the group is bound to rather than by who is in it.

    The group name mirrors the account alias so the correspondence is legible
    in the console, where the two appear in different services and nothing
    links them.
    """
    t = roster["tenant"]
    stem = f"{t['partner']}-{t['client_slug']}-{t['application_code']}"

    groups = {}
    for e in envs:
        members = [p for p in roster["developers"] if e in p["environments"]]
        groups[e] = {
            "group_name": f"Developers-{stem}-{e}",
            "account_alias": f"altdig-{stem}-{e}",
            "permission_set": PERMISSION_SET[e],
            # The SSM parameter identity/10-developer-access.yaml publishes the
            # ARN to. Read rather than hardcoded so that redeploying the
            # permission sets cannot leave this pointing at a stale ARN.
            "permission_set_param": f"/identity/permission-set/{PERMISSION_SET[e]}",
            "members": [
                {
                    "given_name": p["given_name"].strip(),
                    "family_name": p["family_name"].strip(),
                    "email": p["email"].strip(),
                    "user_name": user_name_of(p),
                    "console_access": p.get("console_access", True),
                    "exception": (p.get("exception") or "").strip() or None,
                }
                for p in members
            ],
        }

    # The oversight group. Named for the TENANT rather than an account,
    # because it reaches all of them.
    readonly = {
        "group_name": f"ReadOnly-{stem}",
        "permission_set": READONLY_PERMISSION_SET,
        "permission_set_param": f"/identity/permission-set/{READONLY_PERMISSION_SET}",
        "environments": envs,
        "members": [
            {
                "given_name": p["given_name"].strip(),
                "family_name": p["family_name"].strip(),
                "email": p["email"].strip(),
                "user_name": user_name_of(p),
                "reason": (p.get("reason") or "").strip() or None,
            }
            for p in (roster.get("readonly") or [])
        ],
    }

    return {
        "tenant_stem": stem,
        "partner": t["partner"],
        "client_slug": t["client_slug"],
        "application_code": t["application_code"],
        "environments": envs,
        "groups": groups,
        "readonly": readonly,
        "people_total": len(roster["developers"]),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def report(d, w):
    print("-" * 70)
    print(f"Derived developer access -- {d['partner']}/{d['client_slug']}/"
          f"{d['application_code']}")
    print("-" * 70)
    print(f"  people on the roster  : {d['people_total']}")
    print()
    for e in d["environments"]:
        g = d["groups"][e]
        print(f"  {e}")
        print(f"    group        : {g['group_name']}")
        print(f"    account      : {g['account_alias']}")
        print(f"    grant        : {g['permission_set']}")
        if not g["members"]:
            print("    members      : (nobody)")
        else:
            for i, m in enumerate(g["members"]):
                label = "    members      : " if i == 0 else "                   "
                # Name AND sign-in name. The sign-in name alone is what the
                # identity store keys on and is often unrecognisable; the name
                # alone is not unique. An operator approving this needs both.
                flag = "" if m["console_access"] else "   [pipeline-only]"
                print(f"{label}{m['given_name']} {m['family_name']}"
                      f"  <{m['user_name']}>{flag}")
        print()

    r = d["readonly"]
    print("  read-only, every environment")
    print(f"    group        : {r['group_name']}")
    print(f"    accounts     : {', '.join(r['environments'])}")
    print(f"    grant        : {r['permission_set']}")
    for i, m in enumerate(r["members"]):
        label = "    members      : " if i == 0 else "                   "
        print(f"{label}{m['given_name']} {m['family_name']}  <{m['user_name']}>")
    print()

    if w:
        print("-" * 70)
        print("  Review before applying:")
        print("-" * 70)
        for item in w:
            print(f"  ! {item}\n")
    print("-" * 70)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roster", required=True,
                    help="Developer roster sidecar, YAML.")
    ap.add_argument("--questionnaire", required=True,
                    help="The completed questionnaire this roster belongs to.")
    ap.add_argument("--json", action="store_true",
                    help="Emit the derived grants as JSON for the pipeline.")
    ap.add_argument("--members", metavar="ENV",
                    help="Emit that environment's members as "
                         "GivenName|FamilyName|email|userName records, one per "
                         "line -- the format config/identity.env uses and "
                         "grant-developer-access.sh parses. ENV may also be "
                         "'readonly' for the oversight group.")
    args = ap.parse_args()

    roster = load(Path(args.roster), yaml.safe_load)
    q = load(Path(args.questionnaire), yaml.safe_load)
    rules = load(RULES_PATH, yaml.safe_load)

    # Schema first, for both documents. The cross-field checks assume the shape
    # is right, and running them against a malformed file produces KeyErrors
    # instead of advice.
    for label, doc, schema_path in (
            ("roster", roster, ROSTER_SCHEMA),
            ("questionnaire", q, QUESTIONNAIRE_SCHEMA)):
        schema = load(schema_path, json.load)
        errors = sorted(Draft202012Validator(schema).iter_errors(doc),
                        key=lambda e: list(e.path))
        if errors:
            print(f"The {label} failed schema validation:\n", file=sys.stderr)
            for e in errors:
                loc = ".".join(str(p) for p in e.path) or "(root)"
                print(f"  {loc}: {e.message}", file=sys.stderr)
            sys.exit(1)

    envs = vested_environments(q, rules)

    try:
        check_tenant_matches(roster, q)
    except Halt as h:
        print(f"\nHALT -- this roster is not for this tenant.\n\n    {h}\n",
              file=sys.stderr)
        sys.exit(2)

    problems = []
    for check in CHECKS:
        if check is check_environments_exist:
            problems.extend(check(roster, envs))
        elif check is check_readonly:
            problems.extend(check(roster, q))
        else:
            problems.extend(check(roster))
    if problems:
        print("The roster is well-formed but cannot be applied:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        sys.exit(1)

    d = derive(roster, envs)

    if args.members == "readonly":
        for m in d["readonly"]["members"]:
            print(f"{m['given_name']}|{m['family_name']}|{m['email']}|"
                  f"{m['user_name']}")
        return

    if args.members:
        if args.members not in d["groups"]:
            sys.exit(f"'{args.members}' is not one of this tenant's environments "
                     f"({', '.join(d['environments'])}).")
        for m in d["groups"][args.members]["members"]:
            print(f"{m['given_name']}|{m['family_name']}|{m['email']}|"
                  f"{m['user_name']}")
        return

    if args.json:
        print(json.dumps(d, indent=2))
        return

    report(d, warnings_for(roster, envs))


if __name__ == "__main__":
    main()
