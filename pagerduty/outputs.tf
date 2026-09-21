output "platform_service_id" {
  description = "PagerDuty service carrying platform alarms"
  value       = pagerduty_service.platform.id
}

output "escalation_policy_id" {
  description = "Escalation policy the platform service pages through"
  value       = pagerduty_escalation_policy.platform.id
}

output "schedule_id" {
  description = "On-call schedule referenced by the escalation policy"
  value       = pagerduty_schedule.platform.id
}

output "cloudwatch_integration_key" {
  description = <<-EOT
    Routing key for the CloudWatch integration. This is a credential: anyone
    holding it can raise an incident on this service.

    Consumed by scripts/sync-pagerduty-secrets.sh, which writes it to AWS
    Secrets Manager. CloudFormation then reads it from there with a dynamic
    reference, so the key is never a stack parameter and never appears in a
    changeset.
  EOT
  value       = pagerduty_service_integration.cloudwatch.integration_key
  sensitive   = true
}

output "cloudwatch_integration_url" {
  description = "Endpoint an SNS HTTPS subscription posts to."
  value       = "https://events.pagerduty.com/integration/${pagerduty_service_integration.cloudwatch.integration_key}/enqueue"
  sensitive   = true
}

output "low_urgency_service_id" {
  description = "PagerDuty service carrying dev and test alarms at low urgency"
  value       = pagerduty_service.platform_low.id
}

output "cloudwatch_low_integration_key" {
  description = <<-EOT
    Routing key for the low-urgency CloudWatch integration. A credential, same
    as the production one — written to Secrets Manager by
    scripts/sync-pagerduty-secrets.sh under a separate secret.
  EOT
  value       = pagerduty_service_integration.cloudwatch_low.integration_key
  sensitive   = true
}

output "events_v2_routing_key" {
  description = <<-EOT
    Events API v2 routing key for the high-urgency service. The router POSTs to
    https://events.pagerduty.com/v2/enqueue with this as routing_key, which is
    what allows a full dossier in custom_details and a dedup_key that merges the
    raw alarm with the dossier that follows it.

    A credential. Synced to Secrets Manager by
    scripts/sync-pagerduty-secrets.sh and read from there, never a stack
    parameter.
  EOT
  value       = pagerduty_service_integration.events_v2.integration_key
  sensitive   = true
}

output "events_v2_routing_key_low" {
  description = "Events API v2 routing key for the dev/test low-urgency service."
  value       = pagerduty_service_integration.events_v2_low.integration_key
  sensitive   = true
}

output "commercial_routing_key" {
  description = "Events API v2 routing key for the client-conversation class. Read by commercial/src/route.py via Secrets Manager, never hardcoded."
  value       = pagerduty_service_integration.commercial_events_v2.integration_key
  sensitive   = true
}

output "commercial_business_service_id" {
  description = "What the commercial team subscribes to."
  value       = pagerduty_business_service.commercial.id
}

# ---------------------------------------------------------------------------
# Per-tenant outputs, consumed by sync-pagerduty-secrets.sh
# ---------------------------------------------------------------------------
# One map rather than one output per tenant, so adding a tenant changes data
# and never changes this file.
#
# `sensitive` on the whole map because routing keys are inside it. That makes
# the map unprintable as a unit, which is intentional: the sync script reads it
# with `terraform output -json` and never displays it, and a person running
# `terraform output` casually gets a redaction rather than a screenful of
# credentials.

output "tenants" {
  description = "Per-client business service id and per-app production services with their routing keys. Read by scripts/sync-pagerduty-tenants.sh."
  sensitive   = true
  value = {
    for id, m in module.tenant : id => {
      business_service_id = m.business_service_id
      services            = m.services
      # Carried so the consumer can tell "no production application" from
      # "the apply did not create one". See modules/tenant/outputs.tf.
      production_app_count = m.production_app_count
    }
  }
}

output "tenant_count" {
  description = "How many clients this configuration manages. Unredacted, so a sync run can report what it examined rather than only what it found."
  value       = length(module.tenant)
}
