# Developer Guide — Working in an AltDigital Member Account

For the people who build and oversee an application hosted on the AltDigital
Managed Platform: the development manager and the developers. Examples use
Arc8 (partner `oeight`, application code `arc8`); for another tenant,
substitute its slugs.

Four things are covered here:

1. [Signing in](#1-signing-in) — console and CLI
2. [Tokens and secrets](#2-tokens-and-secrets) — AWS credentials on your
   machine, and the API keys and passwords your application needs
3. [Deploying code](#3-deploying-code) — directly in dev, through the pipeline
   everywhere else
4. [Guardrails you will hit](#4-guardrails-you-will-hit) — what is denied, why,
   and what to do instead

---

## The short version

- **Sign in through `https://altdigital.awsapps.com/start`.** There are no IAM
  users and no access keys, and you cannot create either.
- **Dev is yours.** Build, break and rebuild it directly, within the guardrails.
- **Test and prod change only through your GitHub Actions pipeline**, which
  assumes the `TenantAppDeployer-arc8` role over OIDC. The pipeline can
  provision almost anything your application needs: compute, data stores,
  queues, topics, alarms, secrets. You can look at test and prod, but you
  cannot change them by hand.
- **Secrets are infrastructure as code too.** The template declares the
  secret. Generated values are created inside AWS. Third-party keys are typed
  in once by a **secrets custodian**, who can write values but never read
  them.
- **Every deployment to test or prod needs an approved Jira change record
  before it runs.** Deploying without one is not blocked, but it is detected,
  and in prod it pages someone.
- **Tag everything you create**, deploy only to `us-east-1`, `us-east-2` or
  `us-west-2`, encrypt every data store, and leave anything named
  `platform-*` or `Platform*` alone.
- **IAM roles for your application are a request to AltDigital**, not
  something the pipeline creates.
- **No production data in dev or test, ever.** Data scanning (Macie) runs in
  dev exactly as it does in production.
- **If your application is HIPAA-scoped, uat and prod run only on
  HIPAA-eligible AWS services.** Dev and test warn you in the digest, so you
  find out while building, not at go-live. See [4.16](#416-hipaa-scoped-applications-eligible-services-only-in-uat-and-prod).

---

## Your accounts and what you can do in each

Each environment is its own AWS account, named
`altdig-<partner>-<client>-<env>`. For Arc8: `altdig-oeight-oeight-dev`,
`altdig-oeight-oeight-test` and `altdig-oeight-oeight-prod`. Arc8 has no UAT.

| | dev | test | prod (and UAT where used) |
|---|---|---|---|
| Permission set | `DeveloperDev` | `DeveloperTest` | `DeveloperProd` |
| Console | Full, within the guardrails | Read-only, plus restart | Read and observe |
| Create and delete resources | Yes | Pipeline only | Pipeline only |
| Create IAM roles | Yes, with the boundary (§4.4) | No | No |
| Read secret and parameter **values** | Yes | No | No |
| Read S3 objects and table rows | Yes | No | No |
| Session length | 4 hours | 4 hours | 2 hours |

**In test you can also** restart an ECS service (`ecs:UpdateService`), stop or
run a task, and invoke a Lambda function. Those let you recover a test cycle
without a redeploy. They change what is *running*, not what is *deployed*.

**In prod you can see** your application's logs, every CloudWatch metric,
alarms and their state, ECS stopped reasons, exit codes and service events,
the deployed task definition *revision*, CloudFormation and deployment history,
X-Ray and Application Signals traces, and parameter and secret *names*.

**In prod you cannot see** S3 object contents, database rows, secret or
parameter values, or the task definition itself. The last one is because
`ecs:DescribeTaskDefinition` returns environment variable values along with
the keys, and AWS has no way to return the keys alone. The same goes for
`lambda:GetFunctionConfiguration`. You can get the revision that is running
from `ecs describe-services`.

### Two grants that are not developer access

**`TenantReadOnly`** is the oversight grant for the development manager and
anyone else named for oversight. It applies in every account. It shows every
resource's metadata, logs, metrics, traces and findings. It cannot write
anything, read data, read secret values or assume roles.

**`SecretsCustodian`** is held by a small named group and applies in test,
UAT and prod (not dev, where developers manage secrets themselves). It can
create a secret, put a new value, rotate it, and write parameters. It
**cannot read a value back**, including one it wrote, and cannot delete
anything. Sessions last one hour. See §2.3.

### You can see the controls that watch you

Every permission set can read the AWS Config rules, Security Hub findings,
GuardDuty findings, platform alarms and flow log configuration in your
account. That is deliberate. If something is flagging your resources, you can
look up which rule fired and why without filing a ticket. Start with Security
Hub → Findings, filtered to your account.

---

## 1. Signing in

### First time

1. **Accept the invitation.** AWS IAM Identity Center sends an email titled
   *"Invitation to join AWS IAM Identity Center"*. Accept it, set a password
   and register an MFA device. Use an authenticator app or a security key, not
   SMS.
2. **Open the access portal:** `https://altdigital.awsapps.com/start`.
3. **Pick an account, then a role.** Each account lists the permission sets you
   hold there (`DeveloperDev`, `DeveloperTest`, `DeveloperProd`,
   `TenantReadOnly` or `SecretsCustodian`). Choose **Management console** to
   open it in the browser, or **Access keys** to see the CLI settings.

If an account you expect is missing, you have not been granted access to that
environment. Ask the development manager. Access is granted per environment
from the tenant's developer roster.

**If you hold two identities** (one AltDigital and one tenant), use the
**tenant** identity for application work. The AltDigital one is for platform
roles and will not show your application's developer accounts.

### CLI and SDKs (AWS CLI v2)

Configure single sign-on once:

```bash
aws configure sso
#   SSO session name:        altdigital
#   SSO start URL:           https://altdigital.awsapps.com/start
#   SSO region:              us-east-2
#   SSO registration scopes: sso:account:access
```

The CLI opens a browser to approve the device and then asks you to pick an
account and role. Run it once per account and role you use, and give each
profile a clear name, for example `arc8-dev`, `arc8-test`, `arc8-prod`. Set the
default region to `us-east-2`.

The result in `~/.aws/config` (on Windows, `%USERPROFILE%\.aws\config`):

```ini
[sso-session altdigital]
sso_start_url = https://altdigital.awsapps.com/start
sso_region = us-east-2
sso_registration_scopes = sso:account:access

[profile arc8-dev]
sso_session = altdigital
sso_account_id = <dev account id, shown in the portal>
sso_role_name = DeveloperDev
region = us-east-2
```

Day to day:

```bash
aws sso login --sso-session altdigital    # once per session; opens the browser
aws sts get-caller-identity --profile arc8-dev
export AWS_PROFILE=arc8-dev               # PowerShell: $env:AWS_PROFILE = "arc8-dev"
```

All current AWS SDKs, CDK, SAM and Terraform read SSO profiles directly, so
set `AWS_PROFILE` and do not copy keys anywhere.

**When the session expires** (after 4 hours in dev and test, 2 in prod, 1 for
`SecretsCustodian`), the CLI returns `Token has expired` or `The SSO session
associated with this profile has expired`. Run `aws sso login` again. The
short session is deliberate: it limits how long a lost laptop or a revoked
account keeps working.

**Make your everyday default `arc8-dev`, not a prod profile.** AI coding
assistants and scripts run with whatever profile your shell has active. Pick
a prod profile on purpose, when you need it, and close it afterwards.

---

## 2. Tokens and secrets

### 2.1 AWS credentials: short-lived only

- **No IAM users and no access keys, for people or tools.** You cannot create
  them, and requests for them will be refused.
- **On your machine**, use the SSO profile from §1.
- **For a container or tool that needs plain environment variables**, export
  temporary credentials from your SSO session:

  ```bash
  aws configure export-credentials --profile arc8-dev --format env
  ```

  These expire with your session. Never write them into a committed `.env`,
  a Dockerfile or an image.
- **In CI**, use OIDC (§3.2). The pipeline never holds a stored AWS key.
- **Inside AWS**, your workloads use their IAM role (ECS task role, Lambda
  execution role) and need no credentials at all.

### 2.2 Application secrets are infrastructure as code

Secrets go in **AWS Secrets Manager**. Configuration that is not secret goes in
**SSM Parameter Store**. Both are declared in your templates and deployed by
your pipeline like any other resource.

The one rule: **a secret's value never goes in the template.** The template
lives in git. Which pattern you use depends on where the value comes from:

| Where the value comes from | Pattern | Who ever sees the value |
|---|---|---|
| Your application can make it up (database password, signing key, internal token) | `GenerateSecretString` in the template, or `ManageMasterUserPassword: true` on RDS | Nobody. It is created inside AWS. |
| A third party issues it (Stripe, carrier APIs, an OAuth client secret) | The template declares the secret with no value. A **secrets custodian** then puts the value in once (§2.3). | The custodian, once, while typing it |
| It must rotate | Secrets Manager rotation, configured in the template | Nobody |

Generated secret, CloudFormation:

```yaml
DbAppPassword:
  Type: AWS::SecretsManager::Secret
  Properties:
    Name: arc8/db-app-password
    KmsKeyId: alias/platform-secrets
    GenerateSecretString:
      PasswordLength: 40
      ExcludePunctuation: true
```

Third-party secret, declared with no value:

```yaml
StripeApiKey:
  Type: AWS::SecretsManager::Secret
  Properties:
    Name: arc8/stripe-api-key
    KmsKeyId: alias/platform-secrets
    Description: Set by a secrets custodian. Never in the repository.
```

**Naming is what makes them reachable.** The deploy role, the custodian and
your application can only work with names under the application's prefix:

| Kind | Name pattern | Example |
|---|---|---|
| Secret | `arc8/<name>` | `arc8/stripe-api-key` |
| Parameter | `/arc8/<name>` | `/arc8/feature-flags/new-checkout` |

Anything under `platform/` or `/platform/` belongs to the platform and is
off limits.

**Encrypt with a customer-managed key**: `alias/platform-secrets`, or the
application's dedicated key if it has one. Creating a secret without
`KmsKeyId` is **refused**. The AWS default key cannot carry the platform's
deletion controls, and a secret using it raises a finding
(`platform-secret-uses-cmk`).

**Rotate them.** A secret not rotated within 90 days raises a finding
(`platform-secret-max-age`). It is a reminder with an owner, and nothing is
revoked automatically.

**Never** put a secret in plain task-definition environment variables, a Lambda
environment variable or source control.

### 2.3 Putting a third-party value into test or prod

1. Your pipeline deploys the secret, declared with no value as above.
2. A **secrets custodian** signs in with `SecretsCustodian` for that account
   and sets the value:

   ```bash
   aws secretsmanager put-secret-value --profile arc8-prod-secrets \
     --secret-id arc8/stripe-api-key \
     --secret-string file://stripe-key.txt     # then delete the file
   ```

   Read the value from a file or from standard input, not as a command-line
   argument, which other processes on the machine can see.
3. Your application reads it at runtime. In ECS, reference it in the
   container definition's `secrets` block (`valueFrom` takes the secret ARN)
   rather than fetching it in code. The task role needs
   `secretsmanager:GetSecretValue` on that secret and `kms:Decrypt` on the key.

The custodian can **replace** a value but never **read** one. Someone who sets
the production payment key does not thereby gain a way to read it next month.
Every write is logged in CloudTrail under the custodian's name.

Treat a production secret change as a production change: raise a change record
for it (§3.3).

**Who the custodians are** is set in the tenant's roster, through the
development manager. Keep the group small.

**In dev** you need none of this. `DeveloperDev` creates, reads and deletes
secrets directly. Use test or sandbox credentials from your providers there,
never production ones.

---

## 3. Deploying code

### 3.1 Dev: deploy however you like

Deploy from your machine with the `arc8-dev` profile, using the console,
CLI, CloudFormation, CDK, SAM or Terraform. Changes in dev are not reconciled
against change records, and you need no ticket. The guardrails in §4 still
apply, and three of them catch nearly everyone on the first day:

- **tags** on every create (§4.2)
- **the permissions boundary** on every IAM role (§4.4)
- **an approved region** (§4.1)

Build dev with the same templates your pipeline will use for test and prod.
If it only works in dev because you clicked something in the console, it
will not work in test.

### 3.2 Test and prod: through the pipeline only

Changes reach test and prod **only** through your CI pipeline assuming the
role `TenantAppDeployer-arc8` in that account. There is no stored key. GitHub
issues a short-lived token for each workflow run, and AWS exchanges it for
credentials lasting at most an hour.

**Setup, done once per environment with AltDigital:**

1. Tell AltDigital the **repository** (`owner/name`) and the **branch or
   GitHub environment** that may deploy to each account. For example: `main`
   deploys to test, and the `production` environment deploys to prod.
2. AltDigital deploys the role pinned to exactly that repository and
   branch or environment, and sends you the **role ARN**. The role ARN is not
   a secret.
3. Run the workflow once. The first run confirms that the trust works.
   GitHub sometimes sends the repository's numeric IDs instead of its name.
   When that happens the first run is refused, AltDigital reads the exact
   claim from CloudTrail, and the role is re-pinned to it. Expect this to
   happen once.

Pin to a branch or a GitHub environment, never `repo:owner/name:*`. A
wildcard would let any branch, including a pull request from a fork, deploy
to production.

**Workflow skeleton:**

```yaml
name: deploy-test
on:
  push:
    branches: [main]

permissions:
  id-token: write      # required: this is what lets GitHub issue the OIDC token
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<test account id>:role/TenantAppDeployer-arc8
          aws-region: us-east-2

      - run: aws sts get-caller-identity   # proves which role the run is using

      # ... build, push to ECR, deploy the stack
```

**What the deploy role can provision.** Everything is scoped to names
beginning `arc8-` (`arc8/` for secrets) and to the approved regions:

| Area | Services |
|---|---|
| Stacks | CloudFormation, for stacks named `arc8-*` |
| Compute | ECS, Lambda, Step Functions, API Gateway, load balancers, autoscaling |
| Images | ECR repositories `arc8*` |
| Data | S3, RDS and Aurora, DynamoDB, ElastiCache, EFS (EFS by the `Application` tag) |
| Messaging | SQS, SNS, EventBridge rules and buses |
| Observability | CloudWatch Logs, alarms and dashboards |
| Network | Security groups for your own resources, inside the baseline VPC |
| Configuration | Secrets `arc8/*`, parameters `/arc8/*` |

**What the deploy role cannot do:**

- **Create or change IAM roles or policies.** Your application's roles (ECS
  task roles, Lambda execution roles) are created by AltDigital on request.
  Name them `arc8-<purpose>` (for example `arc8-api-task`) and the pipeline
  can attach them. The pipeline can create the *service-linked* roles AWS
  itself defines, such as the RDS one, so a first deploy in a fresh account
  does not fail on IAM.
- Anything in §4 that applies to everyone.

**Name your data stores.** Give every database, table, bucket, cache and
queue an explicit name (`arc8-orders-db`, not left to CloudFormation). If a
template change would force CloudFormation to *replace* a named resource, the
update fails before anything happens, instead of quietly creating a new,
empty database and switching your stack to it. Read every change set before
it runs, and treat `Replacement: True` on a data store as a stop sign.

### 3.3 The change record: every test and prod deployment

AltDigital does not gate your pipeline. Every deployment is **detected** from
CloudTrail and **compared** with your Jira change records. That is the SOC 2
change-management control, and it works whether or not anyone remembers it.

**Before deploying to test or prod:**

1. Raise a **Change** in the application's Jira change project.
2. Add the label `account-<account id>` to scope it to the account being
   changed. This is optional, but without it the record matches any account.
3. Get it **approved**. The requester's own approval does not count, and
   where a list of required approvers has been published, each of them must
   approve.
4. Deploy within **24 hours** of the record's creation.

**What happens:**

| What reconciliation finds | Result |
|---|---|
| Deployment with an approved change record | Kept as audit evidence. Nothing else happens. |
| Deployment with **no** change record | Finding plus a Jira ticket to the change approver. **In prod this pages.** |
| Change record created *after* the deployment | Treated as a retrospective note, not an approval. Finding. |
| Change record matched but not approved | Finding |
| Approved change record with no deployment | Flagged for review as stale or abandoned |

Deployments counted include CloudFormation stack updates, ECS task definition
and service changes, Lambda code and configuration updates, launch template
and AMI changes, and RDS parameter group or engine version changes. Restarting
a service in test with your own `DeveloperTest` access is not a deployment of
new code, but it is still recorded.

*For Arc8, the Jira site and change project are being set up. Until AltDigital
confirms they are connected, raise change records wherever the development
manager directs, so the history is there to reconcile once the project is
live.*

---

## 4. Guardrails you will hit

Three layers enforce these:

- **Organization policies (SCPs and RCPs)** apply to everyone in the account,
  account administrators included.
- **A permissions boundary** applies to every role you create, and to the
  deploy role, so nothing built can do more than its builder could.
- **Your permission set** sets your own access in that environment.

### Reading an access-denied error

Current AWS errors name the type of policy that refused the request, and that
tells you who can help:

| The error says… | Meaning | What to do |
|---|---|---|
| `…with an explicit deny in a service control policy` | An organization guardrail. Section 4 lists them. | Change the request (tags, region, name). If it is still legitimate, ask AltDigital. |
| The same, in **uat or prod**, on a call that **worked in dev** | Almost certainly HIPAA eligibility (§4.16): the service is not HIPAA-eligible | Check the §4.16 table for what to use instead. Your dev digest will already have named it. |
| `…with an explicit deny in a resource control policy` | A cross-organization access attempt (§4.7) | Not workable as designed. Talk to AltDigital. |
| `…because no permissions boundary allows…` or `…explicit deny in a permissions boundary` | A boundary ceiling was reached (§4.4, §4.9, §4.10) | Usually a data store missing encryption, or a deletion in prod |
| `…because no identity-based policy allows…` | Your permission set does not grant it in this environment, or the resource name is outside `arc8-*` | In test or prod, use the pipeline. Check the name. |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | Pipeline trust mismatch | Send AltDigital the workflow run URL (§3.2 step 3). |

### 4.1 Regions: `us-east-1`, `us-east-2` and `us-west-2` only

Every regional action anywhere else is denied. Home region is **`us-east-2`**.
Global services (IAM, CloudFront, Route 53, STS and similar) are unaffected.

**Why:** monitoring, Config and threat detection exist only in the approved
regions. A resource anywhere else is invisible to them, and that is the most
common accidental way round every other control. If a tutorial or a tool
defaults to `us-west-1` or `eu-west-1`, that is the reason for the denial.

### 4.2 Tags are required when you create

These creates are **denied unless all four tags are on the request**:
`ec2:RunInstances`, `ec2:CreateVolume`, `rds:CreateDBInstance`,
`rds:CreateDBCluster`, `s3:CreateBucket`, `lambda:CreateFunction`,
`ecs:CreateService`, `elasticloadbalancing:CreateLoadBalancer` and
`dynamodb:CreateTable`.

| Tag | Value |
|---|---|
| `Owner` | A team name, for example `arc8-dev-team`. **Not** a person's name or email. |
| `Application` | `arc8` |
| `DataClassification` | One of `public`, `internal`, `confidential`, `regulated` |
| `platform-layer` | `application`, always |

**Why:** the account identifies who pays. The tags identify who owns the
resource and whom to page when it alarms. `platform-layer` is what routes an
alarm to your team rather than to AltDigital's infrastructure on-call.

- Set tags at the **stack level** in CloudFormation, CDK or SAM so they
  propagate to every resource. Tags on the resource itself also work. Both
  have been tested against the guardrail for S3, DynamoDB, EBS, ECS services
  and Lambda. In Terraform, use the provider's `default_tags`.
- Tags must be on the **create call itself**. CloudFormation does this. A tool
  that creates first and tags afterwards is denied at the create. If that
  happens, tell AltDigital which tool and version produced it.
- **EFS file systems** must carry `Application=arc8` when created through the
  pipeline, or the pipeline cannot manage them afterwards.
- **Never set `platform-layer=infrastructure`**, and never add or remove it
  on a platform resource. Copying a tag block out of a platform template is
  the usual way this happens.
- You cannot add or remove these platform tags on anything: `platform-managed`,
  `AlarmTier`, `BackupPolicy`, `PatchGroup`, `BaselineVersion`,
  `InstrumentedAt`, `BillingRollup`, `PartnerSlug`, `ClientSlug`.

### 4.3 Platform resources are read-only to you

You can see all of these. You cannot change, delete or recreate any of them:

- Anything named **`platform-*`** or **`Platform*`**: alarms, EventBridge
  rules, Lambda functions, DynamoDB tables, CloudFormation stacks, IAM roles
  and policies
- SSM parameters under **`/platform/`**, secrets under **`platform/`**
- Log groups under `/aws/platform/` and `/aws/lambda/platform-*`
- The roles `TenantAppDeployer-*`, `OrganizationAccountAccessRole` and
  `stacksets-exec-*`
- CloudTrail, the AWS Config recorder and rules, GuardDuty, Security Hub,
  Macie, Inspector, Access Analyzer, VPC flow logs, DNS query logging and the
  account-level log data protection policy
- The baseline VPC: its route tables, NACLs, subnets, VPC endpoints and
  platform-managed security groups
- The account-level S3 public access block
- AWS Backup vaults, plans and recovery points, and platform-managed snapshots

**Do not name your own resources with a `platform` prefix.** The name itself
is reserved and you will be denied.

### 4.4 IAM roles need the permissions boundary (dev)

In dev you can create IAM roles, but **only with the boundary
`PlatformDeveloperBoundary` attached**. Without it the create is denied.
Removing or swapping a boundary is also denied.

```bash
aws iam create-role --profile arc8-dev \
  --role-name arc8-api-task \
  --assume-role-policy-document file://trust.json \
  --permissions-boundary arn:aws:iam::<dev account id>:policy/PlatformDeveloperBoundary
```

- **CloudFormation:** `PermissionsBoundary` on `AWS::IAM::Role`
- **CDK:** `PermissionsBoundary.of(this).apply(ManagedPolicy.fromManagedPolicyName(this, 'B', 'PlatformDeveloperBoundary'))`
- **SAM:** `PermissionsBoundary` under `Globals.Function`
- **Terraform:** `permissions_boundary` on `aws_iam_role`

**Why:** without the boundary, someone who cannot read a secret could create
a role that can, attach it to a Lambda and invoke it, and every step would be
individually allowed. The boundary keeps everything you build within the
guardrails you are within. It allows anything except the platform denials, so
your application roles are not otherwise limited.

In test and prod, roles are created by AltDigital on request (§3.2). Send the
role's purpose and the policy it needs, ideally the same one you proved in
dev.

**No IAM users, and no access keys.** They cannot be created.

### 4.5 Networking stays inside the baseline VPC

Denied:

- creating internet gateways, NAT gateways or egress-only gateways
- VPC peering, Transit Gateway attachments, VPN, Client VPN, Direct Connect
- VPC endpoint services
- changing the baseline VPC's routes, subnets, NACLs or platform-managed
  security groups

**Allowed:** your own security groups in the baseline VPC, load balancers and
everything that runs inside the VPC. In test and prod the pipeline can only
change security groups tagged `Application=arc8`. Stack-level tags take care
of that.

**Outbound internet** depends on the account's egress profile. Arc8 uses
`dns-filtered`: outbound goes through NAT, and DNS lookups for known-malicious
domains are blocked. If an outbound call fails at name resolution, check that
first. The VPC's `EgressProfile` tag shows the profile.

**Why:** no private connectivity between tenants is what keeps one tenant's
compromise from reaching another. Applications call each other over published
APIs, as they would any SaaS vendor.

### 4.6 No SSH: use Session Manager

There is no SSH path, no bastion and no VPN. To reach an instance:

```bash
aws ssm start-session --profile arc8-dev --target <instance id>
```

Opening inbound port 22 is alarmed as a finding of its own. Session Manager
sessions are logged against your identity, and SSH logins cannot be.

### 4.7 No cross-account trust, and no identity providers of your own

- A role that trusts a principal **outside the AltDigital organization**
  cannot be assumed. The resource control policy denies it.
- You cannot create **OIDC or SAML identity providers**. For CI, use
  `TenantAppDeployer-arc8` (§3.2).
- You cannot **share** EBS snapshots, AMIs or RDS snapshots with another
  account.

**Why:** each of these is a way out of the account that no alarm would see.
Promotion from dev to prod goes through the pipeline and build artifacts,
never through copied data or shared images.

### 4.8 KMS keys

Use the platform keys (`alias/platform-secrets`, `alias/platform-storage`,
`alias/platform-database`), or the application's dedicated key if it has one.
You can create your own keys in dev, but you **cannot disable, schedule
deletion of, change the policy of, or delete the alias of any key**, yours
included. A key you create is effectively permanent, so create one only when
you need one.

### 4.9 Data stores must be encrypted, and stay private

Refused when the pipeline creates them:

- an **RDS instance or Aurora cluster** without `StorageEncrypted: true`
- an **EFS file system** without `Encrypted: true`
- an **ElastiCache replication group** without `AtRestEncryptionEnabled: true`
- an **SNS subscription** that delivers outside AWS (`email`, `email-json`,
  `sms`, `http`, `https`). Deliver to SQS, Lambda or Firehose instead.

S3 and DynamoDB always encrypt, so there is nothing to set.

**Why SNS:** a subscription is how regulated data leaves the account without
anything that looks like an export. An alarm or event payload forwarded to a
mailbox is a HIPAA disclosure. If you need email notifications, raise it with
AltDigital as a change, so what goes in the payload is reviewed first.

Some rules cannot be enforced when a resource is created, so Security Hub
**reports** them instead. Expect a finding if you do any of these:

- make a database `PubliclyAccessible`
- open a database or cache security group to `0.0.0.0/0`
- turn off DynamoDB point-in-time recovery or set RDS backup retention to 0

A finding is not a block. It has an owner and a date, and AltDigital will
follow it up.

### 4.10 In UAT and prod, data stores are never deleted by the pipeline

In UAT and prod, the deploy role **cannot delete** RDS instances, clusters
and snapshots, DynamoDB tables and backups, S3 buckets, ElastiCache caches,
EFS file systems, SQS queues or secrets. Test has no such restriction.

**Why:** the dangerous delete is not a deliberate one. It is CloudFormation
replacing a database because of a one-line template change. With this rule,
the data survives even if a replacement gets through. Name your data stores
(§3.2) so a replacement cannot get through at all.

A deliberate deletion in UAT or prod is a change request to AltDigital. Set
`DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain` on data stores in
your templates, so removing a resource from the template does not try to
delete it.

### 4.11 Your telemetry is watched for silence

If an instance is alive and passing traffic but its CloudWatch agent or SSM
agent stops reporting, that is detected and attributed: Session Manager
sessions are in CloudTrail. In dev it appears in the manager's digest. In
prod it is investigated. Do not stop or uninstall the agents.

### 4.12 Production data never goes to dev or test

Macie scans dev the same way it scans production. Regulated data found in
dev or test is not handled as a policy violation. It is a **scope change**:
that account becomes subject to audit and production-grade controls (full
retention, full cost) until the data is removed and that is attested. The
customer is notified with a timestamp and a remediation deadline.

Use synthetic or de-identified test data. If you need a realistic data
set, ask for one.

### 4.13 Test is not a place to read data either

`DeveloperTest` denies reading S3 objects, secret and parameter values,
DynamoDB items and RDS Data API results, even though test should hold no
real data. *Should not* is not a control.

### 4.14 Dev and test turn off outside working hours

Non-production accounts are scheduled off outside their hours of operation.
For Arc8, dev and test run **Monday to Friday, 08:00–17:00 India time**.
Outside those hours, services are scaled to zero and instances are stopped.
That is the schedule, not an outage. To work outside hours, ask the
development manager to have the environment turned on.

### 4.15 Expensive services are allowed, and visible

Bedrock, SageMaker and similar services are not blocked. Turning one on
appears in the development manager's digest and counts against the budget
alarm. The digest also lists new services enabled in the account, roles
created, guardrail denials and telemetry gaps.

For a HIPAA-scoped application this holds in dev and test. In uat and prod a
service must also be HIPAA-eligible (§4.16). Bedrock and SageMaker both are.

### 4.16 HIPAA-scoped applications: eligible services only in uat and prod

**This section applies only if your application is HIPAA-scoped**: the
organisation chose `hipaa` in the onboarding questionnaire. If it is not,
none of this applies, and you may use any AWS service within the other
guardrails.

AltDigital's agreement with AWS (a Business Associate Addendum) covers patient
data only in the services AWS lists as HIPAA-eligible. So:

| Environment | What happens if you use a service that is not eligible |
|---|---|
| **dev, test** | It works. The next daily digest names the service, says it will be denied in uat and prod, and says what to use instead. |
| **uat, prod** | Denied by an organization policy (`…explicit deny in a service control policy`). |

**The digest is how you find out early.** A line looks like this:

> `apprunner` used in arc8-dev (dev). Not HIPAA-eligible — this will be
> denied in uat and prod. Use instead: ECS on Fargate.

Act on it while you are still building. A design that depends on a
non-eligible service will not deploy to uat or prod.

**A resource tagged `DataClassification=regulated` on a non-eligible service**
is flagged at the highest priority in every environment. In dev it usually
means "this design will put patient data on a service the agreement does not
cover", which must be redesigned before uat. In prod it is treated as a
possible incident.

**Needed for something that never touches patient data?** Ask AltDigital for an
exception through the change project. Say what the service does, why no
eligible service fits, and confirm that no patient data is stored in it,
processed by it or sent through it. Your organisation's change approver signs
that statement, and AltDigital approves it. Exceptions expire (90 days by
default) and are renewed the same way. An exception does not cover a
resource tagged `regulated`.

<!-- hipaa-eligibility:begin (generated by scripts/render-hipaa-eligibility-guide.py; do not edit) -->

*List version `2026-09-03/1`, from AWS's HIPAA Eligible Services Reference.*

**Not eligible: use the alternative.** Allowed in dev and test, where the digest flags it. Denied in uat and prod.

| Service | IAM prefix | Use instead |
|---|---|---|
| Alexa for Business [for healthcare skills only - requires Alexa Skills BAA. See HIPAA whitepaper for details] | `a4b` | — |
| Amazon Q Developer | `q`, `codewhisperer` | Use in dev only; not needed in uat/prod |
| App Runner | `apprunner` | ECS on Fargate |
| CodeArtifact | `codeartifact` | ECR (OCI artifacts) or S3 |
| CodeStar Connections / CodeConnections | `codestar-connections`, `codeconnections` | GitHub OIDC to the deploy role (developer guide 3.2) |
| EC2 Instance Connect | `ec2-instance-connect` | Session Manager |
| ECR Public | `ecr-public` | ECR pull-through cache |
| End User Messaging Social (WhatsApp) | `social-messaging` | End User Messaging SMS |
| Lightsail | `lightsail` | EC2 or ECS on Fargate |

**Eligibility not yet confirmed: treated as not eligible until reviewed.** Mostly features AWS documents under an eligible service but gives an IAM prefix of their own. Ask AltDigital before designing around one.

Amplify backend and UI builder (`amplifybackend`, `amplifyuibuilder`), AWS AppConfig (`appconfig`), Backup gateway (`backup-gateway`), Backup search (`backup-search`), Clean Rooms ML (`cleanrooms-ml`), CloudWatch Application Signals (`application-signals`), CloudWatch cross-account observability (`oam`), CloudWatch Internet Monitor (`internetmonitor`), CloudWatch Network Monitor (`networkmonitor`), CloudWatch RUM (`rum`), CloudWatch Synthetics (`synthetics`), DevOps Agent (candidate prefix) (`aidevops`), DynamoDB Accelerator (DAX) (`dax`), EMR on EKS (`emr-containers`), EMR Serverless (`emr-serverless`), EventBridge Pipes (`pipes`), EventBridge Scheduler (`scheduler`), EventBridge Schema Registry (`schemas`), IoT Device Management Fleet Hub (`iotfleethub`), IoT Jobs data plane (`iotjobsdata`), Neptune Analytics (`neptune-graph`), OpenSearch Serverless (`aoss`), Q Apps (`qapps`), Redshift Serverless (`redshift-serverless`), Route 53 Domains (`route53domains`), Route 53 Profiles (`route53profiles`), S3 Express One Zone (`s3express`), S3 Object Lambda (`s3-object-lambda`), S3 Tables (`s3tables`), S3 Vectors (`s3vectors`), Systems Manager GUI Connect (`ssm-guiconnect`), Systems Manager Incident Manager (`ssm-incidents`, `ssm-contacts`), Systems Manager Quick Setup (`ssm-quicksetup`), Timestream for InfluxDB (`timestream-influxdb`)

**Also allowed: running the account.** These are not on AWS's list, but they hold no patient data: identity, billing, quotas, support, tagging. **Never put patient data in a support case or a tag.**

Account management (`account`), Application Auto Scaling (`application-autoscaling`), AWS Health (`health`), AWS Support (`support`), AWS User Notifications (`notifications`), Compute Optimizer (`compute-optimizer`), Console settings (`uxc`), Console sign-in (`signin`), Cost and billing (`ce`, `cur`, `budgets`, `billing`, `aws-portal`, `payments`, `invoicing`, `tax`, `pricing`, `freetier`, `bcm-data-exports`, `cost-optimization-hub`, `savingsplans`), IAM (`iam`), IAM Access Analyzer (`access-analyzer`), Resource Groups (`resource-groups`), Resource Groups Tagging API (`tag`), Service Quotas (`servicequotas`), STS (`sts`), Trusted Advisor (`trustedadvisor`)

<details><summary><b>Eligible services</b> — 172 on AWS's list. A bracketed note is a limit AWS sets on that service.</summary>

- Amazon API Gateway
- Amazon AppFlow
- Amazon Application Recovery Controller
- Amazon Athena
- Amazon Augmented AI [excludes Public Workforce and Vendor Workforce for all features]
- Amazon Aurora
- Amazon Bedrock
- Amazon Bedrock AgentCore
- Amazon Chime
- Amazon Chime SDK
- Amazon Cloud Directory
- Amazon CloudFront [excludes content delivery through Amazon CloudFront Embedded Point of Presences]
- Amazon CloudWatch
- Amazon CloudWatch Logs
- Amazon CloudWatch SDK Metrics
- Amazon Cognito
- Amazon Comprehend
- Amazon Comprehend Medical
- Amazon Connect
- Amazon DataZone
- Amazon Detective
- Amazon DevOps Guru
- Amazon DocumentDB [with MongoDB compatibility]
- Amazon DynamoDB
- Amazon EC2 Auto Scaling
- Amazon Elastic Block Store (Amazon EBS)
- Amazon Elastic Compute Cloud (Amazon EC2)
- Amazon Elastic Container Registry (ECR)
- Amazon Elastic Container Service (ECS)
- Amazon Elastic File System (EFS)
- Amazon Elastic Kubernetes Service (EKS)
- Amazon Elastic MapReduce (EMR)
- Amazon ElastiCache
- Amazon EventBridge [formerly Amazon Cloudwatch Events]
- Amazon Forecast
- Amazon FreeRTOS
- Amazon FSx
- Amazon GuardDuty
- Amazon Inspector
- Amazon Kendra
- Amazon Keyspaces [For Apache Cassandra]
- Amazon Kinesis Data Firehose
- Amazon Kinesis Data Streams
- Amazon Kinesis Video Streams
- Amazon Lex
- Amazon Location Service
- Amazon Macie
- Amazon Managed Service for Apache Flink
- Amazon Managed Service for Prometheus
- Amazon Managed Streaming for Apache Kafka
- Amazon Managed Workflow for Apache Airflow
- Amazon MemoryDB
- Amazon MQ
- Amazon Neptune
- Amazon Nova Act
- Amazon OpenSearch Service
- Amazon Personalize
- Amazon Pinpoint and End User Messaging (formerly Amazon Pinpoint) [excluding Voice Message capabilities and WhatsApp Channel]
- Amazon Polly
- Amazon Q Business
- Amazon Quantum Ledger Database (QLDB)
- Amazon Quick [formerly Amazon QuickSight, web only]
- Amazon Redshift
- Amazon Rekognition
- Amazon Relational Database Service (Amazon RDS) [SQL Server, MySQL, Oracle, PostgreSQL, Db2 and MariaDB engines only]
- Amazon Route 53
- Amazon S3 Glacier
- Amazon SageMaker AI [formerly Amazon Sagemaker, excludes Studio Lab, Ground Truth Plus, Public Workforce and Vendor Workforce for all features]
- Amazon Simple Email Service (Amazon SES)
- Amazon Simple Notification Service (SNS)
- Amazon Simple Queue Service (SQS)
- Amazon Simple Storage Service (S3)
- Amazon Simple Workflow Service (SWF)
- Amazon Textract
- Amazon Timestream
- Amazon Translate
- Amazon Verified Permissions
- Amazon Virtual Private Cloud (VPC)
- Amazon WorkDocs [Excluding Adding Controls for Deleting Previous File Version Feature]
- Amazon WorkLink
- Amazon WorkSpaces
- Amazon WorkSpaces Applications [formerly known as Amazon AppStream 2.0]
- Amazon WorkSpaces Secure Browser
- Amazon WorkSpaces Thin Client
- AWS Amplify Console
- AWS App Mesh
- AWS AppFabric
- AWS Application Migration Service
- AWS AppSync
- AWS Audit Manager
- AWS B2B Data Interchange
- AWS Backup
- AWS Batch
- AWS Certificate Manager
- AWS Clean Rooms
- AWS Cloud 9
- AWS Cloud Map
- AWS CloudEndure
- AWS CloudFormation
- AWS CloudHSM
- AWS CloudShell
- AWS CloudTrail
- AWS CodeBuild
- AWS CodeCommit
- AWS CodeDeploy
- AWS CodePipeline
- AWS Config
- AWS Control Tower
- AWS Data Exchange
- AWS Database Migration Service (DMS)
- AWS DataSync
- AWS DevOps Agent
- AWS Direct Connect
- AWS Directory Service [excludes Simple AD]
- AWS Elastic Beanstalk
- AWS Elastic Disaster Recovery
- AWS Elemental MediaConnect
- AWS Elemental MediaConvert
- AWS Elemental MediaLive
- AWS Entity Resolution
- AWS Fargate [ECS and EKS engines only]
- AWS Fault Injection Simulator
- AWS Firewall Manager
- AWS Global Accelerator
- AWS Glue
- AWS Glue DataBrew
- AWS HealthImaging
- AWS HealthLake
- AWS HealthOmics
- AWS IAM Identity Center
- AWS IoT Core
- AWS IoT Device Management
- AWS IoT Events
- AWS IoT Greengrass
- AWS IoT SiteWise
- AWS Key Management Service (KMS)
- AWS Lake Formation
- AWS Lambda
- AWS Mainframe Modernization
- AWS Managed Services [excluding Operations on Demand Services, except for the RFC Expedite feature]
- AWS MCP Server
- AWS Network Firewall
- AWS OpsWorks for Chef Automate
- AWS OpsWorks for Puppet Enterprise
- AWS OpsWorks Stacks
- AWS Organizations
- AWS Outposts
- AWS Parallel Computing Service (PCS)
- AWS Private Certificate Authority
- AWS Resilience Hub
- AWS Resource Access Manager (RAM)
- AWS Resource Explorer
- AWS Secrets Manager
- AWS Security Hub CSPM (formerly AWS Security Hub)
- AWS Serverless Application Repository
- AWS Service Catalog
- AWS Shield [Standard and Advanced]
- AWS Snowball
- AWS Snowball Edge
- AWS Step Functions
- AWS Storage Gateway
- AWS Systems Manager
- AWS Transcribe [Includes Healthscribe]
- AWS Transfer Family
- AWS Transform
- AWS Verified Access
- AWS Web Application Firewall (WAF)
- AWS Wickr
- AWS X-Ray
- Elastic Load Balancing
- Kiro [excluding Kiro Web]
- VM Import/Export

</details>

<!-- hipaa-eligibility:end -->

---

## Logging standard

In production you get logs, metrics and traces, not data access. When "I
cannot see why it failed" comes up, the answer is better logging, so build
it in from the start:

- **Structured JSON** logs, one event per line
- A **correlation ID** carried across every service a request touches
- **Configuration logged at startup**: names and versions, never secret values
- **Log level adjustable without a redeploy**

Log data protection masks recognised sensitive data when it is written, as a
backstop. Do not log PHI or cardholder data and rely on the masking to catch
it.

---

## Getting help

| Need | Go to |
|---|---|
| Access to an environment, a new developer, someone leaving, a new secrets custodian | The development manager (roster change) |
| An IAM role for the application in test or prod | AltDigital, via the change project. Include the policy you proved in dev. |
| Deleting a data store in UAT or prod | AltDigital, via the change project |
| A guardrail blocking something legitimate | AltDigital. Include the full error text, the account and the time. |
| A non-eligible service for something that never touches patient data (HIPAA-scoped applications) | AltDigital, via the change project (§4.16) |
| Why something is flagged | Security Hub or Config in your own account first, then AltDigital |
