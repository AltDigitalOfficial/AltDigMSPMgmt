"""Per-tenant Jira artefacts. Prompt 12.3, design/13-client-provisioning.md.

What this module REFUSES to do is the more important half
-----------------------------------------------------------
Prompt 12.3: "DO NOT automate per-project workflow, screen, custom field,
permission scheme or request type configuration." The reason is not effort. A
workflow that differs per tenant means the reconciliation engine has to know
which definition of "approved" applies to which client, and design 08's whole
model rests on there being exactly one. Configuration drift between tenants is
not a tidiness problem, it is a compliance one: two tenants whose change
records mean different things cannot be evidenced by the same control.

So the constraint is enforced rather than documented. `_write` checks every
path against `_ALLOWED_WRITE_PATHS` and anything outside it raises
`ConfigurationRefused`. A later edit that reaches for the workflow API fails on
its first call instead of succeeding quietly and being found by an auditor.

Label, not project, and not component either
----------------------------------------------
design 13 says "Project **or component**, inheriting the shared configuration
scheme". Neither is reachable. The platform credential holds no site-level
ADMINISTER permission, so it cannot create a project, and no ADMINISTER_PROJECTS
on the ops project, so it cannot create a component there either (B-108).

What remains is the label, which prompt 12.3 names directly in its idempotency
clause -- "keyed on the registry ID stamped into a label or custom field". It
is a real downgrade and worth being honest about:

    a project     separate permissions, separate configuration, separate
                  browse rights -- one tenant cannot see another
    a component   shared configuration, but a first-class field with its own
                  picker, its own filters and its own required-ness
    a label       shared configuration, shared visibility, and isolation that
                  is a naming convention rather than a permission

The third gives no confidentiality boundary at all. Anyone who can browse the
ops project sees every tenant's onboarding tasks. That is acceptable while the
only agents are the four partners, and it stops being acceptable the moment a
tenant needs its own people in the tool -- at which point this needs a project
per tenant and a credential that can create one. B-109, with that trigger
rather than a date.

Idempotency is keyed per PERIOD, not per obligation
-----------------------------------------------------
The recurring obligations are the subtle part. Keying idempotency on "this
tenant has an annual penetration test ticket" creates it once and then never
again, because the check keeps finding last year's. The recurrence stops, and
it stops silently -- the check is passing, which is exactly what it looks like
when everything is fine.

So the key is (obligation, period): `jt-obl-acme-pen-test-2026`. Each period is
a different marker, one ticket per period, and a period with no ticket reads as
an absence instead of hiding behind a satisfied check.
"""
import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# The read-only client, reused for its credential handling and -- more
# importantly -- for its JiraUnavailable type. "Jira says no" and "nobody could
# ask Jira" must stay distinguishable across every module that talks to it;
# defining a second exception here would be the first step to collapsing them.
import jira
from jira import JiraUnavailable

OPS_PROJECT = os.environ.get("JIRA_OPS_PROJECT", "")
TASK_ISSUETYPE = os.environ.get("JIRA_TASK_ISSUETYPE", "Task")
SUBTASK_ISSUETYPE = os.environ.get("JIRA_SUBTASK_ISSUETYPE", "Sub-task")
TIMEOUT = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "30"))
MAX_ATTEMPTS = int(os.environ.get("JIRA_MAX_ATTEMPTS", "5"))

# The only paths this module may write to. See the module docstring: the point
# is that adding a workflow call here has to be a deliberate act.
_ALLOWED_WRITE_PATHS = ("/rest/api/3/issue",)


class ConfigurationRefused(Exception):
    """A write was aimed at Jira configuration rather than at issue content."""


# ---------------------------------------------------------------------------
# The task set, transcribed from design 13 "Human tasks"
# ---------------------------------------------------------------------------
#
# The blocking/non-blocking split is carried into labels because design 13
# makes it load-bearing: "If everything is blocking, nothing goes live. If
# nothing is blocking, something goes live without a BAA." The go-live gate
# reads it back -- see `blocking_outstanding`.

