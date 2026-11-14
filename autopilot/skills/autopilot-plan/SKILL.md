---
name: autopilot-plan
description: This skill should be used when the user runs "/autopilot-plan" or "$autopilot-plan", or asks to "plan this with autopilot", "create an autopilot plan", "make an implementation plan for autonomous execution", "start planning only", "draft a plan for the runner", "plan it but don't implement yet", "turn this design doc into a plan" or "redesign this plan for autopilot". It starts the autonomous plan runner's interactive plan creation, relays every clarifying question and plan draft to the user with a recommended answer that is taken when its response time passes without a reply, and finishes with a plan file in docs/plans/ without starting implementation.
argument-hint: "[task description | --redesign <file>] [-- extra runner flags]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion, Agent
metadata:
  version: "0.1.0"
---

# Autopilot Plan

Create an implementation plan with the autonomous plan runner (ralphex) and
walk the user through every step it asks about. Stop once the plan file
exists. Do not start implementation: that is `autopilot-run`.

## Paths

Resolve paths from the directory holding this `SKILL.md`:

| Item | Path |
|---|---|
| Driver | `../autopilot/scripts/autopilot.py` |
| Driver reference | `../autopilot/references/driver.md` |
| Monitoring guide | `../autopilot/references/monitoring.md` |
| Setup gate | `../autopilot/references/preflight.md` |
| Host capabilities | `../autopilot/references/hosts.md` |
| Reviews by the other model | `../autopilot/references/reviews.md` |
| Plan check and redesign | `../autopilot/references/plan-check.md` |
| Changing a finished plan | `../autopilot/references/plan-revision.md` |
| Self-recovery | `../autopilot/references/recovery.md` |
| Final report | `../autopilot/references/report.md` |
| Rate limits | `../autopilot/references/limits.md` |
| Response times | `../autopilot/references/response-times.md` |

Read `driver.md` before the first driver call. Below, `AP` means
`python3 <absolute driver path>` (`py -3` on Windows if `python3` is
missing). If the `autopilot` directory is missing, stop and say that this
skill needs the whole `autopilot` plugin installed.

## Step 0 — Setup Gate

Before anything else, even before asking for a task, follow
`preflight.md`. When ralphex.windows or agrouter is not installed, ask to run
the `autopilot-setup` skill and do not continue until setup succeeds or the
user stops.

## Step 1 — Get the Task

Use the arguments as the task description. Text after `--` is passed to the
runner unchanged (for example `--plan-model=opus:high`).

`--redesign <file>` turns an existing document (a design doc, notes, or a
plan the runner cannot use) into a runnable plan: run `AP check-plan
<file>` for its problems and use the redesign description from "Redesigning"
in `plan-check.md` as the task. After planning, check the new plan the same
way. The original file stays untouched.

When no description was given, ask the user to describe what to build or
change, and why. Ask in one short open question, not a menu. When the
description is a single vague phrase ("fix auth"), ask one follow-up for the
expected outcome. The runner asks its own detailed questions later, so do
not interrogate.

## Step 2 — Active Sessions

When the Step 0 `doctor` report lists a running `plan` in
`active_sessions`, ask whether to resume watching it (`wait` on that
session) or stop it and start fresh.

## Step 3 — Start

```
AP plan --desc "<task description>" [--flow <id>] [-- <extra flags>]
```

Pass `--flow` when the `autopilot` skill provides one.

Planning goes through the same command as the run: agrouter via
`claude_command` (Claude setup) or the Codex wrapper (Codex setup), with
`plan_model` as the planning spec. Read it from `doctor`
(`cli_config.effective.plan_model`, its source from `AP config-summary`)
and say in the start line how the planning model is chosen: "routed by
agrouter" for `:`, or "pinned to opus:high by your global config". An
empty `plan_model` falls back to `task_model`.

Use the returned `executor` after starting: when
`executor_fallback_reason` is set, say that this session switched to Codex
and reset phase models to `:`. Use that executor for draft review too.

Tell the user in one line that planning has started, how the model is
chosen, and that the planner reads the codebase first, which can take a few
minutes. Keep the returned `session` id.

