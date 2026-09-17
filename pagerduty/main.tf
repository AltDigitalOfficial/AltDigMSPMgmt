# ---------------------------------------------------------------------------
# Responders
# ---------------------------------------------------------------------------
# Looked up, not created. See variables.tf — this configuration must not own
# people, because `terraform destroy` would then delete them.

data "pagerduty_user" "responder" {
  for_each = toset(var.responder_emails)
  email    = each.value
}

# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------
# A schedule with one person in it looks like pointless indirection, and it is
# not. Design doc 13 says the escalation policy references the on-call
# schedule; pointing the policy straight at a user instead means that adding
# the second responder later is a restructure rather than an edit, and it
# happens at the moment someone is trying to hand over a pager.
#
# The layer is a 24/7 rotation because there is currently no rotation. When
# there is, this becomes a real handoff and nothing downstream changes.
#
# DEPRECATED RESOURCE, KEPT DELIBERATELY. The provider wants
# pagerduty_schedulev2, and this will have to move before v1 is removed.
#
# Not moved now because v2 replaces the simple "rotate every N seconds" model
# with calendar events carrying RRULE recurrence, effective_since and explicit
# start and end times. A mis-specified RRULE does not fail — it produces a
# schedule with a GAP, and the gap is discovered when an incident at 3am on a
# Tuesday pages nobody.
#
# That is only safe to write alongside a verification step that queries actual
# on-call coverage across a full week after applying, and that step cannot be
# written or run until PagerDuty credentials exist. Migrating blind, to clear a
# warning, would trade a deprecation notice for a silent coverage hole.
# Tracked as B-018.

resource "pagerduty_schedule" "platform" {
  name      = "AltDigital Platform On-Call"
  time_zone = "America/Chicago"

  layer {
    name                         = "Primary"
    start                        = "2026-09-17T00:00:00-05:00"
    rotation_virtual_start       = "2026-09-17T00:00:00-05:00"
    rotation_turn_length_seconds = 604800 # one week

    # Ordered by the variable, not by the set, so escalation order is the
    # order written rather than alphabetical by email.
    users = [for e in var.responder_emails : data.pagerduty_user.responder[e].id]
  }

  teams = []
}

# ---------------------------------------------------------------------------
# Escalation policy
# ---------------------------------------------------------------------------

resource "pagerduty_escalation_policy" "platform" {
  name      = "AltDigital Platform Escalation"
  num_loops = 2

  rule {
    escalation_delay_in_minutes = var.escalation_timeout_minutes

    target {
      type = "schedule_reference"
      id   = pagerduty_schedule.platform.id
    }
  }

  # Second rule pages every responder directly, bypassing the schedule. This
  # is the "the schedule is wrong, or the person on it is unreachable" case,
  # which is precisely when an evidence platform must not fail quietly. With a
  # single responder it is the same phone twice, fifteen minutes apart — which
  # is still the correct behaviour, and becomes materially useful the moment a
  # second name exists.
  dynamic "rule" {
    for_each = length(var.responder_emails) > 0 ? [1] : []
    content {
      escalation_delay_in_minutes = var.escalation_timeout_minutes

      dynamic "target" {
        for_each = var.responder_emails
        content {
          type = "user_reference"
          id   = data.pagerduty_user.responder[target.value].id
        }
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Platform service
# ---------------------------------------------------------------------------

resource "pagerduty_service" "platform" {
  name                    = var.platform_service_name
  description             = "Alarms raised by the AltDigital Managed Platform itself, not by a tenant application."
  escalation_policy       = pagerduty_escalation_policy.platform.id
  alert_creation          = "create_alerts_and_incidents"
  auto_resolve_timeout    = var.auto_resolve_timeout_minutes == 0 ? "null" : tostring(var.auto_resolve_timeout_minutes * 60)
  acknowledgement_timeout = tostring(var.acknowledgement_timeout_minutes * 60)

  incident_urgency_rule {
    type    = "constant"
    urgency = "high"
  }
}

# ---------------------------------------------------------------------------
# Alert grouping
# ---------------------------------------------------------------------------
# A separate resource rather than an alert_grouping_parameters block on the
# service. The inline block is deprecated and this is the provider's
# replacement — note that the direction inverts: the SETTING names the
# services, the service does not name the setting.
#
# "intelligent" rather than "time". Time-based grouping folds an unrelated
# second failure into the first incident, and the second failure is then
# acknowledged by someone who only read the first — which on this service could
# mean acknowledging away the alarm saying the evidence archive has stopped
# replicating.
#
# The config block is REQUIRED even though the documentation reads as though it
# is optional. Omitting it does not produce a validation error — provider
# v3.36.0 panics with a nil pointer dereference and terraform reports "Plugin
# did not respond". Worth knowing before spending time looking for the mistake
# in this file.
#
# `time_window`, not `timeout`. The provider rejects `timeout` here with
# "'timeout' is only applicable when type is time", so the two names are not
# interchangeable: `timeout` is how long a time-grouped incident stays open,
# `time_window` is the window intelligent grouping considers. 900 seconds
# matches the CloudWatch alarm period plus SNS lag, so a flapping alarm
# produces one incident rather than a page per cycle.

resource "pagerduty_alert_grouping_setting" "platform" {
  count = var.alert_grouping_type == "none" ? 0 : 1

  name     = "AltDigital Platform Grouping"
  type     = var.alert_grouping_type
  services = [pagerduty_service.platform.id]

  # Each grouping type takes a DIFFERENT set of config fields, and supplying
  # one that does not belong to the active type is rejected rather than
  # ignored. Hence the nulls: every field is present in the block and only the
  # relevant ones carry a value.
  #
  #   intelligent    time_window            (403 on non-AIOps accounts)
  #   content_based  aggregate, fields, time_window
  #   time           timeout
  config {
    # Group on the alert summary, which for the CloudWatch vendor integration
    # IS the alarm name. So repeats of one alarm collapse into one incident,
    # and two different alarms stay two incidents — which is the behaviour
    # that matters on a service carrying "the evidence archive has stopped
    # replicating".
    aggregate = var.alert_grouping_type == "content_based" ? "all" : null
    fields    = var.alert_grouping_type == "content_based" ? ["summary"] : null

    time_window = var.alert_grouping_type == "time" ? null : 900
    timeout     = var.alert_grouping_type == "time" ? 900 : null
  }
}

# ---------------------------------------------------------------------------
# CloudWatch integration
# ---------------------------------------------------------------------------
# The vendor lookup matters. A generic Events API v2 integration would accept
# an SNS POST and render it as an unparsed JSON blob; the CloudWatch vendor
# integration understands the SNS envelope and produces an incident whose title
# is the alarm name and whose body is the alarm description.
#
# That is why the alarm descriptions in security/10-log-archive.yaml and
# alerting/10-alert-topic.yaml are written as instructions to a woken engineer
# rather than as labels. They are the page.

data "pagerduty_vendor" "cloudwatch" {
  name = "Amazon CloudWatch"
}

resource "pagerduty_service_integration" "cloudwatch" {
  name    = "AWS CloudWatch"
  service = pagerduty_service.platform.id
  vendor  = data.pagerduty_vendor.cloudwatch.id
}
