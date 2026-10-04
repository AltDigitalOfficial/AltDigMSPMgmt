"""The HIPAA eligible-services catalog: load, validate, and answer questions.

Pure: no boto3, no network, no environment. Four consumers read the same file
and must agree exactly:

    scripts/validate-hipaa-eligibility.py   checks the file before any commit
    eligibility/src/generate.py             builds the SCP (B-160)
    eligibility/src/sweep.py                classifies what accounts used (B-161)
    eligibility/src/upstream.py             diffs the AWS page (B-159)

A second parser in any of them would be a second opinion about which services
may hold PHI, and the disagreement would surface only as a denial in prod or a
silence in dev.

See docs/hipaa-eligibility.md.
"""

import re
import unicodedata

COLUMNS = ["service", "on_aws_list", "status", "iam_prefixes", "config_namespaces",
           "deny_actions", "alternative", "date_added", "reviewed_by", "notes"]

# Statuses whose prefixes the SCP permits for every principal.
ALLOWLISTED = {"eligible", "carve-out"}
# Permitted only for named platform principals (design §3, Statement B).
PLATFORM_ONLY = {"platform-only"}
# Denied in uat/prod. "pending" is here deliberately: an unconfirmed prefix
# fails closed, and the cost of being wrong is an exception request rather
# than PHI on a service the BAA does not cover.
DENIED = {"pending", "not-allowlisted"}
STATUSES = ALLOWLISTED | PLATFORM_ONLY | DENIED

PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ACTION_RE = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9]+$")
CONFIG_RE = re.compile(r"^AWS::[A-Za-z0-9]+(::[A-Za-z0-9]+)?$")


class CatalogError(ValueError):
    pass


