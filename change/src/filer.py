"""Files change records. Prompt 8.2's write half, kept deliberately apart.

Why this is a separate module from jira.py
------------------------------------------
`jira.py` is read-only by construction and `scripts/publish-change.sh` refuses
to package it if a write-shaped function appears. That guard exists because a
credential able to edit issues could manufacture the approval it then verifies,
which would make the quorum control (8.3) circular.

None of that changes because the platform now files records. What changes is
that there are two jobs, and they get two modules, two Lambda functions and two
IAM roles:

    jira.py      reads.  Used by reconciliation and by quorum verification.
    filer.py     writes. Used by nothing that verifies anything.

WHAT THIS DOES NOT BUY, stated plainly: real separation needs two Atlassian
accounts, and there is one. The account behind the shared credential can write
regardless of which module reads the secret, and it is currently a human who is
also an approver. So this is code-level separation over a credential that does
not yet have it. B-070 carries the fix -- a genuine service account, which will
not be an approver -- and that is the thing that makes the quorum control sound
rather than merely tidy.

One hard line, enforced in the publish guard: this module never APPROVES and
never TRANSITIONS. Filing a change and approving it are different acts, and the
platform may only do the first. If it could do both, the record it files would
prove nothing.

Two ways to create a record, and the difference matters
-------------------------------------------------------
A change record created through `/rest/api/3/issue` is a valid Change issue and
is NOT a JSM request. It carries no Request Type -- JSM calls that field
`vp-origin` -- and every servicedeskapi endpoint returns 404 for it, including
the approval endpoint. It will not appear in a portal queue and cannot carry an
approval.

That was discovered by asking AMI-1 for its approvals and getting:

    404  Either you do not have access to this request type, or it does not
         exist.   i18nKey: sd.customerview.error.vpOriginMissing

So where the project is a service desk with a matching request type, records are
raised as REQUESTS, which is what makes them approvable and therefore what makes
quorum verification (8.3) possible at all. Where it is not -- a tenant on plain
Jira Software, which not every tenant project will avoid being -- the issue API
is used and the record is still a record, just not an approvable one.

The two paths take different payloads for the same field, which is the trap
this module exists to absorb: the issue API takes rich text as ADF, the request
API takes it as a plain string. Values are therefore held in their raw form and
rendered per path, rather than being built once and sent to whichever endpoint
happened to answer.

Field ids are resolved by NAME, never hardcoded
-----------------------------------------------
`customfield_10059` is "Implementation plan" on this site and something else
entirely on another. The ids are discovered per run from read-only metadata, so
this survives a different Jira, a reconfigured project, and the tenant projects
that do not exist yet.
"""
import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

import jira

secrets = boto3.client("secretsmanager")

JIRA_SECRET_NAME = os.environ.get("JIRA_SECRET_NAME",
                                  "platform/jira/service-account")
OPS_PROJECT = os.environ.get("JIRA_OPS_PROJECT", "")
CHANGE_ISSUETYPE = os.environ.get("JIRA_CHANGE_ISSUETYPE", "[System] Change")
# The request type whose issues are change records. Matched by NAME because the
# numeric id differs per site; "Request a change" is JSM's own default name for
# the change management request type.
CHANGE_REQUEST_TYPE = os.environ.get("JIRA_CHANGE_REQUEST_TYPE",
                                     "Request a change")
TIMEOUT = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "30"))

# Defaults for build-phase records, decided with Jamie 2026-09-19.
#
# Standard, and the label is not decoration. "Standard" in ITIL means
# pre-approved, low-risk and repeatable, and novel platform build work is none
# of those -- it is being called Standard so that records count as approved
# while the platform is being built, and `build-phase` is what stops that
# category being silently overclaimed later. Switch to Normal once the platform
# is in service.
DEFAULT_CHANGE_TYPE = os.environ.get("CHANGE_TYPE_DEFAULT", "Standard")
DEFAULT_REASON = os.environ.get("CHANGE_REASON_DEFAULT", "New functionality")
BUILD_LABEL = "build-phase"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _jira_dt(dt):
    """Jira wants an offset without a colon: 2026-09-19T12:00:00.000+0000."""
    return dt.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000+0000")


def _adf(text):
    """Plain text as an Atlassian Document Format document.

    The issue API takes rich text as ADF, not as a string. Newlines become
    separate paragraphs, because a plan collapsed onto one line is not a plan
    anybody can follow at 3am.
    """
    paragraphs = str(text or "").split("\n")
    return {
        "type": "doc", "version": 1,
        "content": [
            {"type": "paragraph",
             "content": ([{"type": "text", "text": p}] if p.strip() else [])}
            for p in paragraphs
        ] or [{"type": "paragraph", "content": []}],
    }


# Jira custom field types that take ADF on the ISSUE api. Substring match:
# Atlassian namespaces these as
# com.atlassian.jira.plugin.system.customfieldtypes:textarea and JSM adds its
# own, so the discriminating part is the tail.
_RICH_TEXT_CUSTOM_TYPES = ("textarea", "richtext")

# System fields that are rich text and carry no `custom` type at all.
_RICH_TEXT_SYSTEM_FIELDS = ("description", "environment")


