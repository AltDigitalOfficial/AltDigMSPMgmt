variable "responder_emails" {
  type = list(string)

  description = <<-EOT
    PagerDuty user emails, in escalation order, forming the on-call schedule.

    These are looked up rather than created: the users must already exist in
    the PagerDuty account. Creating them here would mean this configuration
    owns people, and destroying it would delete them.

    Direct email addresses are personal data under the repository rules, so
    this is supplied through a gitignored terraform.tfvars rather than a
    default. See CLAUDE.md, "No personal data in the repository".
  EOT

  validation {
    condition     = length(var.responder_emails) > 0
    error_message = "At least one responder is required; an escalation policy with no target pages nobody."
  }
}

variable "escalation_timeout_minutes" {
  type        = number
  default     = 15
  description = <<-EOT
    Minutes before an unacknowledged incident escalates to the next rule.

    Fifteen is a deliberate compromise: long enough not to page a second
    person because the first was driving, short enough that an overnight
    incident does not sit for an hour. Revisit when there is a rotation
    rather than an individual.
  EOT
}

variable "auto_resolve_timeout_minutes" {
  type        = number
  default     = 0
  description = <<-EOT
    Minutes before PagerDuty auto-resolves an untouched incident. Zero
    disables it.

    Disabled on purpose. Auto-resolve is for services that emit an alert per
    occurrence and never send a resolve. CloudWatch sends a real OK
    transition, so an incident that is still open is still true — and a
    replication failure or a sensitive-data finding that quietly closed
    itself overnight is the worst possible outcome for an evidence platform.
  EOT
}

variable "acknowledgement_timeout_minutes" {
  type        = number
  default     = 30
  description = "Minutes before an acknowledged-but-unresolved incident re-triggers."
}

variable "platform_service_name" {
  type        = string
  default     = "AltDigital Platform"
  description = <<-EOT
    The service carrying the PLATFORM's own alarms — log archive replication,
    data protection findings, guardrail drift.

    Distinct from the per-application services design doc 13 creates during
    tenant onboarding. A tenant's application going down is that tenant's
    incident; the evidence archive failing to replicate is AltDigital's, and
    routing them to one service means the noisiest tenant buries the alarm
    that says the platform can no longer prove anything.
  EOT
}

variable "alert_grouping_type" {
  type    = string
  default = "intelligent"

  description = <<-EOT
    How PagerDuty groups alerts on the platform service, or "none" to disable.

    THIS IS PLAN-TIER DEPENDENT, and that is the only reason it is a variable.
    Intelligent Alert Grouping is a Business / AIOps feature. On a lower plan
    the apply fails at this resource with a permissions or feature error rather
    than a configuration error, which reads like a mistake in the Terraform and
    is not one.

      intelligent   — Business / AIOps. Groups on observed alert behaviour.
      content_based — Professional and up. Groups on matching fields.
      time          — Professional and up. Groups anything within a window.
      none          — creates no grouping resource at all.

    "time" is the one to avoid on this service if it can be helped: it folds an
    unrelated second failure into the first incident, and the second is then
    acknowledged by someone who only read the first. On a service that carries
    "the evidence archive has stopped replicating", that matters.
  EOT

  validation {
    condition     = contains(["intelligent", "content_based", "time", "none"], var.alert_grouping_type)
    error_message = "alert_grouping_type must be intelligent, content_based, time, or none."
  }
}
