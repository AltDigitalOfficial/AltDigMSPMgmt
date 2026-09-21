variable "tenant_id" {
  type        = string
  description = <<-EOT
    The registry id (client slug). This is the idempotency key for the whole
    module: every resource name derives from it, so re-rendering the tenant map
    produces the same names and Terraform sees no change.

    Not the client's display name. A display name gets corrected for spelling
    six months in, and every PagerDuty resource would be renamed by an apply
    nobody connected to the edit.
  EOT
}

variable "partner" {
  type        = string
  description = "Partner slug. Appears in descriptions so an incident makes it obvious whose client this is, which matters at 3am."
}

variable "tiers" {
  type = map(object({
    objects = list(string)
  }))
  default = {}

  description = <<-EOT
    Environment tiers this client has, keyed by tier name (dev/test/uat/prod),
    each listing the OBJECTS monitored within it.

    Every tier gets a business service; every object gets a technical service
    linked to it. See main.tf on why those are different things.

    `objects` currently holds app slugs, with the empty string meaning the
    account's single unnamed application. That is the coarsest useful
    granularity and the finest the registry can currently supply -- it carries
    `app` on ACCOUNT records and nothing below it. Finer objects (a database, a
    queue, a specific service) slot into this list without changing any shape
    here, which is why it is a list rather than a single value.
  EOT
}

variable "default_escalation_policy_id" {
  type        = string
  description = <<-EOT
    The escalation policy used by any tier without its own.

    Passed in rather than created here. A tenant with its own policy but the
    SAME schedule behind it looks like separation and provides none -- the same
    phone rings either way. When a tier genuinely needs its own rotation, that
    is a schedule, and it belongs at the root config where the coverage
    verification (B-018) can see it.
  EOT
}

variable "tier_escalation_policy_ids" {
  type        = map(string)
  default     = {}
  description = <<-EOT
    Per-tier escalation policy overrides, keyed by tier name.

    Empty today. Non-production escalation is an open question owned by Jamie
    and Wayne (B-117): dev is the developer's own production, so "does not
    page the on-call rotation" is not the same as "nobody is told", and the
    right answer is probably a different policy rather than no policy.

    The override exists now so that answering it later is a data change here
    rather than a restructuring.
  EOT
}

variable "tier_urgency" {
  type = map(string)
  default = {
    prod = "high"
    uat  = "low"
    test = "low"
    dev  = "low"
  }

  description = <<-EOT
    Incident urgency per tier.

    Production is high; the rest are low PENDING B-117, and low is deliberately
    not the same as silent. A low-urgency PagerDuty incident still notifies
    according to each responder's rules -- it just does not force a page. That
    is the distinction that makes a dev-tier alert reach the developer whose
    working environment it is without waking the production rotation.

    This mirrors the platform's own split, where `AltDigital Platform` and
    `AltDigital Platform - Low Urgency` already separate paging from
    notification rather than separating alerting from nothing.
  EOT

  validation {
    condition     = alltrue([for u in values(var.tier_urgency) : contains(["high", "low"], u)])
    error_message = "urgency must be high or low."
  }
}

variable "acknowledgement_timeout_minutes" {
  type        = number
  default     = 30
  description = "Minutes before an acknowledged-but-unresolved incident re-triggers."
}

variable "auto_resolve_timeout_minutes" {
  type        = number
  default     = 0
  description = "0 disables auto-resolve. An incident that resolves itself because nobody looked is an incident nobody looked at."
}

variable "commercial_point_of_contact" {
  type        = string
  default     = "AltDigital Platform"
  description = "Role reference for the client-conversation business service. A person's name here goes stale and is personal data besides."
}
