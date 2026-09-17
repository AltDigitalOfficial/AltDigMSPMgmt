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