def _is_rich_text(field):
    """Does this field take ADF on the issue API?

    `schema.type` says "string" for both plain and rich text, which is the trap
    that cost the first filing attempt: three fields rejected as "not valid
    Atlassian Document Format" with nothing in the metadata obviously saying
    why. The custom type is the discriminator. B-075.
    """
    custom = (field.get("custom") or "").lower()
    if any(t in custom for t in _RICH_TEXT_CUSTOM_TYPES):
        return True
    return field.get("id") in _RICH_TEXT_SYSTEM_FIELDS


def _request(path, payload, method="POST"):
    url = f"{jira._site()}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": jira._auth_header(),
                 "Accept": "application/json",
                 "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:800]
        except Exception:                          # noqa: BLE001
            pass
        # Jira reports a rejected field by its CUSTOM FIELD ID, not its label,
        # so the raw body is kept whole. "customfield_10059 is not valid
        # Atlassian Document Format" means nothing without it, and the three
        # plausible causes -- wrong format, wrong value, field not on the
        # screen -- are not distinguishable from a summarised message.
        raise jira.JiraUnavailable(
            f"Jira rejected {method} {path} with HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise jira.JiraUnavailable(f"could not reach Jira: {exc}") from exc


def _post(path, payload):
    return _request(path, payload, "POST")


def _field_ids(project, issue_type_name):
    """Map field NAME -> metadata for creating this issue type."""
    id_to_name = jira.project_issue_type_map(project)
    type_id = {v: k for k, v in id_to_name.items()}.get(issue_type_name)
    if not type_id:
        raise jira.JiraUnavailable(
            f"project {project} has no issue type '{issue_type_name}'. "
            f"It has: {', '.join(sorted(id_to_name.values()))}")
    by_name = {}
    for f in jira.field_requirements(project, type_id):
        if f.get("name"):
            by_name[f["name"]] = f
    return by_name


def _request_form(project):
    """The service desk and request type that raise a change, or None.

    None is a normal answer, not an error: a tenant on plain Jira Software has
    no service desk, and a record filed there is still a record. What it is not
    is approvable, and the caller says so in its output rather than letting a
    quorum check discover it later as a 404.
    """
    try:
        desks = jira.service_desks()
    except jira.JiraUnavailable:
        # No JSM on this site at all. Not fatal; the issue API still works.
        return None
    for desk in desks:
        if desk.get("project_key") != project:
            continue
        for rt in jira.request_types(desk["id"]):
            if (rt.get("name") or "").lower() == CHANGE_REQUEST_TYPE.lower():
                form = jira.request_type_fields(desk["id"], rt["id"])
                return {"service_desk_id": desk["id"],
                        "request_type_id": rt["id"],
                        "request_type_name": rt.get("name"),
                        "fields": form["fields"]}
    return None


def file_change(event, context=None):
    """Open a change record BEFORE the work, per Jamie's decision.

    Returns the issue key. Everything the platform then does is expected to
    reference it, which is what lets reconciliation match a deployment to the
    record that authorised it rather than to whatever else was open at the time.
    """
    project = event.get("project") or OPS_PROJECT
    if not project:
        raise ValueError("no project: set JIRA_OPS_PROJECT or pass project")
    summary = event.get("summary")
    if not summary:
        raise ValueError("a change record without a summary is not a record")

    start = _now()
    end = start + datetime.timedelta(
        hours=float(event.get("window_hours") or 4))

    # Values in their RAW form, keyed by field name. Rendering is deferred
    # because the two creation paths disagree about the shape of rich text:
    # the issue API wants ADF, the request API wants a plain string. Building
    # the payload once and sending it to whichever endpoint answered is exactly
    # how this module would send ADF to an endpoint that rejects it.
    desired = {
        "Change type": {"value": event.get("change_type")
                        or DEFAULT_CHANGE_TYPE},
        "Change reason": {"value": event.get("reason") or DEFAULT_REASON},
        "Change risk": {"value": event.get("risk") or "Low"},
        "Impact": {"value": event.get("impact") or "Minor / Localized"},
        "Planned start": _jira_dt(start),
        "Planned end": _jira_dt(end),
        "Implementation plan": event.get("plan"),
        "Backout plan": event.get("backout"),
        "Test plan": event.get("test_plan"),
        "Business Test Plan": event.get("business_test_plan"),
        "Description": event.get("description") or event.get("reason_text"),
    }
    # `extra` reaches a field this code has never heard of, by name:
    # {"Business Test Plan": "..."}. Tenant projects will not all look like
    # AMI, and a field added to a screen should not need a code change.
    desired.update(event.get("extra") or {})

    # Accounts travel as labels. Reconciliation reads `account-<id>` to decide
    # which deployments a change record covers, so a record that loses them
    # matches nothing and every deployment in that account is reported as
    # unapproved. That is why a label failure below is fatal rather than
    # cosmetic.
    labels = [BUILD_LABEL] + [f"account-{a}"
                              for a in (event.get("accounts") or [])]

    form = _request_form(project)
    if form:
        result = _raise_request(form, summary, desired, labels)
    else:
        result = _create_issue(project, summary, desired, labels)

    out = {"record_type": "change-record-filed",
           "project": project, "summary": summary,
           "planned_start": _jira_dt(start), "planned_end": _jira_dt(end),
           "labels": labels,
           "approvable": bool(form),
           **result}
    print(json.dumps(out, default=str))
    return out


def _create_issue(project, summary, desired, labels):
    """Plain issue API. Everything in one call, but not a JSM request."""
    available = _field_ids(project, CHANGE_ISSUETYPE)
    fields = {"project": {"key": project},
              "issuetype": {"name": CHANGE_ISSUETYPE},
              "summary": summary[:250]}
    wrapped = []
    for name, value in desired.items():
        f = available.get(name)
        if f is None or value is None:
            continue
        if _is_rich_text(f) and isinstance(value, str):
            value = _adf(value)
            wrapped.append(name)
        fields[f["id"]] = value
    if "Labels" in available and labels:
        fields["labels"] = labels

    created = _post("/rest/api/3/issue", {"fields": fields})
    return {"key": created.get("key"),
            "created_via": "issue-api",
            "rich_text_fields": sorted(wrapped),
            "fields_set": sorted(k for k in fields
                                 if k not in ("project", "issuetype",
                                              "summary")),
            "fields_unavailable": sorted(n for n, v in desired.items()
                                         if v is not None
                                         and n not in available),
            # Said plainly rather than left to be discovered by a 404 during a
            # quorum check: this record cannot carry a JSM approval.
            "approval_note": (
                f"{project} has no '{CHANGE_REQUEST_TYPE}' request type, so "
                "this record was created through the issue API. It is not a "
                "JSM request and cannot carry an approval.")}


def _raise_request(form, summary, desired, labels):
    """JSM request API. Approvable, but the form takes a narrower field set.

    Two differences from the issue API, both learned from the live form rather
    than assumed:

    - Rich text goes as a PLAIN STRING. The request API renders it; sending ADF
      here is the mirror image of B-075.
    - Labels are not on the request form at all, so they are set immediately
      afterwards by an edit. That edit happens before any approval exists, so
      it cannot disturb design 08 condition 4, "not edited since approval".
    """
    by_name = {f.get("name"): f for f in form["fields"] if f.get("name")}

    values = {"summary": summary[:250]}
    unavailable = []
    for name, value in desired.items():
        if value is None:
            continue
        f = by_name.get(name)
        if f is None:
            unavailable.append(name)
            continue
        values[f["id"]] = value

    created = _post("/rest/servicedeskapi/request", {
        "serviceDeskId": str(form["service_desk_id"]),
        "requestTypeId": str(form["request_type_id"]),
        "requestFieldValues": values,
    })
    key = created.get("issueKey") or created.get("issueId")

    # Labels, in a second call, because the request form has no labels field.
    #
    # Fatal on failure, deliberately. A change record with no `account-<id>`
    # label matches no deployment, so reconciliation would report every
    # deployment in that account as unapproved -- a false accusation caused by
    # a silent half-success here. Better a loud failure naming the key, so the
    # record can be completed by hand, than a record that looks fine and
    # quietly breaks the control that reads it.
    labelled = False
    if labels and key:
        try:
            _request(f"/rest/api/3/issue/{urllib.parse.quote(key)}",
                     {"update": {"labels": [{"add": l} for l in labels]}},
                     method="PUT")
            labelled = True
        except jira.JiraUnavailable as exc:
            raise jira.JiraUnavailable(
                f"{key} was raised but its labels could not be set: {exc}\n"
                f"      Add these to {key} by hand: {', '.join(labels)}\n"
                "      Until they are there, reconciliation cannot match this "
                "record to the deployments it authorises, and those "
                "deployments will be reported as unapproved.") from exc

    return {"key": key,
            "created_via": "jsm-request",
            "request_type": form.get("request_type_name"),
            "labels_set": labelled,
            "fields_set": sorted(k for k in values if k != "summary"),
            "fields_unavailable": sorted(unavailable)}


def complete_change(event, context=None):
    """Record what actually happened, after the work.

    A comment rather than a field edit. The plan is what was intended and
    should stay legible as such; the outcome is a separate statement with its
    own timestamp. Overwriting the plan with the result would destroy the one
    comparison an auditor wants to make -- and would also be an edit after
    approval, which design 08 condition 4 treats as invalidating.
    """
    key = event.get("key")
    if not key:
        raise ValueError("complete_change needs the issue key")
    result = event.get("result") or "Completed."
    body = f"*Completed* {_now().isoformat()}\n\n{result}"
    _post(f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment",
          {"body": _adf(body)})
    out = {"record_type": "change-record-completed", "key": key}
    print(json.dumps(out, default=str))
    return out


HANDLERS = {"file": file_change, "complete": complete_change}


def handler(event, context):
    action = event.get("action") or "file"
    fn = HANDLERS.get(action)
    if fn is None:
        raise ValueError(
            f"unknown action '{action}'. Known: {', '.join(sorted(HANDLERS))}")
    return fn(event, context)