def normalise_service(name):
    """The form the upstream diff compares on.

    The AWS page renders an en dash that does not survive every decoding (it
    arrived as U+FFFD once), carries literal "\\n" sequences inside list items,
    and pads with trailing spaces. None of that is a change to the list, and
    reporting it as one would train the reviewer to dismiss the diff.
    """
    s = unicodedata.normalize("NFKC", name).replace("\\n", " ")
    s = re.sub(r"[‐-―−�]", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _split(value, sep=","):
    return [v.strip() for v in value.split(sep) if v.strip()]


def parse_deny(value):
    """'a:X,a:Y@key=v1,v2; b:Z' -> [{"actions": [...], "key": k|None, "values": [...]}]"""
    entries = []
    for chunk in _split(value, ";"):
        acts, _, cond = chunk.partition("@")
        actions = _split(acts)
        key, values = None, []
        if cond:
            key, _, vals = cond.partition("=")
            key, values = key.strip(), _split(vals)
            if not key or not values:
                raise CatalogError(f"deny_actions condition malformed: {chunk!r}")
        for a in actions:
            if not ACTION_RE.match(a):
                raise CatalogError(f"deny_actions entry is not prefix:Action: {a!r}")
        entries.append({"actions": actions, "key": key, "values": values})
    return entries


def load(path):
    """Returns (list_version, rows). Raises CatalogError on anything malformed."""
    version, header, rows = None, None, []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith("#"):
                continue
            if line.startswith("list_version:"):
                version = line.split(":", 1)[1].strip()
                continue
            fields = line.split("\t")
            if header is None:
                if fields != COLUMNS:
                    raise CatalogError(f"line {n}: header is {fields}, expected {COLUMNS}")
                header = fields
                continue
            if len(fields) != len(COLUMNS):
                raise CatalogError(f"line {n}: {len(fields)} fields, expected {len(COLUMNS)}")
            r = dict(zip(COLUMNS, fields))
            r["line"] = n
            r["prefixes"] = _split(r["iam_prefixes"])
            r["configs"] = _split(r["config_namespaces"])
            r["deny"] = parse_deny(r["deny_actions"])
            rows.append(r)
    if not version:
        raise CatalogError("no list_version line")
    if header is None:
        raise CatalogError("no header row")
    return version, rows


def validate(rows, known_prefixes=None):
    """Every structural rule. Returns a list of problems; empty means valid.

    known_prefixes, when given, is the set of IAM service prefixes AWS
    publishes (the Service Authorization Reference). A prefix outside it is a
    typo, and a typo in an allowlist is a service silently denied, or in a
    denylist one silently permitted.
    """
    problems = []
    seen_service = {}
    prefix_status = {}
    config_status = {}
    for r in rows:
        where = f"line {r['line']} ({r['service']})"
        if r["status"] not in STATUSES:
            problems.append(f"{where}: status {r['status']!r} is not one of {sorted(STATUSES)}")
        if r["on_aws_list"] not in ("yes", "no"):
            problems.append(f"{where}: on_aws_list must be yes or no")
        if r["on_aws_list"] == "no" and r["status"] == "eligible":
            problems.append(f"{where}: eligible but not on the AWS list. "
                            "A carve-out is not eligibility; use status carve-out or pending")
        if r["on_aws_list"] == "yes" and r["status"] == "carve-out":
            problems.append(f"{where}: on the AWS list but marked carve-out")
        key = normalise_service(r["service"])
        if key in seen_service:
            problems.append(f"{where}: duplicate service (also line {seen_service[key]})")
        seen_service[key] = r["line"]
        if r["status"] == "not-allowlisted" and r["on_aws_list"] == "no" \
                and not r["alternative"] and r["prefixes"]:
            problems.append(f"{where}: not-allowlisted with no alternative. "
                            "The digest tells the developer what to use instead")
        if r["reviewed_by"] and re.search(r"[@\s]", r["reviewed_by"]):
            problems.append(f"{where}: reviewed_by looks like a name or email. Opaque id only")
        for p in r["prefixes"]:
            if not PREFIX_RE.match(p):
                problems.append(f"{where}: prefix {p!r} is malformed")
            if "*" in p:
                problems.append(f"{where}: wildcard prefix {p!r}. Wildcards admit future services")
            if known_prefixes is not None and p not in known_prefixes:
                problems.append(f"{where}: prefix {p!r} is not in the Service Authorization Reference")
            prefix_status.setdefault(p, set()).add(r["status"])
        for c in r["configs"]:
            if not CONFIG_RE.match(c):
                problems.append(f"{where}: config entry {c!r} is not AWS::Ns or AWS::Ns::Type")
            config_status.setdefault(c, set()).add(r["status"])
        for d in r["deny"]:
            for a in d["actions"]:
                if a.split(":")[0] not in r["prefixes"]:
                    problems.append(f"{where}: deny action {a} is outside this row's prefixes")
    for p, sts in sorted(prefix_status.items()):
        allowed = bool(sts & ALLOWLISTED)
        denied = bool(sts & DENIED)
        if allowed and denied:
            problems.append(f"prefix {p!r} is both allowlisted and denied ({sorted(sts)})")
        if bool(sts & PLATFORM_ONLY) and (allowed or denied):
            problems.append(f"prefix {p!r} is platform-only and also {sorted(sts - PLATFORM_ONLY)}")
    for c, sts in sorted(config_status.items()):
        if sts & ALLOWLISTED and sts & DENIED:
            problems.append(f"config entry {c!r} is both allowlisted and denied ({sorted(sts)}). "
                            "Use a full type on the denied row so the most specific match decides")
    return problems


def prefix_sets(rows):
    """(allowlisted, platform_only, denied) as sorted lists of IAM prefixes."""
    def collect(statuses):
        return sorted({p for r in rows if r["status"] in statuses for p in r["prefixes"]})
    return collect(ALLOWLISTED), collect(PLATFORM_ONLY), collect(DENIED)


def deny_entries(rows):
    """Feature-level denies from allowlisted rows, de-duplicated."""
    out, seen = [], set()
    for r in rows:
        if r["status"] not in ALLOWLISTED:
            continue
        for d in r["deny"]:
            k = (tuple(d["actions"]), d["key"], tuple(d["values"]))
            if k not in seen:
                seen.add(k)
                out.append(d)
    return out


def classify_prefix(rows, prefix):
    """What the catalog says about one IAM prefix.

    Returns (status, row) where status is a catalog status or "unlisted": a
    prefix the catalog does not mention at all. Unlisted is reported as its own
    thing by the sweep, never folded into not-allowlisted — it means the
    catalog is incomplete, which is a question for the catalog's owner, not a
    finding against the developer.
    """
    best = None
    for r in rows:
        if prefix in r["prefixes"]:
            # A prefix shared by several rows has one status by construction
            # (validate() refuses mixed allowlisted/denied); prefer a row that
            # carries an alternative, because that is what the digest prints.
            if best is None or (r["alternative"] and not best["alternative"]):
                best = r
    if best is None:
        return "unlisted", None
    return best["status"], best


def classify_config_type(rows, resource_type):
    """Most specific match wins: AWS::ECR::PublicRepository before AWS::ECR."""
    ns = "::".join(resource_type.split("::")[:2])
    exact = [r for r in rows if resource_type in r["configs"]]
    if exact:
        return _one_status(exact), exact[0]
    by_ns = [r for r in rows if ns in r["configs"]]
    if by_ns:
        return _one_status(by_ns), by_ns[0]
    return "unlisted", None


def _one_status(matches):
    sts = {m["status"] for m in matches}
    if sts & DENIED:
        return sorted(sts & DENIED)[0]
    return sorted(sts)[0]


def page_services(rows):
    """Normalised service names of the rows that mirror the AWS page."""
    return {normalise_service(r["service"]) for r in rows if r["on_aws_list"] == "yes"}
