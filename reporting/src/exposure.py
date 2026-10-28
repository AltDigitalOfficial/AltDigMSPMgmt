"""What in one account can be reached from the internet, and what will expire.

ADR-015 and ADR-016. B-176.

------------------------------------------------------------------------------
Why this exists
------------------------------------------------------------------------------
ADR-015 (Jamie, 2026-10-28): in dev a tenant's team may build anything that
leaves the platform's controls intact, on condition that it appears in the
daily manager digest. Risk from public access is the tenant's to mitigate, and
the platform's job is to put it in front of the development manager every day
until somebody does.

On the day of the ruling, Arc8 dev had three CloudFront distributions with no
web ACL, in front of an instance whose SSH port was open to the whole internet.
The digest mentioned none of it. This module is what lets the digest say so.

Design 02 also makes certificate expiry monitoring AltDigital's under all three
service models, and nothing monitored it. The same read answers that, so the
expiry scan (expiry.py) imports this module rather than reading again in its
own way.

------------------------------------------------------------------------------
Two halves
------------------------------------------------------------------------------
  read_account()   API calls through the per-account read role
                   (reporting/00-exposure-reader.yaml). Every section is read
                   on its own, and a section that fails records its error and
                   lets the others continue. "Could not check" and "nothing
                   there" must stay distinguishable all the way to the email.

  assess(), expiring()
                   Pure. They turn an inventory into the lines a reader acts on.
                   No AWS calls, so the judgments are testable
                   (scripts/test-exposure.py) and the facts document holds the
                   raw inventory as well as the opinion of it.

------------------------------------------------------------------------------
What this does not check
------------------------------------------------------------------------------
NOT_CHECKED, below, is shown in the digest so that an empty section is not read
as "nothing is public". Each item there can make an account reachable, and none
of them is read.

------------------------------------------------------------------------------
It reports and never changes
------------------------------------------------------------------------------
The read role holds List/Describe/Get only. Nothing here closes a port, attaches
a web ACL or disables a distribution, and nothing may be added that does: the
hard rule on automated remediation, and ADR-015's ruling that dev mitigation is
the tenant's decision.
"""

import datetime

# The regions member accounts may operate in (config/platform.env,
# PLATFORM_ALLOWED_REGIONS). Passed in by the caller from its environment, not
# read here, so the list has one owner.
GLOBAL_REGION = "us-east-1"   # CloudFront, Route 53 and Route 53 Domains answer here

# Ports that make "open to the internet" worse than it already is, named so the
# line says what is exposed rather than a bare number.
ADMIN_PORTS = {22: "SSH", 3389: "RDP", 5985: "WinRM", 5986: "WinRM"}
DATA_PORTS = {
    1433: "SQL Server", 1521: "Oracle", 3306: "MySQL", 5432: "PostgreSQL",
    6379: "Redis", 9200: "Elasticsearch", 11211: "Memcached", 27017: "MongoDB",
}

WORLD_V4 = "0.0.0.0/0"
WORLD_V6 = "::/0"

# Every ACM key type. ListCertificates returns ONLY RSA_2048 unless asked for
# the others, so a reader that does not name them misses every ECDSA
# certificate and reports a clean account.
ACM_KEY_TYPES = ["RSA_1024", "RSA_2048", "RSA_3072", "RSA_4096",
                 "EC_prime256v1", "EC_secp384r1", "EC_secp521r1"]

# --- Expiry thresholds ------------------------------------------------------
#
# Certificates: 45 days, for every type. ACM starts renewing a managed
# certificate 60 days before it expires, so one still unrenewed at 45 has been
# failing to renew for two weeks, usually because its DNS validation record has
# gone. An imported certificate is never renewed by ACM, and 45 days is the
# notice ACM itself gives for those.
CERT_WARN_DAYS = 45
#
# Domains: Route 53 renews an auto-renewing domain some time before expiry, at
# a point that varies by top-level domain. 21 days is later than that point for
# the common TLDs, so a domain with auto-renew on that is still inside 21 days
# has probably failed to renew (a payment problem, or a TLD that needs action).
# That is an inference, not a documented guarantee for every TLD. With
# auto-renew off, nothing will renew it, so the warning starts at 45.
DOMAIN_WARN_DAYS_AUTORENEW = 21
DOMAIN_WARN_DAYS_MANUAL = 45
#
# Inside this, an expiry is urgent wherever the tier allows urgency.
URGENT_DAYS = 14

