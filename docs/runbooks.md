# Runbooks

**Written for:** whoever has just been paged. Causes are ordered most
likely first; skip to *If it escalates to you*.

**GENERATED FILE — do not edit.** The source is
[`runbooks/catalog.yaml`](../runbooks/catalog.yaml), and this is
regenerated whenever the response engine is packaged. A hand edit here
would be lost, and worse, would describe behaviour the platform does not
have.

---

## How the automation behaves, for all of these

Every alarm runs the same four stages. **Stage 1 always runs and only
reads.** Stages 2 and 3 act, and each is followed by a wait and a
re-check of the original alarm before anything escalates.

| | |
|---|---|
| A remediation ran and the alarm still breaches | escalate to the next stage — the same action is **never retried** |
| The same action failed **2×** on one resource | stop attempting it |
| The same action *fired* **3×** in an hour, **including successes** | circuit breaker — repeated repair is masking a fault |
| More than **20** automated actions in this account in an hour | rate limit |
| A stage 3 action on a resource with no verified redundancy | refused |
| The account's kill switch is not `enabled` | nothing acts; gathering and paging still happen |

Waits before re-checking, so a slow remediation is not mistaken for a failed one:

| Action | Wait |
|---|---|
| `clear_log_files` | 120s |
| `cycle_ecs_task` | 300s |
| `restart_service` | 180s |
| `scale_out` | 600s |
| *anything else* | 300s |

**If a rail refused the action, that refusal is the finding.** Resolve the condition rather than clearing the rail.

---

## Contents

