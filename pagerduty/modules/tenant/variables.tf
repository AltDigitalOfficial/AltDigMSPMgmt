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

variable "production_apps" {
  type    = list(string)
  default = []

  description = <<-EOT
    The applications this client runs in PRODUCTION, by app slug. An empty
    string is a valid entry and means a production account with no app
    component -- `altdig-<partner>-<client>-prod` rather than
    `altdig-<partner>-<client>-<app>-prod`.

    design 13 creates a paging service "per application (production only)", so
    this list is the paging surface and nothing else creates one. Non-production
    accounts are deliberately absent: a dev account that pages is a dev account
    whose failures wake somebody, and the rotation reliably learns to ignore
    pages from it -- a habit that does not stay confined to dev.

    An empty list is correct and expected for a client with no production
    account yet. It creates no paging service, which is not an error.
  EOT
}

variable "escalation_policy_id" {
  type        = string
  description = <<-EOT
    The escalation policy these services page through.

    Passed in rather than created here. A tenant with its own escalation policy
    but the SAME schedule behind it looks like separation and provides none --
    the same phone rings either way. When a tenant genuinely needs its own
    rotation, that is a schedule, and it belongs at the root config where the
    coverage verification (B-018) can see it.
  EOT
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
  description = "Role reference for the business service. A person's name here goes stale and is personal data besides."
}