BLOCKING = [
    ("Contract and BAA executed; notification window recorded",
     "Client level. The notification window is a contractual figure that the "
     "partner record carries, and provisioning step 0 blocks without it.",
     "Art / Wayne"),
    ("Partner relationship confirmed active",
     "Saga step 0 checks the registry flag mechanically. This task is the "
     "human confirmation sitting behind that flag.",
     "Art / Wayne"),
    ("Responsibility matrix signed",
     "Who operates what. Reviewed annually thereafter -- see the recurring "
     "obligation of the same name.",
     "Customer + Jamie"),
    ("Critical journey defined (questionnaire 7.1)",
     "What the canary is supposed to prove. Without it the canary ends up "
     "testing whatever was easiest to script.",
     "Customer, or Jamie where AltDigital builds the app"),
    ("Canary validated - does it actually test the right thing",
     "A canary that passes while the journey is broken is worse than no "
     "canary, because it is reported as healthy.",
     "Platform"),
    ("Licence entitlement evidence, where applicable",
     "Where applicable. Not applicable is still an answer, and it is recorded "
     "by closing this with that reason.",
     "Customer"),
    ("Billing configured; first invoice modelled",
     "Modelled, not merely configured. The first invoice is where a wrong "
     "Cost Category mapping becomes visible.",
     "Tina"),
    ("Go-live approval",
     "The last one, and the only one that moves the tenant to live. Saga step "
     "9 has no automated path past this and never will.",
     "Jamie"),
]

NON_BLOCKING = [
    ("First restore test review",
     "Dated and tracked. A restore that has never been reviewed is a backup, "
     "not a recovery capability.",
     "Platform"),
    ("Penetration test scheduled",
     "The scheduling, which is distinct from the annual obligation ticket "
     "that tracks the test itself.",
     "Art"),
    ("Tenant contacts confirmed",
     "Who gets called. An escalation policy pointing at someone who left "
     "fails at the worst possible moment.",
     "Wayne"),
    ("Monitoring tuning after the first instrumentation digest",
     "The first digest is the only one produced with no tenant-specific "
     "knowledge, which is what makes it worth reading closely.",
     "Platform"),
]

# key, summary, cadence, why
OBLIGATIONS = [
    ("pen-test", "Annual penetration test", "annual",
     "design 13 lists this among the recurring obligations. The scheduling "
     "task in onboarding is a different thing: that one gets the first test "
     "booked, this one recurs for the life of the tenant."),
    ("matrix-review", "Annual responsibility matrix review", "annual",
     "The matrix signed at onboarding describes who operates what. It goes "
     "stale silently -- nothing breaks at the moment it becomes wrong, which "
     "is exactly why it needs a ticket rather than a good intention."),
    ("access-review", "Quarterly access review", "quarterly",
     "Who can reach this tenant, checked against who should. Quarterly "
     "because design 11's developer access is time-bound, and a grant "
     "expiring is not the same event as a grant being removed."),
]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _url(path):
    site = jira._site()
    if not site:
        raise JiraUnavailable("JIRA_SITE is not set")
    return site + path


