# HIPAA eligibility: question to AWS (E1)

docs/hipaa-eligibility.md §9, E1. Decided by Jamie on 2026-10-05: ask AWS
instead of interpreting the Reference ourselves.

**Status:** drafted 2026-10-05; **not yet sent**. The management account has Basic
Support. That plan has no Support API and cannot open technical cases. The
question goes to AWS through the Compliance contact form
(aws.amazon.com/compliance/contact) or the account team. Record the date sent,
the channel and the reference below when it goes, and file the written answer
beside this note as compliance evidence. Until then, every prefix asked about
stays `pending` in the catalog.

| Sent | Channel | Reference | Answered |
|---|---|---|---|
| | | | |

---

Subject: HIPAA eligibility of IAM service prefixes that belong to listed HIPAA Eligible Services

We operate an AWS Organization under an organization-level Business Associate Addendum accepted through AWS Artifact. In accounts that hold PHI we enforce the HIPAA Eligible Services Reference with a service control policy written in IAM service prefixes.

The Reference (last updated September 3, 2026) says that, unless specifically excluded, generally available features of each listed service are also HIPAA eligible. Several features of listed services have an IAM service prefix of their own, and we cannot tell from the Reference whether they are covered. For each of the following, please confirm in writing whether it is HIPAA eligible under the BAA:

1. synthetics: Amazon CloudWatch Synthetics (listed service: Amazon CloudWatch)
2. application-signals: Amazon CloudWatch Application Signals (Amazon CloudWatch)
3. rum: Amazon CloudWatch RUM (Amazon CloudWatch)
4. scheduler: Amazon EventBridge Scheduler (Amazon EventBridge)
5. pipes: Amazon EventBridge Pipes (Amazon EventBridge)
6. aoss: Amazon OpenSearch Serverless (Amazon OpenSearch Service)
7. s3express: Amazon S3 Express One Zone (Amazon Simple Storage Service)
8. s3tables: Amazon S3 Tables (Amazon Simple Storage Service)

Two related questions:

9. Which IAM service prefixes correspond to "AWS DevOps Agent" and "AWS MCP Server" as listed in the Reference?
10. Does AWS publish an authoritative mapping from the services in the Reference to IAM service prefixes? If so, where?

We will keep the written answer as compliance evidence for our HIPAA controls. Thank you.
