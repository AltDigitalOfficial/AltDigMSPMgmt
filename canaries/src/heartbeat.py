"""CloudWatch Synthetics canary: does the service actually work?

Prompt 4.3, design/06 "Synthetic monitoring". Generic infrastructure alarms
tell you a thing is running. They do not tell you it works. This runs the one
transaction the tenant named in questionnaire 7.1 and asserts on it.

What it deliberately does NOT do
--------------------------------
Evaluate a customer-supplied expression. Questionnaire 7.3 is free text, and a
canary that executed it would be a remote code execution path into a tenant
account reachable through an intake form. The assertion here is two fixed
checks — a status code and a substring — and anything richer takes the Fargate
path with the tenant's own Playwright suite.

The Selenium runtime
--------------------
syn-python-selenium is used for the runtime's reporting, not for a browser.
This is an HTTP check, so it drives urllib directly rather than starting
Chrome: a browser adds ~4s of startup to every run, several hundred MB of
memory, and a second failure mode (the browser itself) that reports as a
service failure. A tenant whose "working" genuinely requires a rendered page
has a user journey, not a heartbeat, and that is the Fargate path.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request

import boto3

try:
    # Present in the Synthetics runtime. Gives the run a named step, which is
    # what produces the SuccessPercent and Duration metrics and the screenshot
    # and HAR capture the design calls out as useful for triage.
    from aws_synthetics.common import synthetics_configuration
    from aws_synthetics.common import synthetics_logger as logger
    from aws_synthetics.selenium import synthetics_webdriver as syn_webdriver
    IN_RUNTIME = True

    # execute_step screenshots the browser at step start, success and failure.
    # There is no browser here — this is an HTTP check — so every run failed
    # with 'NoneType' object has no attribute 'save_screenshot', which names the
    # screenshot and not the missing driver, and looks like a runtime bug rather
    # than a configuration one. Found on the first live run.
    #
    # The cost is that design/06's "screenshots and HAR files captured on
    # failure" does not apply to a heartbeat. There is no rendered page to
    # capture; a tenant whose "working" needs one has a user journey, and that
    # is the Fargate path.
    synthetics_configuration.set_config({
        "screenshot_on_step_start": False,
        "screenshot_on_step_success": False,
        "screenshot_on_step_failure": False,
    })
except ImportError:  # local testing
    import logging as logger
    syn_webdriver = None
    IN_RUNTIME = False

ssm = boto3.client("ssm")
secrets = boto3.client("secretsmanager")
cloudwatch = boto3.client("cloudwatch")

CANARY_NAME = os.environ.get("CANARY_NAME", "")

ENDPOINT = os.environ.get("ENDPOINT", "")
EXPECTED_STATUS = int(os.environ.get("EXPECTED_STATUS", "200"))
BODY_MUST_CONTAIN = os.environ.get("BODY_MUST_CONTAIN", "")
RESPONSE_MS_PARAM = os.environ.get("RESPONSE_MS_PARAM", "")
AUTH_SECRET_ARN = os.environ.get("AUTH_SECRET_ARN", "")

# A hard ceiling on the request itself, separate from the pass condition. See
# the RunConfig comment in 10-canary.yaml: exceeding the pass condition must
# produce a recorded failure with a duration, not a killed run with no data.
REQUEST_TIMEOUT_S = 30


def acceptable_response_ms():
    """Questionnaire 7.4, read from the one place that holds it.

    Read at RUN TIME rather than baked into an environment variable at deploy
    time. The same value is the SLA alarm threshold and the ALB alarm
    threshold; if it is changed, the canary's idea of "acceptable" must change
    in the same moment, not at the next stack update. A stale pass condition
    would report the service as meeting an SLA it no longer has.
    """
    if not RESPONSE_MS_PARAM:
        raise RuntimeError(
            "RESPONSE_MS_PARAM is unset. The canary has no pass condition and "
            "will not guess one: a canary that passes everything is worse than "
            "no canary, because it reports health it never checked.")
    return int(ssm.get_parameter(Name=RESPONSE_MS_PARAM)["Parameter"]["Value"])


def auth_header():
    """Credentials from Secrets Manager, never from the canary configuration.

    Synthetics environment variables are stored and displayed in plaintext, so
    a credential placed in one is readable by anyone who can describe the
    canary. The canary holds the ARN and fetches the value on each run, which
    also means a rotated secret takes effect without redeploying anything.

    Two shapes are accepted, because both turn up in practice:
        {"header": "Authorization", "value": "Bearer ..."}
        {"username": "...", "password": "..."}       -> HTTP basic
    """
    if not AUTH_SECRET_ARN:
        return {}
    raw = secrets.get_secret_value(SecretId=AUTH_SECRET_ARN)["SecretString"]
    data = json.loads(raw)
    if "header" in data and "value" in data:
        return {data["header"]: data["value"]}
    if "username" in data and "password" in data:
        import base64
        token = base64.b64encode(
            f"{data['username']}:{data['password']}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    raise RuntimeError(
        "The auth secret matched neither supported shape. Expected either "
        "header/value or username/password. The secret VALUE is not logged "
        "here and must not be added to this message.")


def publish_response_time(elapsed_ms):
    """Emit ResponseTimeMs. Best-effort: a metric failure must not fail the run.

    The check has already happened by the time this is called. Raising here
    would turn "the service is fine but CloudWatch was briefly unavailable" into
    a canary failure, and the canary failing is the signal that pages someone.
    """
    if not CANARY_NAME:
        return
    try:
        cloudwatch.put_metric_data(
            Namespace="CloudWatchSynthetics",
            MetricData=[{
                "MetricName": "ResponseTimeMs",
                "Dimensions": [{"Name": "CanaryName", "Value": CANARY_NAME}],
                "Value": elapsed_ms,
                "Unit": "Milliseconds",
            }])
    except Exception as exc:
        logger.info(json.dumps({
            "could_not_publish_response_time": f"{type(exc).__name__}: {exc}"}))


def check():
    limit_ms = acceptable_response_ms()
    headers = {"User-Agent": "AltDigital-Platform-Canary/1.0"}
    headers.update(auth_header())

    req = urllib.request.Request(ENDPOINT, headers=headers, method="GET")
    ctx = ssl.create_default_context()

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S,
                                    context=ctx) as resp:
            status = resp.status
            body = resp.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # An HTTP error is a RESULT, not a transport failure — a 503 is exactly
        # the thing this canary exists to catch, so it is measured rather than
        # raised.
        status = exc.code
        body = exc.read(65536).decode("utf-8", errors="replace")
    elapsed_ms = int((time.monotonic() - started) * 1000)

    failures = []
    if status != EXPECTED_STATUS:
        failures.append(f"status {status}, expected {EXPECTED_STATUS}")
    if BODY_MUST_CONTAIN and BODY_MUST_CONTAIN not in body:
        # The body itself is NOT logged. It is tenant application output and may
        # carry anything the application renders for an authenticated user.
        failures.append(
            f"body did not contain the expected marker "
            f"({len(BODY_MUST_CONTAIN)} chars); response was {len(body)} bytes")
    if elapsed_ms > limit_ms:
        # The pass condition from questionnaire 7.4. A service that answers
        # correctly but too slowly has failed — that is the whole point of
        # asking for an acceptable response time rather than inferring one.
        failures.append(f"took {elapsed_ms}ms, acceptable is {limit_ms}ms")

    # The SLA measurement, published as its own metric.
    #
    # NOT the built-in Duration metric, which measures the whole canary RUN:
    # container start, boto3 initialisation, the step, and teardown. Measured on
    # the first live run against a service that answered in 185ms, Duration
    # reported 10,100ms. An SLA alarm on Duration therefore thresholds our own
    # cold start against the tenant's agreed response time and fires constantly
    # — and worse, would have been reported to the tenant as their number.
    #
    # This is the elapsed time of the request itself and nothing else.
    publish_response_time(elapsed_ms)

    logger.info(json.dumps({
        "endpoint": ENDPOINT, "status": status, "elapsed_ms": elapsed_ms,
        "acceptable_ms": limit_ms, "passed": not failures,
    }))

    if failures:
        raise RuntimeError(
            f"Canary failed against {ENDPOINT}: " + "; ".join(failures))
    return elapsed_ms


async def handler(event=None, context=None):
    """Async because execute_step is a coroutine from syn-python-selenium 3.0 on.

    A synchronous handler calling it does not fail in any way that names the
    cause: the coroutine is never awaited, the runtime tries to serialise it
    into the run report, and the canary reports

        Object of type coroutine is not JSON serializable

    which reads like a bug in the canary's own data handling. Found on the first
    live run against syn-python-selenium-12.0.
    """
    if not ENDPOINT:
        raise RuntimeError("ENDPOINT is unset; nothing to check.")
    if IN_RUNTIME:
        # Wrapping in a named step is what emits SuccessPercent and Duration.
        # Without it the canary runs and reports nothing, which looks identical
        # to a canary that is passing.
        return await syn_webdriver.execute_step("heartbeat", check)
    return check()
