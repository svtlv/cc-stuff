---
name: autopilot
description: This skill should be used when the user runs "/autopilot" or "$autopilot", or asks to "autopilot this", "plan and implement this autonomously", "do the full autopilot run", "take this from idea to code", "plan it, explain it, then build it", "run the whole plan-and-execute flow" or "build this while I'm away". It orchestrates the full flow on the autonomous plan runner and model router - interactive planning with the user, a summary and explanation of the plan, the decision to implement or stop, then a supervised implementation run with automatic restarts, self-recovery and live status. It keeps going when the user is away (recommended answers after a response time: 10 minutes for questions, an hour to decide on implementation) and always ends with a short report: first every decision made without the user, then the goal and what was done.
argument-hint: "[task description | path to an existing plan file] [-- extra runner flags]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion, Skill, Agent, CronCreate, CronDelete
metadata:
  version: "0.3.0"
---

# Autopilot

Take a task from description to implemented code with the autonomous plan
runner (ralphex) and model router (agrouter): plan it with the user, explain
the plan, ask whether to build it, then build it under supervision.

```
 task ─► autopilot-plan ─► explain-summarize ─► implement? ─┬─► autopilot-run ─► report
            (Q&A, drafts)     (plan report)                 └─► stop: plan kept
 plan.md ─────────────────────────────────────────────────────► autopilot-run
```
**Note:** this skill chains its sibling skills. A file path as the argument goes
to the run skill, which checks the file and redesigns it first when the
runner cannot use it.

## Sibling Skills

Resolve paths from the directory holding this `SKILL.md`. Read each skill's
file in full when its step begins and follow it; that works in every agent
host, with or without a way to load skills by name (`references/hosts.md`):

| Step | Instructions |
|---|---|
| Planning | `../autopilot-plan/SKILL.md` |
| Running | `../autopilot-run/SKILL.md` |
| Setup | `../autopilot-setup/SKILL.md` |
| Driver and monitoring | `references/driver.md`, `references/monitoring.md` |
| Host capabilities | `references/hosts.md` |
| Reviews by the other model | `references/reviews.md` |
| Plan check and redesign | `references/plan-check.md` |
| Changing a finished plan | `references/plan-revision.md` |
| Setup gate | `references/preflight.md` |
| Self-recovery | `references/recovery.md` |
| Final report | `references/report.md` |
| Rate limits | `references/limits.md` |
| Response times | `references/response-times.md` |

## Working Unattended

The flow keeps going when the user steps away. Read "Unattended Defaults"
in `monitoring.md` before Step 2:

- every question carries a recommended answer, taken when its response
  time passes without a reply (`references/response-times.md`): 10 min for
  planner questions, 30 min for the draft, **60 min to decide whether to
  implement**; reminders before the long ones, and "wait" buys more time;
- failures are diagnosed and fixed per `recovery.md`, then the run starts
  again, up to 3 recovery rounds;
- rate limits are waited out (ralphex retries in place; the driver waits
  until the reset), and only a reset hours away needs a decision, per
  `limits.md`;
- every critical decision lands in the decision log, and the flow always
  ends with the full report (Step 6).

Only installing software, entering credentials and logging in wait for the
user without a time limit.

Use one flow id for the whole run: take `flow` from the first `plan` (or
`run`) result and pass `--flow <id>` to every later `plan`/`run`, including
recovery restarts.

## Step 0 — Setup Gate

Before anything else, even before asking for a task, follow
`references/preflight.md`. When ralphex.windows or agrouter is not
installed, ask to run the `autopilot-setup` skill and do not continue until
setup succeeds or the user stops. After setup, resume this flow with the
original arguments. The planning and run steps below skip their own gate,
since this one already ran.

## Step 1 — Route the Input

| Argument | Path |
|---|---|
| A path to an existing file | Hand it to the run: follow `autopilot-run` from its Step 1, which checks the file (`references/plan-check.md`). A ready plan goes straight to the run (Step 5). When it gets redesigned, come back for Steps 3–4 with the new plan, since the user has not seen it yet, then Step 5. |
| Text | Task description for Step 2. |
| Nothing | Ask the user to describe what to build or change, in one open question. |

Text after `--` is passed through: planning flags (such as
`--plan-model=...`) to planning, execution flags (such as `--worktree`,
`--tasks-only`) to the run.

## Step 2 — Plan

