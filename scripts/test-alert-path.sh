#!/usr/bin/env bash
#
# Prove that an alarm in one account can actually publish to the alerting topic
# in another.
#
#   scripts/test-alert-path.sh --alarm platform-replication-failed-flow-logs \
#     --alarm-account altdig-security-logarchive
#
# ---------------------------------------------------------------------------
# What this exists to catch
# ---------------------------------------------------------------------------
# Cross-account alarm -> SNS has three separate places to fail, and all three
# fail the same way: silently, with the alarm showing ALARM in the console and
# nothing arriving.
#
#   1. the topic policy does not admit the publisher
#   2. the KMS key policy does not admit the publisher
#   3. the condition key used to scope either of them is never populated for
#      the calling service principal, so it can never match
#
# (3) is not hypothetical here. alerting/10-alert-topic.yaml scopes both
# policies with aws:SourceOrgID. That condition is CONFIRMED to work for
# config.amazonaws.com and delivery.logs.amazonaws.com elsewhere in this
# repository, and is ASSERTED for cloudwatch.amazonaws.com. The same class of
# assumption — using aws:PrincipalOrgID against a service principal, which can
# never match — cost a full day on the CloudTrail key.
#
# So the assertion gets tested rather than believed, and it is tested by
# reading the RECEIVING side. Checking that set-alarm-state succeeded proves
# nothing: it succeeds whether or not the publish that follows is refused.
#
# ---------------------------------------------------------------------------
# Why this is slow
# ---------------------------------------------------------------------------
# SNS publishes its metrics on a five-minute period and they lag. There is no
# faster authoritative signal — CloudWatch does not report alarm action
# failures as a metric, and the delivery failure surfaces only in the topic
# owner's account. Budget ten minutes and run it in the background.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ALARM=""; ALARM_ACCOUNT=""; TOPIC_ACCOUNT="altdig-security-audit"
TOPIC_NAME="platform-alerts"; WAIT_SECONDS=600

usage() {
  cat <<'USAGE'
Usage: test-alert-path.sh --alarm <name> --alarm-account <canonical-alias>
                          [--topic-account <canonical-alias>]
                          [--topic-name <name>] [--wait <seconds>]

  --alarm          CloudWatch alarm to drive into ALARM state.
  --alarm-account  Canonical account holding that alarm.
  --topic-account  Canonical account holding the SNS topic.
                   Default: altdig-security-audit.
  --topic-name     SNS topic name. Default: platform-alerts.
  --wait           Seconds to wait for SNS metrics. Default: 600.

The alarm is returned to OK when the test finishes, including on failure.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alarm)         ALARM="$2"; shift 2 ;;
    --alarm-account) ALARM_ACCOUNT="$2"; shift 2 ;;
    --topic-account) TOPIC_ACCOUNT="$2"; shift 2 ;;
    --topic-name)    TOPIC_NAME="$2"; shift 2 ;;
    --wait)          WAIT_SECONDS="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "${ALARM}" ]]         || { usage; die "--alarm is required."; }
[[ -n "${ALARM_ACCOUNT}" ]] || { usage; die "--alarm-account is required."; }

export AWS_DEFAULT_REGION="${PLATFORM_HOME_REGION}"
require_cli
require_account "${PLATFORM_MGMT_ACCOUNT_ID}"

ALARM_ACCT_ID="$(get_param "/org/account/${ALARM_ACCOUNT}")"
TOPIC_ACCT_ID="$(get_param "/org/account/${TOPIC_ACCOUNT}")"
[[ -n "${ALARM_ACCT_ID}" && "${ALARM_ACCT_ID}" != "None" ]] || die "Unknown account: ${ALARM_ACCOUNT}"
[[ -n "${TOPIC_ACCT_ID}" && "${TOPIC_ACCT_ID}" != "None" ]] || die "Unknown account: ${TOPIC_ACCOUNT}"

# assume <account-id> -> exports AK/SK/ST for that account
assume() {
  local acct="$1" creds
  creds="$(AWS_PROFILE="${PARENT_PROFILE}" aws sts assume-role \
    --role-arn "arn:aws:iam::${acct}:role/OrganizationAccountAccessRole" \
    --role-session-name platform-alert-test \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text 2>/dev/null | no_cr)" || true
  [[ -n "${creds}" ]] || die "Could not assume into ${acct}."
  read -r A S T <<<"${creds}"
  # An EMPTY AWS_PROFILE is not the same as an unset one: the CLI reads it as
  # a profile named "" and fails with "The config profile () could not be
  # found". Unset it.
  unset AWS_PROFILE
  export AWS_ACCESS_KEY_ID="${A}" AWS_SECRET_ACCESS_KEY="${S}" AWS_SESSION_TOKEN="${T}"
}