- [ec2-disk-high](#ec2-disk-high) — Instance
- [ec2-memory-high](#ec2-memory-high) — Instance
- [ec2-cpu-high](#ec2-cpu-high) — Instance
- [ec2-status-check-failed](#ec2-status-check-failed) — Instance
- [rds-connections-high](#rds-connections-high) — DBInstance
- [rds-free-storage-low](#rds-free-storage-low) — DBInstance
- [ecs-cpu-high](#ecs-cpu-high) — Service
- [ecs-memory-high](#ecs-memory-high) — Service
- [ecs-running-vs-desired](#ecs-running-vs-desired) — Service
- [ecs-task-restart-rate](#ecs-task-restart-rate) — Service
- [alb-5xx-rate](#alb-5xx-rate) — LoadBalancer
- [lambda-error-rate](#lambda-error-rate) — Function

---

# AWS::EC2::Instance

## ec2-disk-high

A filesystem on this instance is close to full. When it fills, writes fail — usually the application first, then logging, which is how you lose the evidence of what happened.

**Common causes**, most likely first:

- An application log that is not being rotated, or rotated but not compressed. Most common by a distance.
- A log level left at DEBUG after a deployment.
- Core dumps or crash artefacts from a process that keeps restarting.
- A batch job writing temporary files it does not clean up.
- Container image layers on a Docker host, which look like disk use from nowhere.

**Automation will act.**

| Stage | What happens |
|---|---|
| 1 — gather | Disk usage per mount, so you know WHICH filesystem rather than that one of them is full. Plus the standard set — recent changes matters here because a log-level change is a deployment, and it is the single most likely cause. |
| 2 — non-destructive | Delete ROTATED logs only — *.gz, *.1, *.old older than three days — and vacuum journald to 200M. It never touches a live log file: truncating one breaks the writing process's file handle and destroys the evidence you are about to need. |
| 3 — mildly disruptive | Nothing. There is no safe disruptive action for a full disk that clearing rotated logs did not fix. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Clearing rotated logs did not free enough, so something large and current is growing. Look at the disk_usage_by_mount output for which mount, then find the biggest recent file on it. If the growth is a live application log, the fix is the log level or the application, not the disk.

## ec2-memory-high

Memory use on this instance is sustained above the threshold. The next thing that happens is usually the OOM killer choosing a process, and it rarely chooses the one you would have.

**Common causes**, most likely first:

- A genuine leak in a long-running process. Grows monotonically and never recovers.
- A workload change — bigger requests, more concurrency — after a deployment.
- A JVM or runtime heap sized larger than the instance.
- Page cache counted as used, which is normal and not a problem.

**Automation will act.**

| Stage | What happens |
|---|---|
| 1 — gather | The processes using the most memory, so the dossier names what is growing rather than reporting that something is. Plus the standard set. |
| 2 — non-destructive | Nothing. There is no non-disruptive way to reclaim memory from a process that wants it. |
| 3 — mildly disruptive | Restart the service named by the instance's platform-managed-service tag, but ONLY if the instance has healthy peers behind a load balancer. If it is the last healthy target, the restart is refused — that would be an outage rather than a remediation. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Either the restart was refused for lack of redundancy, or it ran and memory climbed again. If it climbed again within the settle period you are looking at a fast leak; capture a heap dump before restarting anything else, because the next restart destroys the evidence.

## ec2-cpu-high

Sustained high CPU. Worth knowing; not necessarily worth acting on. A machine at 95% may be working exactly as intended.

**Common causes**, most likely first:

- Legitimate load. Check this first and check it honestly.
- A runaway process or an infinite retry loop after a dependency failed.
- A deployment that changed the work done per request.
- A batch job overlapping with interactive traffic.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | The standard set only. recent_changes is the useful one — CPU that moves usually moves because something shipped. |
| 2 — non-destructive | Nothing. |
| 3 — mildly disruptive | Nothing, deliberately. High CPU is as likely to be load as a fault, and restarting a busy service to fix busyness is how automation turns a slow day into an outage. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Every alarm here escalates — this runbook only gathers. Start with whether the load is real. If it is, this is a capacity conversation, not an incident.

## ec2-status-check-failed

AWS cannot reach the instance, or the instance's own status check is failing. This is hypervisor-level: the machine is unreachable, not merely busy.

**Common causes**, most likely first:

- Underlying host failure. AWS usually retires the instance.
- Kernel panic or a failed boot after a patch.
- Network configuration that removed the instance's own connectivity.
- The instance was stopped by something outside the platform.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | The standard set. Note that most gather steps need the instance to respond, so expect thin output — that thinness is itself consistent with the alarm. |
| 2 — non-destructive | Nothing. |
| 3 — mildly disruptive | Nothing. The remedies are all restart-shaped, and SSM cannot reach an instance that is failing its status check, so the automation could not perform them even if permitted. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Check whether this is a system status check (AWS's problem, usually resolved by stop/start onto new hardware) or an instance status check (the guest's problem). The AWS console distinguishes them; the alarm does not.

---

# AWS::RDS::DBInstance

## rds-connections-high

Connections to this database are near the instance maximum. When it is reached, new connections are refused and the application sees errors that look nothing like a database problem.

**Common causes**, most likely first:

- A connection pool sized larger than the database allows, multiplied by more application instances than when it was sized.
- Connections not returned to the pool — a leak in error handling, where the failure path forgets to close.
- A deployment that increased pool size or task count.
- Long-running queries holding connections while new work arrives.
- max_connections lower than expected, because on RDS it is derived from instance memory unless set explicitly.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | Connection count over time, so you can tell a step change from a gradual climb. A step change points at a deployment; a climb points at a leak. Plus the standard set. |
| 2 — non-destructive | Nothing. Doc 07 lists "release stuck database connections" as a stage 2 action, and the platform does not do it: killing connections requires credentials to the database that the platform deliberately does not hold, and identifying a genuinely stuck connection rather than a slow one is not something we can do confidently. Guessing wrong disconnects a real user mid-transaction. |
| 3 — mildly disruptive | Nothing. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Look at the connection_trend shape first. A step change at a deployment time is a pool configuration problem and the fix is in the application. A steady climb with no plateau is a leak. If you need headroom immediately, max_connections can be raised in the parameter group — but that is a change, and it needs a change record.

## rds-free-storage-low

Free storage on this database is below the tier threshold. RDS stops accepting writes when it runs out, and the recovery from that is slower than you would like.

**Common causes**, most likely first:

- Normal data growth that nobody projected.
- Binary or transaction logs retained longer than intended.
- A large temporary table or an index rebuild in progress.
- A failed or paused replication slot holding WAL segments on PostgreSQL. Grows fast and does not stop.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | The storage trend, because the slope tells you how long you have — a cliff and a gentle decline need different responses and the alarm cannot distinguish them. |
| 2 — non-destructive | Nothing. Doc 07 lists "expand the volume" and the platform refuses it: AWS does not permit SHRINKING allocated RDS storage, by any means, ever. An automated expansion raises the floor of the customer's bill permanently, and a runaway log would do it repeatedly. It is the one genuinely irreversible action on doc 07's list. |
| 3 — mildly disruptive | Nothing. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Check the trend slope for how much time you have. If storage autoscaling is enabled, AWS may handle it — and you should still find out why. If it is a replication slot, resolving the slot frees the space without spending anything.

---

# AWS::ECS::Service

## ecs-cpu-high

Sustained CPU across the tasks of this service. Unlike an EC2 instance, a service has a defined way to get more capacity, so this one is sometimes actionable automatically.

**Common causes**, most likely first:

- More traffic than the current task count can serve.
- A deployment that increased the work per request.
- A task-level CPU limit set lower than the workload needs.
- A dependency slowing down, so tasks spend longer per request.

**Automation will act.**

| Stage | What happens |
|---|---|
| 1 — gather | CPU per task, which separates "all tasks are busy" (capacity) from "one task is pegged" (a stuck task). Plus the standard set. |
| 2 — non-destructive | Add one task — but ONLY if the service has a scaling policy that can also remove one. Reversible is not the same as self-reversing: on a fixed desired count the automation would add capacity that nothing ever takes away, which is reversible in principle and permanent in practice until somebody reads the bill. |
| 3 — mildly disruptive | Nothing. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Either the service has no scale-in policy — in which case adding capacity is a decision for a human who can also remove it — or a task was added and CPU stayed high. The second usually means the bottleneck is downstream, not here.

## ecs-memory-high

A task is approaching its memory limit. ECS kills a task that exceeds it, so this usually precedes a restart you did not ask for.

**Common causes**, most likely first:

- A leak in the container. Grows monotonically across the task's life.
- A memory limit set from a guess rather than a measurement.
- A workload change after a deployment.
- A caching layer inside the application with no upper bound.

**Automation will act.**

| Stage | What happens |
|---|---|
| 1 — gather | Memory per task, so the dossier can say whether one task is leaking or all of them are near the limit. Those need different responses. |
| 2 — non-destructive | Nothing. |
| 3 — mildly disruptive | Stop ONE task, and only if the service has at least two running. The scheduler replaces it, memory returns to baseline, and the service keeps serving throughout. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

If cycling was refused, the service has fewer than two running tasks and cycling would be an outage. If it ran and memory climbed again, the leak is fast — capture what you need from a running task before anything cycles it again, because the circuit breaker will stop after three and you want the evidence from before that.

## ecs-running-vs-desired

Fewer tasks are running than the service wants. ECS is already trying to start them and not succeeding.

**Common causes**, most likely first:

- The container exits immediately — a bad image, a missing environment variable, a failed migration on boot.
- Health checks failing, so ECS kills tasks it just started.
- No capacity to place tasks on — the cluster is full, or Fargate has no room in the subnets. (Note for anyone editing this file: a plain YAML list item cannot contain a colon followed by a space. It parses as a mapping and the whole catalog fails to load, which fails the packaging step rather than anything subtle — but it is an easy thing to reintroduce while writing prose.)
- An image that cannot be pulled, usually a registry permission.
- A task role missing a permission the container needs at startup.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | Stopped task reasons and recent service events. This IS the answer almost every time — ECS records why it stopped a task, and the reason string usually names the problem outright. |
| 2 — non-destructive | Nothing. |
| 3 — mildly disruptive | NOTHING, and this is deliberate rather than an omission. Cycling a task here would race the scheduler that is already trying to start tasks, and deepen the deficit the alarm is reporting. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Read ecs_stopped_task_reasons first and believe it. "Essential container exited" with a non-zero code is the application; "unable to place task" is capacity; "CannotPullContainerError" is the registry. Each has a different owner.

## ecs-task-restart-rate

Tasks are restarting more often than the threshold allows. The service may look healthy from outside while churning underneath.

**Common causes**, most likely first:

- A crash loop — the container starts, fails, and ECS restarts it.
- Health check timing too aggressive for the application's startup.
- Memory limits killing tasks under normal load.
- A dependency that is intermittently unavailable at startup.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | The same as above: stopped task reasons and service events. The exit codes tell you whether the container chose to exit or was killed. |
| 2 — non-destructive | Nothing. |
| 3 — mildly disruptive | Nothing. This alarm IS the circuit breaker's scenario written as an alarm — tasks are already cycling too often, so cycling one more is indistinguishable from the fault it is reporting. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Exit code 137 means the task was killed, usually for memory. A non-zero application exit code means the container chose to stop, and the reason is in its logs. If restarts began at a deployment, that is your answer.

---

# AWS::ElasticLoadBalancingV2::LoadBalancer

## alb-5xx-rate

The load balancer itself is returning 5xx — not the targets behind it. Users are seeing errors that the application never saw, because their requests did not reach it.

**Common causes**, most likely first:

- No healthy targets. The commonest cause by far; check this first.
- Targets that accept a connection and then close it before responding.
- Request or response headers exceeding the ALB's limits.
- Idle timeout shorter than the application's response time, so the ALB gives up on a request the target is still working on.
- A target group whose health check path stopped existing after a deployment.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | The standard set. related_health matters more than usual here: if tg-no-healthy-hosts is also firing, that is the whole story and this alarm is a symptom of it. |
| 2 — non-destructive | Nothing. Everything that fixes an ALB 5xx is on the targets, not on the load balancer, and the automation cannot know which target group belongs to which application. |
| 3 — mildly disruptive | Nothing. Doc 07 lists "drain and replace an instance behind a load balancer" and the platform refuses it: draining deregisters and replacing terminates, which are two of the verbs CLAUDE.md names, and the recovery story is hoping the replacement comes up healthy. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Check healthy target count first — if it is zero, this is a deployment or a health check problem and the 5xx is a consequence. If targets are healthy, compare the ALB 5xx count with the TARGET 5xx count: if only the ALB is counting them, the requests are failing before they arrive, which points at timeouts or header limits rather than at the application.

---

# AWS::Lambda::Function

## lambda-error-rate

This function is failing on a percentage of its invocations. The threshold is a RATE, not a count, so it means the same thing on a function invoked twice a day and one invoked ten thousand times a minute.

**Common causes**, most likely first:

- A deployment. Overwhelmingly the most likely, and the one the gather stage answers directly.
- A downstream dependency failing or throttling.
- Timeouts, which count as errors — often after a dependency slowed down rather than failed.
- Permission changes on the execution role.
- Malformed input from a new producer.

**Automation will NOT act on this — it gathers only.**

| Stage | What happens |
|---|---|
| 1 — gather | The actual exceptions from recent invocations, not just the count. Plus the standard set — recent_changes answers this alarm more often than any other in the catalog. |
| 2 — non-destructive | Nothing. Every remedy for a failing function is a code or configuration change, and those belong to a human and a change record. |
| 3 — mildly disruptive | Nothing. |
| 4 — page | With everything stages 1–3 learned attached. |

### If it escalates to you

Start with recent_changes. If something deployed in the last thirty minutes, that is almost certainly it, and rolling back is faster than diagnosing. If nothing deployed, read the exceptions — a dependency error and a timeout look similar in the metrics and nothing alike in the logs.
Note the alarm only fires above the minimum invocation count in 06a, so this is not one error out of two. There is real volume behind it.

---

## Every way this can end

The engine has one exit. Every path goes through it, and it pages unless
the state is one that means the thing is genuinely fine. **A state it
does not recognise also pages** — an unanticipated outcome is never
treated as a resolved one.

| State | What it means | Who hears about it |
|---|---|---|
| `resolved` | A remediation ran and the alarm cleared | Nobody. Change record only. |
| `resolved-before-action` | The alarm cleared on its own before anything ran | Nobody. |
| `resolved-after-settle` | The sweep re-checked and the alarm had cleared | Nobody. |
| `awaiting-settle` | An action ran; too slow to verify inline | **Low urgency.** The sweep revalidates. |
| `unresolved-after-settle` | The settle period elapsed and it is still breaching | **Page.** The fix did not fix. |
| `unresolved` | Every permitted action ran, still breaching | **Page.** |
| `blocked` | A safety rail refused | **Page.** The refusal is the finding. |
| `no-runbook` | No entry above for this alarm | **Low urgency.** The alarm paged on its own. |
| `bad-event` | The engine was invoked with an unusable event | **Page.** Platform fault. |
| `engine-error` | The engine itself raised | **Page**, and the Lambda Errors alarm fires too. |
| *anything else* | A state the engine does not know | **Page.** This is the else. |

### Why the low-urgency two are low urgency

Both report a gap in the *platform*, not an unattended incident.
`no-runbook` means the alarm already paged on its own and what is missing
is the dossier. `awaiting-settle` means an action ran and the sweep is
about to check it. Paging twice for one incident is how somebody ends up
writing a notification rule that then swallows the real pages too.

### The sweep

Only `clear_log_files` settles fast enough to verify inside the same
invocation. Everything else is parked in the remediation history table
and re-checked by `platform-response-sweep`, which runs every two
minutes. So for most remediations the verdict arrives a few minutes after
the action, not immediately.

If the sweep stops running, remediations park and are never re-checked.
That failure is silent, so it has its own alarm:
`platform-response-sweep-stalled`, which treats missing data as
breaching. **If you see that one, assume any recent automated action is
unverified.**

A revalidation the engine could not *read* — the alarm query itself
failed — is retried up to three times before it pages regardless. An
alarm that is genuinely still breaching does not wait for three tries; it
pages on the first.