# Shown with every exposure section, so an empty one is never read as a
# statement that nothing is public.
NOT_CHECKED = (
    "Lambda function URLs", "S3 buckets or websites made public",
    "ECS tasks with public IPs", "App Runner, Elastic Beanstalk and Lightsail",
    "Global Accelerator",
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(ts):
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _error(exc):
    """A section's failure, in words that say whose problem it is.

    An explicit deny from a service control policy is not a read failure. It
    means the service cannot be used in this account at all, so nothing of that
    kind can exist here to be found. SCP 09 denies Route 53 Domains in
    HIPAA-enforced accounts, for instance. Recorded as `blocked`, which the
    digest treats as checked.
    """
    err = getattr(exc, "response", {}).get("Error", {}) if hasattr(exc, "response") else {}
    code = err.get("Code") or type(exc).__name__
    message = err.get("Message") or str(exc)
    if "service control policy" in message:
        return {"state": "blocked", "code": code}
    return {"state": "error", "code": code, "message": message[:300]}


def _tag(tags, key):
    for t in tags or []:
        if t.get("Key") == key:
            return t.get("Value")
    return None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
#
# Each reader takes a client factory, make(service, region), so tests can hand
# in stubs and the caller decides which credentials are behind it.


def _cloudfront(make):
    out = []
    for page in make("cloudfront", GLOBAL_REGION).get_paginator(
            "list_distributions").paginate():
        for d in (page.get("DistributionList") or {}).get("Items") or []:
            vc = d.get("ViewerCertificate") or {}
            out.append({
                "id": d.get("Id"),
                "domain": d.get("DomainName"),
                "aliases": (d.get("Aliases") or {}).get("Items") or [],
                "enabled": bool(d.get("Enabled")),
                # A WAFv2 ARN, a classic WAF id, or "" for none.
                "web_acl": d.get("WebACLId") or None,
                "viewer_protocol": (d.get("DefaultCacheBehavior") or {}).get(
                    "ViewerProtocolPolicy"),
                "default_certificate": bool(vc.get("CloudFrontDefaultCertificate")),
                "origins": [o.get("DomainName") for o in
                            (d.get("Origins") or {}).get("Items") or []],
            })
    return out


def _load_balancers(make, region):
    elb = make("elbv2", region)
    out = []
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page.get("LoadBalancers") or []:
            if lb.get("Scheme") != "internet-facing":
                continue
            entry = {"region": region, "name": lb.get("LoadBalancerName"),
                     "arn": lb.get("LoadBalancerArn"), "type": lb.get("Type"),
                     "dns": lb.get("DNSName"), "web_acl": None}
            if lb.get("Type") == "application":
                # Only an application load balancer can carry a web ACL. A
                # failed lookup is recorded on the entry rather than read as
                # "no web ACL", which would be a false accusation.
                try:
                    resp = make("wafv2", region).get_web_acl_for_resource(
                        ResourceArn=lb["LoadBalancerArn"])
                    entry["web_acl"] = (resp.get("WebACL") or {}).get("Name")
                except Exception as exc:                 # noqa: BLE001
                    entry["web_acl_error"] = _error(exc)
            out.append(entry)
    return out


def _prefix_list_names(ec2):
    names = {}
    for page in ec2.get_paginator("describe_managed_prefix_lists").paginate():
        for pl in page.get("PrefixLists") or []:
            names[pl.get("PrefixListId")] = pl.get("PrefixListName")
    return names


def _rules(group):
    """Inbound rules of one security group, flattened to one entry per source."""
    out = []
    for p in group.get("IpPermissions") or []:
        proto = p.get("IpProtocol")
        ports = (None, None) if proto == "-1" else (p.get("FromPort"), p.get("ToPort"))
        base = {"protocol": "all" if proto == "-1" else proto,
                "from": ports[0], "to": ports[1]}
        for r in p.get("IpRanges") or []:
            out.append({**base, "source": r.get("CidrIp")})
        for r in p.get("Ipv6Ranges") or []:
            out.append({**base, "source": r.get("CidrIpv6")})
        for r in p.get("PrefixListIds") or []:
            out.append({**base, "prefix_list": r.get("PrefixListId")})
        for r in p.get("UserIdGroupPairs") or []:
            out.append({**base, "group": r.get("GroupId")})
    return out


def _instances(make, region):
    ec2 = make("ec2", region)
    running = []
    for page in ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for r in page.get("Reservations") or []:
            for i in r.get("Instances") or []:
                if i.get("PublicIpAddress") or i.get("Ipv6Address"):
                    running.append(i)
    if not running:
        return []

    group_ids = sorted({g["GroupId"] for i in running for g in i.get("SecurityGroups") or []})
    groups = {}
    for page in ec2.get_paginator("describe_security_groups").paginate(GroupIds=group_ids):
        for g in page.get("SecurityGroups") or []:
            groups[g["GroupId"]] = g
    pl_names = _prefix_list_names(ec2)

    out = []
    for i in running:
        rules = []
        for g in i.get("SecurityGroups") or []:
            for rule in _rules(groups.get(g["GroupId"]) or {}):
                rule["group_name"] = g.get("GroupName")
                if rule.get("prefix_list"):
                    rule["prefix_list_name"] = pl_names.get(rule["prefix_list"])
                rules.append(rule)
        out.append({
            "region": region, "id": i.get("InstanceId"),
            "name": _tag(i.get("Tags"), "Name"),
            "public_ip": i.get("PublicIpAddress"),
            "public_dns": i.get("PublicDnsName") or None,
            "rules": rules,
        })
    return out


def _databases(make, region):
    out = []
    for page in make("rds", region).get_paginator("describe_db_instances").paginate():
        for db in page.get("DBInstances") or []:
            if db.get("PubliclyAccessible"):
                out.append({"region": region, "id": db.get("DBInstanceIdentifier"),
                            "engine": db.get("Engine")})
    return out


def _apis(make, region):
    out = []
    for page in make("apigateway", region).get_paginator("get_rest_apis").paginate():
        for api in page.get("items") or []:
            types = (api.get("endpointConfiguration") or {}).get("types") or []
            if "PRIVATE" in types:
                continue
            out.append({"region": region, "id": api.get("id"), "name": api.get("name"),
                        "kind": "REST", "default_endpoint": not api.get("disableExecuteApiEndpoint")})
    v2 = make("apigatewayv2", region)
    token = None
    while True:
        resp = v2.get_apis(**({"NextToken": token} if token else {}))
        for api in resp.get("Items") or []:
            out.append({"region": region, "id": api.get("ApiId"), "name": api.get("Name"),
                        "kind": api.get("ProtocolType") or "HTTP",
                        "default_endpoint": not api.get("DisableExecuteApiEndpoint")})
        token = resp.get("NextToken")
        if not token:
            return out


def _hosted_zones(make):
    out = []
    for page in make("route53", GLOBAL_REGION).get_paginator("list_hosted_zones").paginate():
        for z in page.get("HostedZones") or []:
            if (z.get("Config") or {}).get("PrivateZone"):
                continue
            out.append({"id": (z.get("Id") or "").rsplit("/", 1)[-1],
                        "name": (z.get("Name") or "").rstrip("."),
                        "records": z.get("ResourceRecordSetCount")})
    return out


def _domains(make):
    out = []
    for page in make("route53domains", GLOBAL_REGION).get_paginator("list_domains").paginate():
        for d in page.get("Domains") or []:
            out.append({"name": d.get("DomainName"), "expiry": _iso(d.get("Expiry")),
                        "auto_renew": d.get("AutoRenew"),
                        "transfer_lock": d.get("TransferLock")})
    return out


def _domain_operations(make, since):
    """Registrar operations submitted since `since`: a registration in
    progress, one that failed and why. A developer who registers a domain sees
    the outcome by email to themselves; this is how the manager sees it."""
    out = []
    pages = make("route53domains", GLOBAL_REGION).get_paginator("list_operations").paginate(
        SubmittedSince=since)
    for page in pages:
        for op in page.get("Operations") or []:
            out.append({"domain": op.get("DomainName"), "type": op.get("Type"),
                        "status": op.get("Status"), "submitted": _iso(op.get("SubmittedDate")),
                        "flag": op.get("StatusFlag"), "message": op.get("Message")})
    return out


def _certificates(make, region):
    out = []
    pages = make("acm", region).get_paginator("list_certificates").paginate(
        Includes={"keyTypes": ACM_KEY_TYPES})
    for page in pages:
        for c in page.get("CertificateSummaryList") or []:
            out.append({"region": region, "arn": c.get("CertificateArn"),
                        "domain": c.get("DomainName"), "status": c.get("Status"),
                        "type": c.get("Type"), "in_use": c.get("InUse"),
                        "not_after": _iso(c.get("NotAfter")),
                        "renewal_eligibility": c.get("RenewalEligibility")})
    return out


SECTIONS = ("cloudfront", "load_balancers", "instances", "databases", "apis",
            "hosted_zones", "domains", "domain_operations", "certificates")


def read_account(make, regions, since=None, sections=SECTIONS):
    """The inventory for one account.

    {section: [entries], "errors": {"section" or "section/region": {...}}}.
    A section in `errors` with state `error` was NOT checked, entirely or for
    that region, and the digest says so. `blocked` is a service control
    policy denying the service, which counts as checked (see _error).
    """
    inv = {s: [] for s in sections}
    inv["errors"] = {}
    since = since or (_now() - datetime.timedelta(days=1))

    def run(section, fn, *args, region=None):
        key = f"{section}/{region}" if region else section
        try:
            inv[section].extend(fn(*args))
        except Exception as exc:                         # noqa: BLE001
            inv["errors"][key] = _error(exc)

    if "cloudfront" in sections:
        run("cloudfront", _cloudfront, make)
    if "hosted_zones" in sections:
        run("hosted_zones", _hosted_zones, make)
    if "domains" in sections:
        run("domains", _domains, make)
    if "domain_operations" in sections:
        run("domain_operations", _domain_operations, make, since)
    for region in regions:
        for section, fn in (("load_balancers", _load_balancers),
                            ("instances", _instances),
                            ("databases", _databases),
                            ("apis", _apis),
                            ("certificates", _certificates)):
            if section in sections:
                run(section, fn, make, region, region=region)
    return inv


def unchecked(inv):
    """Sections, or section/region pairs, that could not be read."""
    return sorted(k for k, v in (inv.get("errors") or {}).items()
                  if v.get("state") != "blocked")


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


def _ports(rule):
    if rule.get("protocol") == "all" or rule.get("from") is None:
        return "all ports"
    if rule.get("from") == rule.get("to"):
        return f"port {rule['from']}"
    return f"ports {rule['from']}-{rule['to']}"


def _named(rule):
    """'SSH (22)' for a known port, else the bare range."""
    if rule.get("protocol") == "all" or rule.get("from") is None:
        return "all traffic"
    lo, hi = rule.get("from"), rule.get("to")
    for table in (ADMIN_PORTS, DATA_PORTS):
        for port, name in table.items():
            if lo <= port <= hi and lo == hi:
                return f"{name} ({port})"
    return _ports(rule)


def _world_open(rule):
    return rule.get("source") in (WORLD_V4, WORLD_V6)


def _is_sensitive(rule):
    if rule.get("protocol") == "all" or rule.get("from") is None:
        return True
    lo, hi = rule["from"], rule["to"]
    return any(lo <= p <= hi for p in list(ADMIN_PORTS) + list(DATA_PORTS))


def _instance_findings(inst):
    world = [r for r in inst.get("rules") or [] if _world_open(r)]
    limited = [r for r in inst.get("rules") or [] if r.get("prefix_list")]
    risks, notes = [], []
    # One line per distinct exposure, sensitive ones first: a manager reads the
    # first line, and SSH to the world should be it.
    seen = set()
    for r in sorted(world, key=lambda r: (not _is_sensitive(r), r.get("from") or 0)):
        label = _named(r)
        if label in seen:
            continue
        seen.add(label)
        if _is_sensitive(r):
            risks.append(f"{label} is open to the whole internet")
        else:
            notes.append(f"{label} is open to the whole internet")
    names = sorted({r.get("prefix_list_name") or r.get("prefix_list") for r in limited})
    if names:
        notes.append("other inbound is limited to: " + ", ".join(names))
    if not world and not limited:
        notes.append("has a public IP, but no inbound rule admits the internet")
    return risks, notes


def assess(inv):
    """Entries for the digest: [{kind, name, where, risks, notes}].

    `risks` are what ADR-015 says the manager must see: something an attacker
    can reach, with no layer the team chose in front of it. `notes` are facts
    worth knowing that are not, on their own, a gap.
    """
    out = []
    instances = inv.get("instances") or []
    by_host = {}
    for i in instances:
        for host in (i.get("public_dns"), i.get("public_ip")):
            if host:
                by_host[host] = i
    lbs = inv.get("load_balancers") or []
    by_lb = {lb.get("dns"): lb for lb in lbs if lb.get("dns")}

    for d in inv.get("cloudfront") or []:
        risks, notes = [], []
        if not d.get("enabled"):
            notes.append("disabled")
        if not d.get("web_acl"):
            risks.append("no web ACL (WAF) in front of it")
        if d.get("viewer_protocol") == "allow-all":
            risks.append("serves plain HTTP as well as HTTPS")
        if d.get("aliases"):
            notes.append("answers as " + ", ".join(d["aliases"]))
        for origin in d.get("origins") or []:
            inst = by_host.get(origin)
            if inst:
                world = [r for r in inst.get("rules") or [] if _world_open(r)]
                label = inst.get("name") or inst.get("id")
                if world:
                    # Name the ports. "Reachable directly" would overstate an
                    # origin whose web ports admit only CloudFront and whose
                    # SSH is the one thing open, which is Arc8 dev's case.
                    opened = ", ".join(sorted({_named(r) for r in world}))
                    risks.append(f"its origin, instance {label}, is itself open to the "
                                 f"whole internet on {opened}")
                else:
                    notes.append(f"origin is instance {label}, reachable only as its "
                                 "inbound rules allow")
            elif origin in by_lb:
                notes.append(f"origin is load balancer {by_lb[origin].get('name')}")
        out.append({"kind": "CloudFront distribution", "name": d.get("domain"),
                    "where": "global", "risks": risks, "notes": notes})

    for lb in lbs:
        risks, notes = [], []
        if lb.get("type") == "application":
            if lb.get("web_acl_error"):
                notes.append("could not check for a web ACL")
            elif not lb.get("web_acl"):
                risks.append("no web ACL (WAF) in front of it")
        else:
            notes.append(f"{lb.get('type')} load balancer: cannot carry a web ACL, so "
                         "its security groups are the only filter")
        out.append({"kind": "Internet-facing load balancer", "name": lb.get("name"),
                    "where": lb.get("region"), "risks": risks, "notes": notes})

    for i in instances:
        risks, notes = _instance_findings(i)
        name = f"{i.get('name')} ({i.get('id')})" if i.get("name") else i.get("id")
        out.append({"kind": "EC2 instance with a public IP", "name": name,
                    "where": i.get("region"), "risks": risks, "notes": notes})

    for db in inv.get("databases") or []:
        out.append({"kind": "Database marked publicly accessible", "name": db.get("id"),
                    "where": db.get("region"),
                    "risks": ["its address resolves from the internet; only its "
                              "security groups decide who can connect"],
                    "notes": [db.get("engine")] if db.get("engine") else []})

    for api in inv.get("apis") or []:
        notes = [f"{api.get('kind')} API"]
        if api.get("default_endpoint"):
            notes.append("default execute-api endpoint enabled")
        out.append({"kind": "Public API Gateway endpoint", "name": api.get("name") or api.get("id"),
                    "where": api.get("region"), "risks": [], "notes": notes})

    for z in inv.get("hosted_zones") or []:
        out.append({"kind": "Public DNS zone", "name": z.get("name"), "where": "global",
                    "risks": [], "notes": [f"{z.get('records')} records"]})

    for d in inv.get("domains") or []:
        notes = [f"expires {str(d.get('expiry') or '?')[:10]}",
                 "auto-renew on" if d.get("auto_renew") else "auto-renew OFF"]
        if d.get("transfer_lock") is False:
            notes.append("transfer lock off")
        out.append({"kind": "Domain registered in this account", "name": d.get("name"),
                    "where": "global", "risks": [], "notes": notes})

    for op in inv.get("domain_operations") or []:
        status = op.get("status")
        risks = []
        notes = [f"{(op.get('type') or '').replace('_', ' ').lower()}: {(status or '?').lower()}"]
        if status in ("ERROR", "FAILED"):
            risks.append("did not succeed" + (f": {op['message']}" if op.get("message") else ""))
        elif op.get("flag"):
            notes.append(f"waiting on: {op['flag'].replace('_', ' ').lower()}")
        out.append({"kind": "Domain registrar operation", "name": op.get("domain"),
                    "where": "global", "risks": risks, "notes": notes})

    for c in inv.get("certificates") or []:
        status = c.get("status")
        if status == "ISSUED":
            continue   # expiry is judged by expiring(); an issued cert is not exposure
        risks, notes = [], [f"status {status}"]
        if status == "PENDING_VALIDATION":
            notes.append("waiting for its DNS validation record")
        elif status in ("FAILED", "VALIDATION_TIMED_OUT"):
            risks.append("was not issued")
        elif status == "EXPIRED" and c.get("in_use"):
            risks.append("expired and still in use")
        out.append({"kind": "Certificate not in service", "name": c.get("domain"),
                    "where": c.get("region"), "risks": risks, "notes": notes})
    return out


def expiring(inv, now=None):
    """Certificates and domains inside their warning threshold.

    [{kind, name, where, expires, days, urgent, cause, resource}]. Sorted
    soonest first. Shared by the digest and the expiry scan so the two cannot
    disagree about what is expiring.
    """
    now = now or _now()
    out = []
    for c in inv.get("certificates") or []:
        if c.get("status") != "ISSUED":
            continue
        end = _parse(c.get("not_after"))
        if end is None:
            continue
        days = (end - now).days
        if days > CERT_WARN_DAYS:
            continue
        managed = c.get("type") == "AMAZON_ISSUED" and c.get("renewal_eligibility") == "ELIGIBLE"
        cause = ("ACM should have renewed this by now and has not. Usually the DNS "
                 "validation record has been removed."
                 if managed else
                 "ACM will not renew this certificate. Someone has to replace it.")
        if not c.get("in_use"):
            cause += " It is not attached to anything."
        out.append({"kind": "certificate", "name": c.get("domain"),
                    "where": c.get("region"), "expires": _iso(end), "days": days,
                    "urgent": days <= URGENT_DAYS, "cause": cause,
                    "resource": c.get("arn")})
    for d in inv.get("domains") or []:
        end = _parse(d.get("expiry"))
        if end is None:
            continue
        days = (end - now).days
        limit = DOMAIN_WARN_DAYS_AUTORENEW if d.get("auto_renew") else DOMAIN_WARN_DAYS_MANUAL
        if days > limit:
            continue
        cause = ("Auto-renew is on, but the domain has not renewed. Check the "
                 "registrar's renewal status and the account's payment method."
                 if d.get("auto_renew") else
                 "Auto-renew is off. Nothing will renew this domain.")
        out.append({"kind": "domain", "name": d.get("name"), "where": "global",
                    "expires": _iso(end), "days": days, "urgent": days <= URGENT_DAYS,
                    "cause": cause, "resource": d.get("name")})
    return sorted(out, key=lambda e: e["days"])
