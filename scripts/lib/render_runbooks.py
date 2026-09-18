#!/usr/bin/env python3
"""Render runbooks/catalog.yaml as docs/runbooks.md.

    render_runbooks.py <catalog.yaml> <out.md>

The catalog is the source and this is the reading surface. They are different
jobs: the catalog interleaves tier2 prose with engineering comments, YAML
structure and action parameters, because it has to drive the engine. None of
that helps someone woken at 3am.

GENERATED, not written. A hand-maintained copy would be wrong within two edits,
and wrong in the worst direction — a runbook document that describes a
remediation the platform no longer performs is worse than no document, because
it is believed. Regenerated at packaging, so drift is not possible.
"""
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python -m pip install --user pyyaml")


def para(text):
    """Collapse a YAML folded block back into paragraphs."""
    if not text:
        return ""
    return "\n\n".join(p.strip() for p in text.split("\n\n") if p.strip())


def main():
    cat_path, out_path = sys.argv[1], sys.argv[2]
    with open(cat_path, encoding="utf-8") as fh:
        cat = yaml.safe_load(fh)

    out = []
    w = out.append

    w("# Runbooks")
    w("")
    w("**Written for:** whoever has just been paged. Causes are ordered most")
    w("likely first; skip to *If it escalates to you*.")
    w("")
    w("**GENERATED FILE — do not edit.** The source is")
    w("[`runbooks/catalog.yaml`](../runbooks/catalog.yaml), and this is")
    w("regenerated whenever the response engine is packaged. A hand edit here")
    w("would be lost, and worse, would describe behaviour the platform does not")
    w("have.")
    w("")

    fp = cat.get("failure_policy") or {}
    settle = cat.get("settle_seconds") or {}

    w("---")
    w("")
    w("## How the automation behaves, for all of these")
    w("")
    w("Every alarm runs the same four stages. **Stage 1 always runs and only")
    w("reads.** Stages 2 and 3 act, and each is followed by a wait and a")
    w("re-check of the original alarm before anything escalates.")
    w("")
    w("| | |")
    w("|---|---|")
    w("| A remediation ran and the alarm still breaches | escalate to the next "
      "stage — the same action is **never retried** |")
    w(f"| The same action failed **{fp.get('failed_attempt_limit', 2)}×** on one "
      "resource | stop attempting it |")
    w("| The same action *fired* **3×** in an hour, **including successes** | "
      "circuit breaker — repeated repair is masking a fault |")
    w("| More than **20** automated actions in this account in an hour | rate "
      "limit |")
    w("| A stage 3 action on a resource with no verified redundancy | refused |")
    w("| The account's kill switch is not `enabled` | nothing acts; gathering "
      "and paging still happen |")
    w("")
    w("Waits before re-checking, so a slow remediation is not mistaken for a "
      "failed one:")
    w("")
    w("| Action | Wait |")
    w("|---|---|")
    for k, v in sorted(settle.items()):
        if k == "default":
            continue
        w(f"| `{k}` | {v}s |")
    w(f"| *anything else* | {settle.get('default', 300)}s |")
    w("")
    w("**If a rail refused the action, that refusal is the finding.** Resolve "
      "the condition rather than clearing the rail.")
    w("")

    # Contents
    w("---")
    w("")
    w("## Contents")
    w("")
    for rtype, rbs in (cat.get("runbooks") or {}).items():
        short = rtype.split("::")[-1]
        for aid in (rbs or {}):
            w(f"- [{aid}](#{aid}) — {short}")
    w("")

    for rtype, rbs in (cat.get("runbooks") or {}).items():
        w("---")
        w("")
        w(f"# {rtype}")
        w("")
        for aid, rb in (rbs or {}).items():
            t = rb.get("tier2") or {}
            w(f"## {aid}")
            w("")
            if t.get("what_this_means"):
                w(para(t["what_this_means"]))
                w("")

            if t.get("common_causes"):
                w("**Common causes**, most likely first:")
                w("")
                for c in t["common_causes"]:
                    w(f"- {' '.join(str(c).split())}")
                w("")

            s2 = [a["action"] for a in (rb.get("stage_2") or [])]
            s3 = [a["action"] for a in (rb.get("stage_3") or [])]
            verdict = ("**Automation will act.**" if (s2 or s3)
                       else "**Automation will NOT act on this — it gathers only.**")
            w(verdict)
            w("")
            w("| Stage | What happens |")
            w("|---|---|")
            w(f"| 1 — gather | {' '.join(str(t.get('stage_1_gathers','')).split())} |")
            w(f"| 2 — non-destructive | {' '.join(str(t.get('stage_2_will','')).split())} |")
            w(f"| 3 — mildly disruptive | {' '.join(str(t.get('stage_3_will','')).split())} |")
            w("| 4 — page | With everything stages 1–3 learned attached. |")
            w("")

            if t.get("if_it_escalates_to_you"):
                w("### If it escalates to you")
                w("")
                w(para(t["if_it_escalates_to_you"]))
                w("")

    w("---")
    w("")
    w("## Every way this can end")
    w("")
    w("The engine has one exit. Every path goes through it, and it pages unless")
    w("the state is one that means the thing is genuinely fine. **A state it")
    w("does not recognise also pages** — an unanticipated outcome is never")
    w("treated as a resolved one.")
    w("")
    w("| State | What it means | Who hears about it |")
    w("|---|---|---|")
    w("| `resolved` | A remediation ran and the alarm cleared | Nobody. Change "
      "record only. |")
    w("| `resolved-before-action` | The alarm cleared on its own before anything "
      "ran | Nobody. |")
    w("| `resolved-after-settle` | The sweep re-checked and the alarm had "
      "cleared | Nobody. |")
    w("| `awaiting-settle` | An action ran; too slow to verify inline | **Low "
      "urgency.** The sweep revalidates. |")
    w("| `unresolved-after-settle` | The settle period elapsed and it is still "
      "breaching | **Page.** The fix did not fix. |")
    w("| `unresolved` | Every permitted action ran, still breaching | **Page.** |")
    w("| `blocked` | A safety rail refused | **Page.** The refusal is the "
      "finding. |")
    w("| `no-runbook` | No entry above for this alarm | **Low urgency.** The "
      "alarm paged on its own. |")
    w("| `bad-event` | The engine was invoked with an unusable event | "
      "**Page.** Platform fault. |")
    w("| `engine-error` | The engine itself raised | **Page**, and the Lambda "
      "Errors alarm fires too. |")
    w("| *anything else* | A state the engine does not know | **Page.** This is "
      "the else. |")
    w("")
    w("### Why the low-urgency two are low urgency")
    w("")
    w("Both report a gap in the *platform*, not an unattended incident.")
    w("`no-runbook` means the alarm already paged on its own and what is missing")
    w("is the dossier. `awaiting-settle` means an action ran and the sweep is")
    w("about to check it. Paging twice for one incident is how somebody ends up")
    w("writing a notification rule that then swallows the real pages too.")
    w("")
    w("### The sweep")
    w("")
    w("Only `clear_log_files` settles fast enough to verify inside the same")
    w("invocation. Everything else is parked in the remediation history table")
    w("and re-checked by `platform-response-sweep`, which runs every two")
    w("minutes. So for most remediations the verdict arrives a few minutes after")
    w("the action, not immediately.")
    w("")
    w("If the sweep stops running, remediations park and are never re-checked.")
    w("That failure is silent, so it has its own alarm:")
    w("`platform-response-sweep-stalled`, which treats missing data as")
    w("breaching. **If you see that one, assume any recent automated action is")
    w("unverified.**")
    w("")
    w("A revalidation the engine could not *read* — the alarm query itself")
    w("failed — is retried up to three times before it pages regardless. An")
    w("alarm that is genuinely still breaching does not wait for three tries; it")
    w("pages on the first.")
    w("")

    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")

    count = sum(len(v) for v in (cat.get("runbooks") or {}).values())
    acting = sum(1 for v in (cat.get("runbooks") or {}).values()
                 for rb in v.values() if rb.get("stage_2") or rb.get("stage_3"))
    print(f"  rendered {count} runbook(s), {acting} of which act")


if __name__ == "__main__":
    main()