PARENT_PROFILE="${AWS_PROFILE:-}"

clear_creds() {
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  [[ -n "${PARENT_PROFILE}" ]] && export AWS_PROFILE="${PARENT_PROFILE}"
}

# Sum a topic metric over a window, from the TOPIC owner's account.
topic_metric() {
  local metric="$1" start="$2" v
  v="$(MSYS_NO_PATHCONV=1 aws cloudwatch get-metric-statistics \
        --namespace AWS/SNS --metric-name "${metric}" \
        --dimensions "Name=TopicName,Value=${TOPIC_NAME}" \
        --start-time "${start}" \
        --end-time "$(date -u -d '+5 minutes' +%Y-%m-%dT%H:%M:%SZ)" \
        --period 300 --statistics Sum \
        --query 'sum(Datapoints[].Sum)' --output text 2>/dev/null | no_cr)"
  [[ -z "${v}" || "${v}" == "None" ]] && v=0
  printf '%s' "${v}"
}

restore_alarm() {
  clear_creds
  assume "${ALARM_ACCT_ID}"
  aws cloudwatch set-alarm-state --alarm-name "${ALARM}" \
    --state-value OK --state-reason "test-alert-path.sh complete" >/dev/null 2>&1 || true
  clear_creds
}
trap restore_alarm EXIT

hr
log "Cross-account alert path test"
log "  alarm   : ${ALARM} in ${ALARM_ACCOUNT} (${ALARM_ACCT_ID})"
log "  topic   : ${TOPIC_NAME} in ${TOPIC_ACCOUNT} (${TOPIC_ACCT_ID})"
log "  region  : ${AWS_DEFAULT_REGION}"
hr

START="$(date -u -d '-5 minutes' +%Y-%m-%dT%H:%M:%SZ)"

assume "${TOPIC_ACCT_ID}"
BASE_PUB="$(topic_metric NumberOfMessagesPublished "${START}")"
BASE_FAIL="$(topic_metric NumberOfNotificationsFailed "${START}")"
info "baseline: published=${BASE_PUB} failed=${BASE_FAIL}"

clear_creds
assume "${ALARM_ACCT_ID}"
info "driving ${ALARM} to ALARM"
aws cloudwatch set-alarm-state --alarm-name "${ALARM}" --state-value ALARM \
  --state-reason "Synthetic test from scripts/test-alert-path.sh" >/dev/null \
  || die "set-alarm-state failed. The alarm does not exist in this account."
ok "alarm state set (this proves nothing on its own — see the header)"

clear_creds
assume "${TOPIC_ACCT_ID}"

info "waiting up to ${WAIT_SECONDS}s for SNS metrics"
DEADLINE=$(( $(date +%s) + WAIT_SECONDS ))
PUB="${BASE_PUB}"
while [[ $(date +%s) -lt ${DEADLINE} ]]; do
  PUB="$(topic_metric NumberOfMessagesPublished "${START}")"
  if awk -v a="${PUB}" -v b="${BASE_PUB}" 'BEGIN{exit !(a>b)}'; then break; fi
  printf '  .'
  sleep 30
done
printf '\n'

FAIL="$(topic_metric NumberOfNotificationsFailed "${START}")"

hr
if awk -v a="${PUB}" -v b="${BASE_PUB}" 'BEGIN{exit !(a>b)}'; then
  ok "PUBLISH REACHED THE TOPIC (${BASE_PUB} -> ${PUB})"
  log "    The cross-account topic policy and the KMS key policy both admit"
  log "    cloudwatch.amazonaws.com, and aws:SourceOrgID IS populated for it."
else
  printf '%sFAIL%s  NOTHING REACHED THE TOPIC (still %s)\n' "${C_RED}" "${C_RESET}" "${PUB}"
  log "    The alarm fired and the publish was refused. In order of likelihood:"
  log "      1. aws:SourceOrgID is not populated for cloudwatch.amazonaws.com."
  log "         Replace it with aws:SourceAccount in BOTH the topic policy and"
  log "         the KMS key policy in alerting/10-alert-topic.yaml."
  log "      2. The KMS grant is missing while the topic grant is present —"
  log "         the publish is refused by KMS, not by SNS, and neither says so."
  log "    Metrics lag; re-run with --wait 900 before concluding."
fi

if awk -v a="${FAIL}" -v b="${BASE_FAIL}" 'BEGIN{exit !(a>b)}'; then
  warn "delivery to a subscriber FAILED (${BASE_FAIL} -> ${FAIL})"
  log "    The topic accepted the message and could not deliver it. If the"
  log "    PagerDuty subscription is PendingConfirmation, the routing key is"
  log "    wrong — a bad key returns 200 to the confirmation POST and then"
  log "    discards everything."
fi
hr
