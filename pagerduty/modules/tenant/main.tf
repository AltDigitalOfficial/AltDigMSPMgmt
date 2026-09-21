# One client's PagerDuty footprint. design 13, "What gets created where".
#
# ---------------------------------------------------------------------------
# Two different granularities, and they are not the same
# ---------------------------------------------------------------------------
# design 13 lists "Service per application (production only)" and "Business
# service for the client-conversation event class" in the same list, which
# makes them look like the same shape. They are not:
#
#   paging service    one per PRODUCTION APPLICATION. A client with three apps
#                     in production has three, because an incident on one is
#                     not an incident on the others and folding them together
#                     means acknowledging away a second outage while reading
#                     about the first.
#   business service  one per CLIENT. design 12's client-conversation class is
#                     commercial, not operational -- the partner wants to know
#                     their client raised a conversation, and which app it
#                     concerned is a detail inside the conversation rather than
#                     a reason to subscribe separately.
#
# Treating both as per-tenant would under-create paging services; treating both
# as per-application would spray a partner's subscription list with one entry
# per app.
#
# ---------------------------------------------------------------------------
# Business service versus technical service
# ---------------------------------------------------------------------------
#   technical service   INGESTS events and pages. Has an integration.
#   business service    is SUBSCRIBED to and notifies. Ingests nothing.
#
# Getting this backwards is not a naming mistake. A business service cannot
# receive events at all -- there is no integration to attach -- so routing
# alarms at one produces events that vanish silently, with no error anywhere.

terraform {
  required_providers {
    pagerduty = {
      source  = "PagerDuty/pagerduty"
      version = "~> 3.0"
    }
  }
}

locals {
  # Keyed by app slug. The empty string is a real key and means "the production
  # account with no app component", which is the common case for a client
  # running one thing.
  apps = { for app in var.production_apps : app => app }

  # Written into every description rather than living only in Terraform state.
  # Somebody looking at this service in the PagerDuty console at 3am should be
  # able to see that it is managed here, and whose client it is, without
  # going to find out.
  provenance = "Managed by AltDigital platform Terraform (pagerduty/modules/tenant). Client ${var.tenant_id}, partner ${var.partner}. Console edits are drift and are reported."
}

# ---------------------------------------------------------------------------
# Paging services -- production applications only
# ---------------------------------------------------------------------------

resource "pagerduty_service" "app" {
  for_each = local.apps

  name                    = each.key == "" ? "tenant-${var.tenant_id}-prod" : "tenant-${var.tenant_id}-${each.key}-prod"
  description             = "Production alarms for ${var.tenant_id}${each.key == "" ? "" : "/${each.key}"}. ${local.provenance}"
  escalation_policy       = var.escalation_policy_id
  alert_creation          = "create_alerts_and_incidents"
  auto_resolve_timeout    = var.auto_resolve_timeout_minutes == 0 ? "null" : tostring(var.auto_resolve_timeout_minutes * 60)
  acknowledgement_timeout = tostring(var.acknowledgement_timeout_minutes * 60)

  incident_urgency_rule {
    type    = "constant"
    urgency = "high"
  }
}

# ---------------------------------------------------------------------------
# CloudWatch integration -- the routing key the alarm layer uses
# ---------------------------------------------------------------------------
# The vendor integration, not a generic Events API v2 one, for the same reason
# the platform service uses it: the vendor integration understands the SNS
# envelope and produces an incident titled with the alarm name. A generic
# integration renders the whole SNS payload as an unparsed blob, which is what
# a woken engineer would then be reading.

data "pagerduty_vendor" "cloudwatch" {
  name = "Amazon CloudWatch"
}

resource "pagerduty_service_integration" "cloudwatch" {
  for_each = local.apps

  name    = "AWS CloudWatch"
  service = pagerduty_service.app[each.key].id
  vendor  = data.pagerduty_vendor.cloudwatch.id
}

# ---------------------------------------------------------------------------
# The business service -- one per client, regardless of production
# ---------------------------------------------------------------------------
# Deliberately NOT gated on having a production application.
#
# A client running only a dev environment can still raise a conversation, and
# the partner still wants to know. Nothing here pages, so the argument for
# suppressing non-production -- that it trains the rotation to ignore alerts --
# does not apply. There is no rotation involved.

resource "pagerduty_business_service" "client" {
  name             = "tenant-${var.tenant_id}-conversations"
  description      = "Client-conversation event class for ${var.tenant_id} (design 12). Subscribed to, never paged from. ${local.provenance}"
  point_of_contact = var.commercial_point_of_contact
}
