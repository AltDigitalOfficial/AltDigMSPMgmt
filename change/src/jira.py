"""Minimal Jira Cloud client. Shared by reconciliation (8.2) and quorum (8.3).

design/08-change-and-release.md is explicit about what Jira is and is not:

    Truveon is the system of record for audit. Jira is the workflow layer
    feeding it, not the record.

    This distinction matters because Jira history can be altered by a project
    admin, and auditors will ask about that.

So nothing here treats a Jira response as authoritative. It is evidence of what
the workflow system SAYS, captured with a timestamp, and the quorum pipeline
snapshots it immutably at the moment of execution precisely because it can
change afterwards.

Read-only by construction
-------------------------
There is no create, update or transition method in this module, and that is a
control rather than an omission. A credential able to edit issues could be used
to manufacture the approval it then verifies, which would make the whole quorum
check circular. The service account should be scoped read-only in Jira as well;
this is the second half of that, so a mis-scoped account still cannot write
through this code path.

urllib, not the atlassian SDK
-----------------------------
Three endpoints are needed. A dependency would mean a Lambda layer or a vendored
package, both of which have to be rebuilt and re-published when the SDK moves,
for no gain over forty lines of urllib. The same reasoning alerting/src/router.py
applies to the PagerDuty Events API.
"""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

secrets = boto3.client("secretsmanager")

JIRA_SECRET_NAME = os.environ.get("JIRA_SECRET_NAME",
                                  "platform/jira/service-account")
JIRA_SITE = os.environ.get("JIRA_SITE", "")
TIMEOUT = int(os.environ.get("JIRA_TIMEOUT_SECONDS", "20"))

_AUTH = {}


class JiraUnavailable(Exception):
    """Jira could not be reached or could not be read.

    A DISTINCT type, and the distinction is the whole point. Everything that
    consumes this module has to tell "Jira says there is no approved change"
    apart from "nobody could ask Jira", because the first is a finding about a
    deployment and the second is a finding about the platform. Collapsing them
    is B-054 -- a denied lookup reported as a negative result -- and here it
    would mean every deployment in the estate being reported as unapproved the
    moment a token expires.
    """


def _site():
    # Trailing slash tolerated. config/platform.env asks for it without one,
    # and a value pasted from a browser address bar will have it; the
    # difference is a double slash in every request path, which Atlassian
    # happens to accept and which would look like a client bug the first time
    # it did not.
    return (JIRA_SITE or "").rstrip("/")


def _auth_header():
    """Basic auth from the stored service account.

    Cached for the life of the execution environment. Rotation takes effect on
    the next cold start, which is the right trade for a credential read on
    every reconciliation run.
    """
    if "header" not in _AUTH:
        try:
            raw = secrets.get_secret_value(
                SecretId=JIRA_SECRET_NAME)["SecretString"]
        except Exception as exc:                   # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            raise JiraUnavailable(
                f"could not read {JIRA_SECRET_NAME}: {code or type(exc).__name__}. "
                "The Jira credential is missing from this account. Run "
                "scripts/set-jira-token.sh.") from exc
        try:
            creds = json.loads(raw)
            pair = f"{creds['email']}:{creds['api_token']}"
        except (ValueError, KeyError) as exc:
            raise JiraUnavailable(
                "the Jira secret is not the expected JSON "
                '{"email": ..., "api_token": ...}') from exc
        _AUTH["header"] = "Basic " + base64.b64encode(
            pair.encode()).decode()
    return _AUTH["header"]