## Step 4 — Relay Until Done

Loop on `AP wait <session> --since <seq> --timeout 240` as described in
`monitoring.md`, in the way the host supports (`hosts.md`).
Post a one-line heartbeat when nothing happened ("Still planning · 4 min ·
reading pkg/store and the tests · on claude-opus-5-5 (high)"), adding the
`model` agrouter selected when the report has one. Rate limits during
planning are handled as in a run ("Planning Sessions" in `limits.md`). Act on the report:

Every prompt follows "Unattended Defaults" in `monitoring.md`: pick a
recommended answer, register it with `AP recommend`, ask in plain text with
the auto-answer time, keep monitoring. The driver applies the
recommendation when the response time passes (questions 10 min, the draft
30 min with a reminder; `response-times.md`).

**`question`** — Pick the option that best fits the task and the codebase;
read the relevant code briefly when the choice depends on it. Register it
(`AP recommend <session> --choice N --reason "..."`), then show the
planner's question with numbered options, the recommended one marked, plus
"or answer in your own words". Answer with `AP answer <session> --choice N`
or `--text "..."`.

**`draft`** — Read `waiting.draft_file`. Show a compact digest: the title,
one line of overview, the task list as a table (task number, title, step
count) and the validation commands. Give the file path for the full text.
When plan review is enabled, have the other model review the draft
(`AP review-plan <draft_file> --executor <session executor> --task "..."`,
per "Plan Review" in `reviews.md`) and show its findings with the digest.
Use the session's executor, including fallback, to choose the other model.
Honor a request to skip that review; skip an unavailable reviewer with
its reason. Log either skip and continue planning.
Check it against the task: recommend `accept`, or `revise` with the
concrete gaps found as feedback (a missing requirement, a wrong file, no
tests, the reviewer's high and medium findings). Register the
recommendation, then ask:

| Option | Answer |
|---|---|
| Accept | `--action accept` |
| Revise with feedback | ask for feedback, then `--action revise --text "..."` |
| Edit the draft directly | tell the user to edit `waiting.edited_file` (or make the edits they dictate there), then `--action edited` |
| Reject | `--action reject` (ends planning without a plan) |

**`choice`, `yesno`, `text`** — Relay the prompt verbatim with a registered
recommendation and answer with the matching flag.

**`auto-answered`** — Tell the user in one line what was chosen and why.
When they reply later with a different wish, apply it at the next prompt
(usually the draft review, as revise feedback).

**`error` / `restart`** — Show the error line and say a restart happens
automatically; earlier answers are kept by the runner.

**`failed`** — Show `last_error` and `recent_errors`, then recover per
`recovery.md` and start `AP plan` again with the same description and
`--flow` (the runner keeps earlier answers). Stop only when recovery needs
the user or 3 rounds are used.

**`done`** — Continue to Step 5.

## Step 5 — Changes to the Finished Plan

When this skill was invoked by the `autopilot` skill, skip this step: its
decide step offers revisions. Otherwise show the plan digest (title, task
table, validation commands, file path) and ask: "Plan ready. Any changes?"
with the `plan_review` response time (30 min; no reply means no changes).
On a change request, run revision rounds per `plan-revision.md` (snapshot,
direct edit or re-plan, diff, check, ask again) until the user is done.

## Step 6 — Hand Over

When this skill was invoked by the `autopilot` skill, return control to it
with the plan path and the flow id. Otherwise give a short report per
`report.md`, limited to planning: the plan path and task table, the
decisions taken (by user or by default after the response time, with reasons, from
`AP report --flow <id>`), plan revisions, problems and recoveries. End with
the next step:

```
Plan ready: docs/plans/<name>.md (5 tasks)
Run it with <run-only command in the host's form> docs/plans/<name>.md
```

Do not start implementation from this skill.

## Rules

- Answers come from the user, or from the registered recommendation after
  its response time without a reply. When the user says "you decide", answer with
  the recommendation using `--by agent --reason "..."`.
- Keep one planning session per task. Never start a second `plan` while one
  is active for the same description.
- To cancel, run `AP stop <session>` and confirm.
