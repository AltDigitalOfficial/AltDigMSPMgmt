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

  # Group alerts by the alarm that raised them rather than by time. Time-based
  # grouping folds an unrelated second failure into the first incident, and the
  # second failure is then acknowledged without anyone having looked at it.
  alert_grouping_parameters {
    type = "intelligent"
  }

  incident_urgency_rule {
    type    = "constant"
    urgency = "high"
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
# That is why the alarm descriptions in security/10-log-archive.yaml are
# written as instructions to a woken engineer rather than as labels. They are
# the page.

data "pagerduty_vendor" "cloudwatch" {
  name = "Amazon CloudWatch"
}

resource "pagerduty_service_integration" "cloudwatch" {
  name    = "AWS CloudWatch"
  service = pagerduty_service.platform.id
  vendor  = data.pagerduty_vendor.cloudwatch.id
}
