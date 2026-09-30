# Premium options — what they add to AWS cost

**Written for:** the AltDigital partner deciding whether to offer, price or
talk a client out of an option the platform does not build by default —
Jamie on the architecture, Tina on the price, Art where a framework is cited.

**What this is.** AWS **public list prices**, us-east-2 (Ohio), on-demand,
USD, read on **2026-09-30** from AWS's pricing pages and Price List Bulk API
files. It is the raw AWS cost only. AltDigital's flat fee and uplift sit on
top and are not here, and nothing in this document is a client's spend.

**Re-read before quoting.** AWS changes these. Each row names its source.

**What "small" means below.** One application, three accounts (dev, test,
prod), prod multi-AZ in us-east-2: two t3.medium app instances, an RDS
PostgreSQL db.t4g.medium Multi-AZ with 100 GB, an Application Load Balancer,
about 50 GB/month out. At list price that prod account's core is **about
$205/month**, or about $270 with two NAT gateways. Every figure below is
**added** to that, for prod only unless it says otherwise. An option enabled
in dev and test as well multiplies per account.

---

## Summary

| Option | Questionnaire | What drives the cost | Added per month, small app, prod | Confidence |
|---|---|---|---|---|
| Multi-region, backup only | 8.3 | Cross-region snapshot copies | ~$15 — RTO in hours | Medium |
| Multi-region, pilot light | 8.1, 8.3 | Standby database replica | ~$75 | Medium |
| Multi-region, warm standby (RDS replica) | 8.1 = 15 min, 8.3 | Replica + a running app tier, ALB and NAT in us-west-2 | ~$130–160 | Medium |
| Multi-region, warm standby (Aurora Global) | 8.1 = 15 min, 8.3 | Memory-optimised instances forced in both regions; migration off RDS | ~$570 | Medium |
| Network Firewall, 2 AZs (`locked` egress) | 6.6 | Endpoint-hours | ~$520 net; ~$1,075 with TLS inspection | High |
| Dedicated Instances | 9.6 | $2/hour fee per region per account | ~$1,464 **per account** | High |
| Dedicated Host (m5 / m6i) | 9.6, 10.1 | Per host-hour | $3,700 / $4,933 per host; ×2 for multi-AZ | High |
| CloudHSM, 2 HSMs | 9.2 | HSM-hours | ~$2,117 | High |
| KMS customer keys, incl. BYOK | 9.1, 9.3 | Key count and rotations | ~$1–3 per key; BYOK adds nothing | High |
| KMS with a CloudHSM key store | 9.2, 9.3 | CloudHSM, above | ~$2,100+ | Medium |
| Site-to-Site VPN | 6.4 | Connection-hours | ~$37–41 | High |
| Direct Connect | 6.4 | Port-hours, plus carrier charges AWS does not bill | $22 (50 Mbps) / $44 (100 Mbps) / $219 (1 Gbps) + carrier | High for AWS; carrier unknown |
| WAF (being built as standard) | 6.1, 6.5 | Web ACL, rules, requests | ~$13; +$10 with Bot Control | High |
| Budgets and anomaly detection (phase 15.9) | 13.2 | Free, bar action-enabled budgets beyond two per account | ~$0 | High / Medium |

**The two answers on the first real questionnaire that carry a price:**

- **RTO 15 minutes with multi-region premium** is roughly $130–160/month at
  list if the database stays on RDS, or roughly $570 on Aurora Global, which
  also means migrating the database engine. On a $205 prod core that is +60%
  to +280%, before the design and rehearsal work a 15-minute RTO needs. It
  also sits badly beside "no out-of-hours support": nobody is there to fail
  over.
- **Dedicated infrastructure** is about $1,464/month **per account** in the
  region fee alone. Across dev, test and prod that is about $4,400/month
  before a single instance, and none of HIPAA, SOC 2 or PCI DSS requires it
  (see [isolation-tiers.md](isolation-tiers.md)).

---

## Detail

### Multi-region (8.1, 8.3)

us-west-2 prices matched us-east-2 on every line checked.

| Item | Price |
|---|---|
| Cross-region transfer, Ohio → Oregon | $0.02/GB |
| RDS PostgreSQL db.t4g.medium | $0.065/hr Single-AZ, $0.129/hr Multi-AZ |
| RDS gp3 storage | $0.115/GB-mo Single-AZ, $0.23 Multi-AZ |
| Backup / snapshot storage beyond the free allocation | $0.095/GB-mo |
| Aurora PostgreSQL db.r6g.large | $0.26/hr |
| Aurora storage / I/O | $0.10/GB-mo, $0.20 per million |
| Aurora Global replicated write I/O | $0.20 per million |
| Route 53 health checks, AWS endpoints | first 50 free, then $0.50/mo; optional features $1.00/mo |

- **Pilot light:** an Oregon read replica (db.t4g.medium Single-AZ + 100 GB,
  ~$59), replication transfer (~$0.40), cross-region snapshot copies (~$15).
  No app tier runs until failover. About **$75**.
- **Warm standby, RDS replica:** the replica, one t3.medium ($30), an ALB
  ($22), one NAT ($33), health checks (~$2), snapshot copies (~$15). About
  **$160**, or ~$128 without the NAT. Promotion is a scripted step that takes
  minutes, so 15 minutes is plausible only if it is rehearsed.