def _get(path, params=None, _retried=False):
    """One GET. Re-reads the credential once on a 401.

    The retry exists because the cached auth header outlives a rotation. A
    Lambda execution environment stays warm for a long time, and this function
    is invoked every six hours; without this, rotating a bad credential would
    appear not to work until the container happened to recycle.

    That is not hypothetical — it cost a diagnostic cycle on 2026-09-19. The
    secret was rotated at 20:34, the container had cached the previous value at
    20:09, and the retry after rotation failed with the OLD credential. The
    symptom is indistinguishable from "the new token is also wrong", which is
    the worst possible failure mode for a credential fix: it makes the correct
    action look ineffective.

    Once only. A genuinely bad credential should fail, not loop.
    """
    if not _site():
        raise JiraUnavailable(
            "JIRA_SITE is unset. Nothing can be asked of Jira, so no change "
            "record can be confirmed or ruled out.")
    url = f"{_site()}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": _auth_header(),
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not _retried:
            # The cached header may predate a rotation. Drop it, read the
            # secret again, and try once more before concluding anything.
            _AUTH.clear()
            return _get(path, params, _retried=True)
        body = ""
        try:
            body = exc.read().decode("utf-8")[:400]
        except Exception:                          # noqa: BLE001
            pass
        # 401 and 403 are the interesting ones and are NOT "no results".
        #
        # Jira answers a bad credential with "Client must be authenticated to
        # access this resource", which is true and useless — it does not
        # distinguish a wrong token from a wrong identity from the wrong auth
        # SCHEME. The causes are few and worth naming here, because the
        # alternative is somebody regenerating a perfectly good token.
        hint = ""
        if exc.code == 401:
            hint = (
                "\n      A 401 from Jira Cloud is almost always one of:"
                "\n      (1) the email is not the Atlassian account's PRIMARY "
                "address. A mail alias or distribution address is not an "
                "Atlassian identity — the account is known by the address it "
                "was registered with, and Basic auth must use that one."
                "\n      (2) the token is a SCOPED api token. Scoped tokens "
                "authenticate as Bearer, not Basic, and will 401 against this "
                "client. Create a classic unscoped API token instead."
                "\n      (3) the token was revoked, expired, or belongs to a "
                "different Atlassian site."
                "\n      Re-run scripts/set-jira-token.sh to correct it; the "
                "previous version is retained by Secrets Manager.")
        elif exc.code == 403:
            hint = (
                "\n      403 means the credential is VALID but the account "
                "lacks permission. Check it has product access to Jira and "
                "browse permission on the project.")
        raise JiraUnavailable(
            f"Jira returned HTTP {exc.code} for {path}: {body}{hint}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JiraUnavailable(f"could not reach Jira: {exc}") from exc


# ---------------------------------------------------------------------------
# The three things the platform asks Jira
# ---------------------------------------------------------------------------


def project_issue_types(project_key):
    """Issue types that actually exist in a project.

    Called before any search, and the reason is a specific failure: if
    PLATFORM_JIRA_CHANGE_ISSUETYPE names a type the project does not have, a
    JQL search for it returns zero issues, and an engine that trusted that
    would report EVERY deployment as having no change record. A configuration
    mistake would read as an estate-wide compliance failure.

    open-items.md B9 -- the golden Jira scheme -- is what settles which type is
    correct. Until it does, this check is what stops a wrong guess looking like
    a finding.
    """
    data = _get(f"/rest/api/3/project/{urllib.parse.quote(project_key)}")
    return [t.get("name") for t in (data.get("issueTypes") or [])]


def service_desks():
    """JSM service desks on the site, with their project keys.

    A request type is a JSM concept layered on top of an issue type: the
    portal shows "Request a change", the issue underneath has issue type
    "[System] Change". Reconciliation searches by ISSUE TYPE in JQL, so the
    issue type is what the configuration needs -- but the request type is what
    somebody sees in the Jira UI, and being unable to map between them is how
    an hour goes missing.
    """
    data = _get("/rest/servicedeskapi/servicedesk", {"limit": 100})
    return [{"id": d.get("id"), "project_key": d.get("projectKey"),
             "project_name": d.get("projectName")}
            for d in (data.get("values") or [])]


def request_types(service_desk_id):
    """Request types in a service desk, each with the issue type behind it."""
    data = _get(
        f"/rest/servicedeskapi/servicedesk/{urllib.parse.quote(str(service_desk_id))}"
        "/requesttype", {"limit": 100})
    # groupIds is a list of STRING ids, not objects. Treating it as objects
    # threw "'str' object has no attribute 'get'" on the first live call --
    # a reminder that this client is written against documentation and every
    # new endpoint needs one real response before it is trusted. B-068.
    return [{"id": r.get("id"), "name": r.get("name"),
             "issue_type_id": r.get("issueTypeId"),
             "group_ids": [str(g) for g in (r.get("groupIds") or [])]}
            for r in (data.get("values") or [])]


def request_type_fields(service_desk_id, request_type_id):
    """The fields a JSM request FORM accepts, which is not the create screen.

    A request type exposes its own subset of fields, and
    /rest/servicedeskapi/request rejects anything outside it. That set is
    narrower than createmeta's: createmeta describes what the ISSUE type can
    carry, this describes what the REQUEST form will take.

    Asked rather than assumed, for the same reason as createmeta -- the
    rejection names a custom field id and not a label, and a field silently
    absent from a form is indistinguishable from one the caller got wrong.
    """
    data = _get(
        f"/rest/servicedeskapi/servicedesk/{urllib.parse.quote(str(service_desk_id))}"
        f"/requesttype/{urllib.parse.quote(str(request_type_id))}/field")
    out = []
    for f in (data.get("requestTypeFields") or []):
        out.append({"id": f.get("fieldId"), "name": f.get("name"),
                    "required": bool(f.get("required")),
                    "type": ((f.get("jiraSchema") or {}).get("type")),
                    "custom": ((f.get("jiraSchema") or {}).get("custom"))})
    return {"fields": out,
            # JSM can forbid raising a request on behalf of somebody else, and
            # the platform's credential is not the requester in the human
            # sense. Worth knowing before a 403 says so.
            "can_raise_on_behalf_of":
                bool(data.get("canRaiseOnBehalfOf")),
            "can_add_request_participants":
                bool(data.get("canAddRequestParticipants"))}


def project_issue_type_map(project_key):
    """Issue type id -> name, so a request type can be resolved to one."""
    data = _get(f"/rest/api/3/project/{urllib.parse.quote(project_key)}")
    return {t.get("id"): t.get("name") for t in (data.get("issueTypes") or [])}


def field_requirements(project_key, issue_type_id):
    """Fields available and required when creating this issue type.

    Named `field_requirements` rather than `create_fields` deliberately. The
    publish guard rejects anything matching "def create_" as write-shaped and
    refused to package this -- correctly, because the name read like a
    constructor for a thing it only describes. The guard stayed; the name
    changed.

    A GET, so this stays inside the read-only boundary. Knowing what a change
    record REQUIRES is not the same as being able to file one, and asking is
    better than guessing: JSM projects carry different field configurations,
    and a required field nobody knew about fails at submission with a message
    naming a custom field id rather than a label anyone recognises.
    """
    data = _get(
        f"/rest/api/3/issue/createmeta/{urllib.parse.quote(project_key)}"
        f"/issuetypes/{urllib.parse.quote(str(issue_type_id))}",
        {"maxResults": 200})
    out = []
    for f in (data.get("fields") or data.get("values") or []):
        allowed = f.get("allowedValues") or []
        out.append({
            "id": f.get("fieldId") or f.get("key"),
            "name": f.get("name"),
            "required": bool(f.get("required")),
            "type": ((f.get("schema") or {}).get("type")),
            # The CUSTOM type, not just "string".
            #
            # `schema.type` reports both a plain text field and a rich text one
            # as "string", and the two take different payloads: rich text needs
            # ADF and rejects a string with HTTP 400. The custom type is what
            # tells them apart, so anything filing an issue can decide from the
            # metadata rather than from a hand-maintained list of field names
            # that goes stale the moment somebody adds a field -- which is what
            # happened within an hour of the first record being filed.
            "custom": ((f.get("schema") or {}).get("custom")),
            # The VALUES, not just whether there are any. A dropdown field
            # rejects anything outside its option set, and the rejection names
            # the custom field id rather than the label -- so knowing the
            # options up front is the difference between filing a record and
            # guessing at one.
            "allowed_values": [
                (a.get("value") or a.get("name") or a.get("label"))
                for a in allowed][:25],
        })
    return out


def site_fields(name_contains=""):
    """Every field defined on the SITE, optionally filtered by name.

    Distinguishes two things that look identical from createmeta: a field that
    does not exist, and a field that exists but is not on the create screen.
    Only the second is a screen configuration problem, and only the first means
    the name is wrong -- but createmeta reports both as simply absent.

    Added after "Business Test Plan" was created in Jira and did not appear in
    the change record's create metadata. Guessing which of the two it was would
    have sent somebody to the wrong configuration page.
    """
    data = _get("/rest/api/3/field")
    want = (name_contains or "").lower()
    out = []
    for f in (data if isinstance(data, list) else []):
        if want and want not in (f.get("name") or "").lower():
            continue
        out.append({"id": f.get("id"), "name": f.get("name"),
                    "custom": ((f.get("schema") or {}).get("custom")),
                    "type": ((f.get("schema") or {}).get("type"))})
    return out


def search(jql, fields=None, max_results=100):
    """JQL search. Returns the raw issue list.

    Paginated deliberately conservatively: a reconciliation window is hours,
    not months, so a run that needs more than a few hundred issues is a signal
    that the window or the JQL is wrong rather than a reason to page through
    thousands.
    """
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ",".join(fields or ["summary", "status", "issuetype",
                                      "created", "updated", "reporter",
                                      "description", "labels"]),
    }
    data = _get("/rest/api/3/search/jql", params)
    return data.get("issues") or []