Follow `autopilot-plan` Steps 3–4 with the description. Relay every question
and draft to the user with a registered recommendation and its response
time.
Planning ends with a `plan_file`, or with a rejected draft: then skip to
Step 6 and report why it ended.

## Step 3 — Explain the Plan

Present the plan with the code-explainer plugin's `explain-summarize` skill:
a short linked summary followed by a detailed walkthrough with diagrams.
Find it as "Finding explain-summarize" in `references/hosts.md` describes
and run it on the plan path.

When the skill is not available, write a compact report yourself: a
five-bullet summary, a table of tasks (number, title, files touched), one
ASCII diagram of how the parts connect with a **Note:** under it, and the
risks. Then mention once that installing the code-explainer plugin (from
the same marketplace, or its skills copied next to these) gives the full
report.

## Step 4 — Decide

This is the user's main checkpoint: give time to read the plan and the
explanation. Ask one question in plain text, recommending **Implement now**
(or Revise, when the explanation surfaced a concrete problem), with the
deadline as a clock time ("Asking" in `references/response-times.md`).
Start `AP ask-timer start --id <flow>-decide --decision decide` (60 min,
reminder 15 min before), loop on `ask-timer wait`, `cancel` it on a reply,
and act on the recommendation when it expires. Log the outcome on the plan
session: `AP log <plan-session> --kind decision --text "<choice>" --reason
"<why>"`, adding `--by user` on a reply, or `--by auto --question "Implement
the plan now?" --waited <N>` when the timer expired.

| Option | Effect |
|---|---|
| Implement now | Go to Step 5. |
| Implement in a separate worktree | Step 5 with `--worktree` added. |
| Revise the plan | Ask what to change, then run one revision round per `references/plan-revision.md` (direct edit for small changes, re-plan for a new approach), show what changed, and ask this question again with a fresh timer. Repeat as often as the user wants. |
| Stop here | Go to Step 6. The report names the plan and the command to run it later, in the host's form ("Naming the Commands" in `references/hosts.md`). |

Implementing, whether chosen or taken by default, counts as consent to
start the run; do not ask again in the run's preflight.

## Step 5 — Run

Follow `autopilot-run` Steps 2–4 with the plan and `--flow <id>`. Stay
with the run until it ends: one fixed line per milestone (each task's start
with its model and its finish with the commit, reviews, restarts, limits),
nothing on heartbeats, failures recovered per `recovery.md`. The user can
ask about the run or have it changed at any time; this chat stays in charge
per `references/control.md` (safe stop, resume, watchdog, reattach).

## Step 6 — Final Report

After the report, `AP mark-reported <run-session>` and delete the watchdog
cron job. Always, whatever happened: done, failed after recovery, stopped, or ended
at the plan stage. Follow `references/report.md` with `AP report --flow
<id>`. The report has two main parts: first every decision made without
the user (`without_user`: timeouts, agent and autopilot choices,
recoveries), then the goal and what was done (summarize on the branch
changes, the task table). Add only the open items that need the user.

## Parallel Work

Delegate to subagents when the host supports them and the work is
independent of the user's next answer:

- During planning, a read-only subagent can map the code areas the
  description touches, so draft reviews come with context.
- During the run, a read-only subagent can diagnose a failure while the
  monitor keeps going (see `references/recovery.md`).
- For the final report, a read-only subagent can write the summary of
  a large branch diff while the decision tables are assembled.

Keep subagents read-only while the runner works in the repository. Without
subagent support, do the same work inline.

## Rules

- Each sibling skill's rules apply during its step.
- Ask before starting implementation; without a reply within the `decide`
  time (60 min by default), implementing is the default.
- Keep updates short: a line or two per event, tables for summaries.

## Output Language

Write all text for the user in ASD-STE100 Simplified Technical English:
chat messages, questions, summaries, reports and the Note under each
diagram. Do not apply it to code, identifiers, file paths, commands, quotes
or error messages. Keep them exactly as they are.

- Use one word for one meaning. Use simple, common words and keep the
  same technical name for the same thing.
- Write sentences of 20 words or fewer for instructions and 25 words or
  fewer for descriptions. Write one instruction in each sentence.
- Use the active voice and simple tenses: present, simple past and simple
  future. Use the imperative for instructions.
- Keep paragraphs to six sentences or fewer. Use lists and tables for
  complex information.
- Use "the" and "a". Do not remove words to make text shorter. Do not use
  more than three nouns together.
- When the user writes in a different language, use that language with
  the same short, simple rules.