def _call(path, payload=None, method="POST"):
    """One Jira call, with back-off, refusing anything that is not content.

    Prompt 12.3 asks for back off and resume. Resume is the cheap half here:
    every caller is check-then-create, so a run that dies part way through is
    continued simply by running it again.
    """
    if method != "GET" and not path.startswith(_ALLOWED_WRITE_PATHS):
        raise ConfigurationRefused(
            f"{method} {path} is not issue content. This module writes issues "
            "and nothing else. Prompt 12.3 forbids automating workflow, "
            "screen, custom field, permission scheme and request type "
            "configuration, because per-tenant configuration drift breaks the "
            "single definition of 'approved' that design 08's reconciliation "
            "depends on. If such a call is genuinely needed, that is a design "
            "decision and belongs in a change record, not a code edit.")

    data = json.dumps(payload).encode() if payload is not None else None
    delay, last = 1.0, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            _url(path), data=data, method=method,
            headers={"Authorization": jira._auth_header(),
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode()
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: {exc.read().decode()[:400]}"
            if exc.code == 429:
                # Retry-After is authoritative where present. Guessing a
                # shorter wait than Jira asked for is how a throttle becomes
                # a block.
                wait = float(exc.headers.get("Retry-After") or delay)
            elif exc.code >= 500:
                wait = delay
            else:
                # Any other 4xx will not become true by waiting for it.
                raise JiraUnavailable(f"{method} {path} refused: {last}")
        except Exception as exc:                        # noqa: BLE001
            last, wait = f"{type(exc).__name__}: {str(exc)[:300]}", delay
        if attempt == MAX_ATTEMPTS:
            break
        time.sleep(min(wait, 60.0))
        delay = min(delay * 2, 60.0)

    raise JiraUnavailable(
        f"{method} {path} failed after {MAX_ATTEMPTS} attempts: {last}. "
        "Nothing is lost by this -- every creation here is check-then-create, "
        "so re-running continues from whatever was already made.")


def _search(jql, max_results=50):
    """Read issues. The allow-list gates writes only; reads are unrestricted
    because reading configuration is how this module avoids assuming it."""
    query = urllib.parse.quote(jql)
    return _call(f"/rest/api/3/search/jql?jql={query}"
                 f"&maxResults={max_results}&fields=summary,status,labels",
                 None, method="GET")


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def tenant_label(tenant_id):
    """The one string tying every artefact back to the registry record."""
    if not tenant_id:
        raise ValueError(
            "tenant_id is required. An unlabelled artefact cannot be found "
            "again, which means the next run creates a second one.")
    return f"tenant-{tenant_id}"


def _marker(*parts):
    return "jt-" + "-".join(str(p) for p in parts)


def _period_key(cadence, when=None):
    """Which period an obligation belongs to.

    See the module docstring. This is the thing that stops a recurrence from
    being created once and then never again.
    """
    when = when or datetime.datetime.now(datetime.timezone.utc)
    if cadence == "annual":
        return str(when.year)
    if cadence == "quarterly":
        return f"{when.year}Q{(when.month - 1) // 3 + 1}"
    raise ValueError(
        f"unknown cadence '{cadence}'. A cadence that falls through to a "
        "default would give every period the same key, which silently "
        "collapses the recurrence to a single ticket.")


def _adf(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": text}]}]}


# ---------------------------------------------------------------------------
# Check-then-create
# ---------------------------------------------------------------------------


def find_by_marker(project, marker):
    """The check half. Returns an issue key, or None.

    Searches by marker label rather than by summary text on purpose. A summary
    gets edited by whoever is working the ticket; the marker is the
    idempotency key and nothing in the human workflow touches it.
    """
    jql = f'project = "{project}" AND labels = "{marker}" ORDER BY created ASC'
    issues = (_search(jql, max_results=2) or {}).get("issues") or []
    if len(issues) > 1:
        # Not fatal, but the key has stopped being unique. Say so rather than
        # quietly taking the first and leaving a duplicate nobody knows about.
        print(json.dumps({
            "record_type": "jira-tenant-warning",
            "marker": marker,
            "problem": "more than one issue carries this idempotency marker, "
                       "so a duplicate was created at some point and this "
                       "check has been returning only one of them",
            "keys": [i.get("key") for i in issues]}))
    return issues[0].get("key") if issues else None


def ensure_issue(project, issuetype, summary, description, labels,
                 parent=None):
    """Create an issue only if its marker is not already present.

    The marker is the FIRST label by convention, not by API requirement, so
    that somebody reading the ticket can see which string makes it unique.
    """
    marker = labels[0]
    existing = find_by_marker(project, marker)
    if existing:
        return {"key": existing, "created": False, "marker": marker}

    fields = {
        "project": {"key": project},
        "issuetype": {"name": issuetype},
        "summary": summary,
        "description": _adf(description),
        "labels": list(labels),
    }
    if parent:
        fields["parent"] = {"key": parent}

    created = _call("/rest/api/3/issue", {"fields": fields})
    key = created.get("key")
    if not key:
        # An API that accepted a request is not an API that completed one --
        # the same rule the saga applies to each of its own steps.
        raise JiraUnavailable(
            f"Jira accepted the creation of '{summary}' but returned no issue "
            "key, so its existence cannot be confirmed. Re-running is safe; "
            "if an issue was in fact created, the marker search will find it.")
    return {"key": key, "created": True, "marker": marker}


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def provision(tenant_id, project=None, when=None):
    """Create the onboarding set and the current period's obligations.

    Safe to run repeatedly, and safe to run after a partial failure. Run a
    year later it creates that year's obligations and leaves everything else
    alone, which is the same property that makes it resumable.
    """
    project = project or OPS_PROJECT
    if not project:
        raise ValueError("JIRA_OPS_PROJECT is not set")

    label = tenant_label(tenant_id)
    made, found = [], []

    def note(result, kind):
        (made if result["created"] else found).append(
            {"key": result["key"], "kind": kind, "marker": result["marker"]})

    # The onboarding parent. AMI has no Epic issue type -- its hierarchy tops
    # out at level 0 -- so design 13's "onboarding epic" becomes a Task with
    # sub-tasks, which is the closest containment the project actually offers.
    parent = ensure_issue(
        project, TASK_ISSUETYPE,
        f"Onboarding: {tenant_id}",
        f"Onboarding task set for tenant {tenant_id}, from design 13 'Human "
        "tasks'. Sub-tasks labelled 'blocking' gate go-live; those labelled "
        "'non-blocking' are tracked and dated but do not gate it.",
        [_marker("onboarding", tenant_id), label, "onboarding"])
    note(parent, "onboarding-parent")

    for index, (summary, why, owner) in enumerate(BLOCKING):
        note(ensure_issue(
            project, SUBTASK_ISSUETYPE, summary,
            f"{why}\n\nOwner: {owner}. BLOCKING -- go-live does not proceed "
            "while this is open.",
            [_marker("onb", tenant_id, "b", index), label, "onboarding",
             "blocking"],
            parent=parent["key"]), "onboarding-blocking")

    for index, (summary, why, owner) in enumerate(NON_BLOCKING):
        note(ensure_issue(
            project, SUBTASK_ISSUETYPE, summary,
            f"{why}\n\nOwner: {owner}. Tracked and dated; does not gate "
            "go-live.",
            [_marker("onb", tenant_id, "n", index), label, "onboarding",
             "non-blocking"],
            parent=parent["key"]), "onboarding-non-blocking")

    for key, summary, cadence, why in OBLIGATIONS:
        period = _period_key(cadence, when)
        note(ensure_issue(
            project, TASK_ISSUETYPE, f"{summary} ({period}): {tenant_id}",
            f"{why}\n\nRecurring {cadence} obligation for tenant {tenant_id}, "
            f"period {period}. One ticket per period -- the idempotency key "
            "includes the period so that next period gets its own.",
            [_marker("obl", tenant_id, key, period), label, "obligation",
             f"obligation-{key}"]), f"obligation-{key}")

    out = {"record_type": "jira-tenant-provision",
           "tenant_id": tenant_id,
           "project": project,
           "tenant_label": label,
           "onboarding_key": parent["key"],
           "created": made,
           "already_present": found,
           "created_count": len(made),
           "already_present_count": len(found)}
    print(json.dumps(out, default=str))
    return out


def blocking_outstanding(tenant_id, project=None):
    """What still gates go-live. Read by saga step 9.

    Step 9 approves nothing -- that is a human decision and stays one. This
    exists so the gate can say WHY it is closed rather than only that it is.
    """
    project = project or OPS_PROJECT
    label = tenant_label(tenant_id)
    jql = (f'project = "{project}" AND labels = "{label}" '
           'AND labels = "blocking" ORDER BY created ASC')
    issues = (_search(jql, max_results=50) or {}).get("issues") or []

    if not issues:
        # Zero outstanding and zero existing are the same number with opposite
        # meanings, and reporting the first when it is the second is the
        # failure this codebase keeps finding (B-086, B-092, B-094).
        return {"tenant_id": tenant_id,
                "onboarding_present": False,
                "gate_clear": False,
                "outstanding": None,
                "why": "no blocking onboarding tasks exist for this tenant. "
                       "That is not 'nothing outstanding' -- it means the "
                       "onboarding set was never created, so provisioning "
                       "step 4 has not run for this tenant."}

    open_issues = [
        {"key": i.get("key"), "summary": (i.get("fields") or {}).get("summary")}
        for i in issues
        if (((i.get("fields") or {}).get("status") or {})
            .get("statusCategory", {}).get("key") != "done")]

    return {"tenant_id": tenant_id,
            "onboarding_present": True,
            "blocking_total": len(issues),
            "outstanding": open_issues,
            "gate_clear": not open_issues}


HANDLERS = {
    "provision": lambda e: provision(e.get("tenant_id"), e.get("project")),
    "blocking": lambda e: blocking_outstanding(e.get("tenant_id"),
                                               e.get("project")),
}


def handler(event, context=None):
    action = (event or {}).get("action") or "provision"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(
            f"unknown action '{action}'. Known: {', '.join(HANDLERS)}.")
    return fn(event or {})