- **Warm standby, Aurora Global:** Aurora Global requires memory-optimised
  classes (AWS recommends db.r5 or larger), so the database moves from RDS
  PostgreSQL to Aurora and from t4g to r6g in **both** regions. About **$570**
  added. Serverless v2 is worse at this size: AWS's recommended 8-ACU minimum
  on the primary alone is ~$700.
- Sources: aws.amazon.com/rds/postgresql/pricing/,
  aws.amazon.com/rds/aurora/pricing/, aws.amazon.com/route53/pricing/;
  RDS offer file 2026-09-29; Aurora Global requirements in the Aurora User
  Guide. WAL and I/O volumes are assumptions, which is why these rows are
  medium confidence.
- **Not priced:** EBS/AMI snapshot storage for the app tier.

### Network Firewall — the `locked` egress profile (6.6, B-010)

- Endpoint **$0.395/hr**, traffic **$0.065/GB**. TLS inspection adds
  $0.380/hr per endpoint.
- NAT gateway charges are waived for traffic in the same path as the
  firewall.
- Two AZs: $577 in endpoint-hours, plus ~$10 of traffic, less ~$66 of waived
  NAT. About **$520 per VPC**, $1,075 with TLS inspection. Per account, because
  this platform forbids a shared inspection VPC.
- Source: aws.amazon.com/network-firewall/pricing/; offer file 2026-09-11.

### Dedicated tenancy (9.6, 10.1)

- **Dedicated Instances:** a fee of **$2 per hour** in any region where at
  least one Dedicated Instance runs, which is $1,460/month, **per account**.
  The per-instance premium is small (t3.medium $0.0441 vs $0.0416 shared;
  m6i.large $0.1056 vs $0.096). t4g has no dedicated option.
- **Dedicated Hosts:** m5 **$5.069/hr** ($3,700/month), m6i **$6.758/hr**
  ($4,933/month), per host. Multi-AZ needs one per AZ. No region fee.
- **Not confirmed:** whether RDS, the load balancer or Fargate can be placed
  on dedicated tenancy. The figure covers the EC2 app tier only.
- Sources: aws.amazon.com/ec2/pricing/dedicated-instances/,
  aws.amazon.com/ec2/dedicated-hosts/pricing/; EC2 offer file 2026-09-25.

### CloudHSM and FIPS (9.2)

- hsm2m.medium **$1.45/hr** ($1,058.50/month per HSM). The KMS page's
  summary says "~$1.60"; the price list says $1.45.
- AWS "strongly recommends" at least two HSMs in two AZs for production, and
  three for mission-critical. Two HSMs: **~$2,117/month**.
- Source: aws.amazon.com/cloudhsm/pricing/, aws.amazon.com/cloudhsm/faqs/;
  offer file 2026-09-11.

### KMS (9.1, 9.3)

- Customer managed key **$1/month**; each of the first two rotations adds
  $1/month, so at most $3 per key.
- Symmetric requests $0.03 per 10,000; 20,000 free per month.
- **Imported key material (BYOK) costs the same.** Its cost is operational:
  AWS cannot rotate or recover it.
- A **CloudHSM custom key store** adds the CloudHSM cost above. An **external
  key store** costs the same on the AWS side; the external key manager is the
  client's cost.
- A per-application key in each of three accounts is a few dollars a month.
- **Not confirmed:** the minimum HSM count KMS requires for a custom key
  store.
- Source: aws.amazon.com/kms/pricing/; offer file 2026-09-11.

### Connectivity (6.4)

- **Site-to-Site VPN:** **$0.05 per connection-hour** ($36.50/month, two
  tunnels). Data out appears to be billed at the normal internet rate
  ($0.09/GB); the VPN page does not say so outright.
- **Direct Connect:** dedicated 1 Gbps port **$0.30/hr** ($219/month); hosted
  50 Mbps $0.03/hr ($21.90), 100 Mbps $0.06/hr ($43.80); data out to US
  locations $0.02/GB. **Carrier, partner and cross-connect charges are not
  AWS charges and are usually the larger part of the bill.**
- Sources: aws.amazon.com/vpn/pricing/, aws.amazon.com/directconnect/pricing/;
  offer files 2026-09-17.

### WAF (6.1, 6.5)

- Web ACL **$5/month**, rule or managed rule group **$1/month**, requests
  **$0.60 per million** within the default 1,500 WCU.
- Extras: Bot Control $10/month plus request charges beyond the free tier;
  account takeover / creation fraud prevention $10/month plus per-request;
  CAPTCHA $0.40 per 1,000 attempts; anti-DDoS managed rule $20/month.
- One ACL, five rules, 5 million requests: about **$13/month**.
- Source: aws.amazon.com/waf/pricing/; offer file 2026-09-14.

### Budgets and Cost Anomaly Detection (13.2)

- Budgets: **free** to monitor. Action-enabled budgets are free for the
  first two per account, then $0.10 per budget-day. Budget reports $0.01 per
  delivery.
- Cost Anomaly Detection: "no additional cost" per AWS's announcement; the
  pricing page does not state a price.
- Related: Cost Explorer API calls are $0.01 each, which matters if
  automation polls it — the tenant cost report makes three per run.
- Source: aws.amazon.com/aws-cost-management/aws-budgets/pricing/.

---

## Not confirmed

- EBS snapshot pricing.
- Whether RDS, ALB or Fargate support dedicated tenancy.
- KMS's minimum HSM count for a CloudHSM key store.
- A pricing-page statement for Cost Anomaly Detection.