def approvals(issue_key):
    """JSM approval records for an issue. The PRIMARY evidence for quorum.

    Not the status field, and design 08 is emphatic about why:

        The pipeline validates rather than trusting the ticket's status field
        alone. Jira permissions are easy to get subtly wrong.

    A status of "Approved" is a workflow position. It says a transition
    happened; it does not say who approved, whether they were authorised,
    whether there were two of them, or whether one of them was the person who
    asked. A project admin can move an issue into Approved directly, and a
    post-function or automation rule can do it with nobody's consent at all.

    The approval records carry the decision, the decider's accountId and the
    time. That is what the five conditions are actually checkable against.

    Returns the raw list. The caller decides what it means, because "approved"
    here is a JSM state machine's word and the quorum rules are AltDigital's.
    """
    data = _get(
        f"/rest/servicedeskapi/request/{urllib.parse.quote(issue_key)}"
        "/approval", {"limit": 50})
    return data.get("values") or []


def issue_state(issue_key):
    """Status, reporter and timing for one issue. Read-only.

    The quorum verifier needs all three and none of them are the approval:
    the REPORTER is condition 2's requester, `created` is condition 3's
    request time, and the status says whether the issue has even reached the
    stage where an approval could exist.

    That last one matters more than it sounds. An approval record is created
    when the workflow enters its approval status, so a request sitting in the
    status before it has zero approvals -- identical, from the API, to a
    request whose approvers have all declined to act. Without the status,
    "count: 0" cannot be told apart from "refused".
    """
    data = _get(f"/rest/api/3/issue/{urllib.parse.quote(issue_key)}",
                {"fields": "status,reporter,created,updated,summary"})
    f = data.get("fields") or {}
    status = f.get("status") or {}
    return {
        "key": data.get("key"),
        "summary": f.get("summary"),
        "status": status.get("name"),
        "status_category": ((status.get("statusCategory") or {})
                            .get("name")),
        # accountId, not a name or an address. It is the identifier the
        # approval records use, and it is what condition 2 compares.
        "reporter_account_id": ((f.get("reporter") or {}).get("accountId")),
        "created": f.get("created"),
        "updated": f.get("updated"),
    }


def issue_with_changelog(issue_key):
    """One issue plus its full history.

    The changelog is what makes design 08's condition 4 checkable -- "the issue
    has not been reopened or edited since approval". The status field alone
    cannot answer it: an issue can be approved, edited, and still show
    "Approved", which is exactly the case design 08 warns about when it says
    Jira history can be altered.
    """
    return _get(f"/rest/api/3/issue/{urllib.parse.quote(issue_key)}",
                {"expand": "changelog"})


def health():
    """Can we talk to Jira at all, and as whom.

    Used by reconciliation before it concludes anything, and by
    scripts/jira-check.sh. Returns (ok, detail) rather than raising, because
    the caller's next move is to record the answer either way.
    """
    try:
        me = _get("/rest/api/3/myself")
        return True, {
            "account_id": me.get("accountId"),
            "display_name": me.get("displayName"),
            "site": _site(),
        }
    except JiraUnavailable as exc:
        return False, {"error": str(exc), "site": _site()}
