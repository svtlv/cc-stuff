---
name: autopilot-plan
description: This skill should be used when the user runs "/autopilot-plan" or "$autopilot-plan", or asks to "plan this with autopilot", "create an autopilot plan", "make an implementation plan for autonomous execution", "start planning only", "draft a plan for the runner", "plan it but don't implement yet", "turn this design doc into a plan" or "redesign this plan for autopilot". It starts the autonomous plan runner's interactive plan creation, relays every clarifying question, plan draft and question of the runner's own plan review (which reviewer, which findings to apply) to the user in order with a recommended answer that is taken when its response time passes without a reply, and finishes with a reviewed plan file in docs/plans/ without starting implementation. It also relays the runner's review of an existing plan file.
argument-hint: "[task description | --redesign <file> | --review <file>] [-- extra runner flags]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion, Agent
metadata:
  version: "0.9.0"
---

# Autopilot Plan

Create an implementation plan with the autonomous plan runner (ralphex) and
walk the user through every step it asks about. Ralphex leads: it asks the
questions, writes the drafts, writes the plan file and reviews it. This
chat is the watcher between ralphex and the user. Stop once the reviewed
plan file exists. Do not start implementation: that is `autopilot-run`.

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

`--review <file>` reviews an existing plan file with the runner's plan
review only: start it with `AP plan-review <file>` in Step 3 and relay it
from Step 4. Ralphex asks no planner questions and makes no drafts here.

When no description was given, ask the user to describe what to build or
change, and why. Ask in one short open question, not a menu. When the
description is a single vague phrase ("fix auth"), ask one follow-up for the
expected outcome. The runner asks its own detailed questions later, so do
not interrogate.

## Step 2 — Active Sessions

When the Step 0 `doctor` report lists a running `plan` in
`active_sessions`, ask whether to resume watching it (`AP next` on that
session) or stop it and start fresh.

## Step 3 — Start

```
AP plan --desc "<task description>" [--flow <id>] [-- <extra flags>]
```

Pass `--flow` when the `autopilot` skill provides one. For `--review
<file>`, start `AP plan-review <file> [--flow ID]` instead.

When the result has `plan_review: false`, the installed runner is older
than v1.7.0-windows.4 and has no plan review. Say this once in the start
line. Offer `autopilot-setup` to upgrade it after planning. Do not review
the plan in another way.

Planning goes through the same command as the run: agrouter via
`claude_command` (Claude setup) or the Codex wrapper (Codex setup), with
`plan_model` as the planning spec. Read it from `doctor`
(`cli_config.effective.plan_model`, its source from `AP config-summary`)
and say in the start line how the planning model is chosen: "routed by
agrouter" for `:`, or "pinned to opus:high by your global config". An
empty `plan_model` falls back to `task_model`.

Use the returned `executor` after starting: when
`executor_fallback_reason` is set, say that this session switched to Codex
and reset phase models to `:`. Under Codex, ralphex offers no External
reviewer: the plan review is a same-agent review.

Tell the user in one line that planning has started, how the model is
chosen, and that the planner reads the codebase first, which can take a few
minutes. Keep the returned `session` id.

## Step 4 — Relay Until Done

This chat is a proxy between the user and ralphex. Ralphex leads; the chat
relays its prompts in order and stays free for the user at all times.

```
AP next ──► PROMPT ──► show it + one recommendation ──► user reply ──► AP answer ──► AP next
   │                         (driver auto-answers at the deadline)
   ├──► update lines ──► post them ──► AP next
   ├──► DONE ──► Step 5
   └──► FAILED ──► recover ──► AP plan again

ralphex prompts, in order:
  questions ─► drafts ─► plan file ─► which reviewer? ─► findings ─► which to apply? ─┐
                                            ▲              (planner edits the plan)  │
                                            └────────────── again, max 5 rounds ◄────┘
```
**Note:** one `AP next` is in flight at a time. The chat does nothing else in
parallel: no extra waits, no reviews of its own and no timers during
planning. The plan review is a ralphex prompt like any other.

Run `AP next <session>` as "The Loop" in `monitoring.md` describes for the
host (Claude Code: in the background, with the Bash timeout at 7200000 ms).
It prints short text and tracks the event counter itself. Post its update
lines as is. Read nothing else from the session to know the state. Rate
limits during planning are handled as in a run ("Planning Sessions" in
`limits.md`).

Handle each `PROMPT` in this fixed order, one prompt at a time:

1. Pick one recommended answer (see below) and register it once:
   `AP recommend <session> ... --reason "..."`. Register again only when the
   user's words change it.
2. Show the prompt in plain text with numbered options, the recommended one
   marked, "or answer in your own words", and the auto-answer clock time
   from the `PROMPT` line.
3. Start `AP next <session>` again in the background and end the turn.
4. On the user's reply, send `AP answer <session> ...`. Put feedback with
   quotes, `$` or backticks in a file and pass `--text-file <file>`.

**`question`** — Pick the option that best fits the task and the codebase;
read the relevant code briefly when the choice depends on it. Answer with
`--choice N` or `--text`.

**`draft`** — Read the `draft:` file once. Show a compact digest: the title,
one line of overview, the task list as a table (task number, title, step
count) and the validation commands. Give the file path for the full text.
Recommend `accept`, or `revise` with the concrete gaps found (a missing
requirement, a wrong file, no tests). The draft gets no review by another
model: ralphex reviews the plan file after it is written.

| Option | Answer |
|---|---|
| Accept | `--action accept` |
| Revise with feedback | `--action revise --text-file <file>` |
| Edit the draft directly | tell the user to edit the `edit copy:` file (or make the edits they dictate there), then `--action edited` |
| Reject | `--action reject` (ends planning without a plan) |

**Plan review** (`PROMPT ... plan review: which reviewer` or `which
findings`) — ralphex reviews the plan file it wrote. Follow "Plan Review"
in `../autopilot/references/reviews.md`:

- Which reviewer ("Review the plan before implementation?"): read the
  `plan:` file once. Recommend the `default on no reply:` answer unless the
  chat knows better, for example "skip the review" from the user. Answer
  with `--text "External reviewer"`, `--text "Same agent"` or `--text
  "Skip review"`. Ralphex asks again after each round that changed the
  plan. Recommend `Skip review` then, unless the user wants one more round.
- Which findings ("Which findings should be applied?"): show the
  `findings:` lines as a table (number, severity, text, your assessment).
  The reviewer did not see this chat: a finding can be settled already, or
  be noise. Say so. Recommend the numbers your assessment accepts. Answer
  with `--text "1,3"`, a range such as `--text "2-4"`, `--text "All
  findings"` or `--text "None"`. The planner applies them and edits the
  plan file. The chat does not edit the plan.
- `• Plan review:` and `⚠ Plan review:` lines: post them. A warning ends
  the plan review. Ralphex keeps the plan as it was before that round, and
  planning goes on.

**`choice`, `yesno`, `text`** — Relay the prompt verbatim with a registered
recommendation and answer with the matching flag.

**`⏱` auto-answered line** — Tell the user in one line what was chosen and
why. When they reply later with a different wish, apply it at the next
prompt (usually the draft, as revise feedback).

**`⚠` error or restart line** — Post the line. A restart happens
automatically; the runner keeps earlier answers.

**`FAILED`** — Show the error lines, then recover per `recovery.md` and
start `AP plan` again with the same description and `--flow`. Stop only
when recovery needs the user or 3 rounds are used.

**`DONE`** — Continue to Step 5 with the `plan file:` path.

When the user writes while `AP next` runs, answer them at once from what
the chat already knows. Do not start a second `AP next`.

## Step 5 — Plan Ready

Ralphex reviewed the plan in Step 4. On `DONE`, run `AP check-plan <plan>`
once and fix nothing yourself: report a problem in Step 6.

When this skill was invoked by the `autopilot` skill, return to it after
this step: its decide step offers further revisions. Otherwise ask "Plan
ready. Any changes?" with the `plan_review` response time (30 min; no reply
means no changes). On a change request, run revision rounds per
`plan-revision.md` until the user is done.

## Step 6 — Hand Over

When this skill was invoked by the `autopilot` skill, return control to it
with the plan path and the flow id. Otherwise give a short report per
`report.md`, limited to planning: the plan path and task table, the
decisions taken (by user or by default after the response time, with reasons, from
`AP report --flow <id>`), the plan review (reviewer, findings applied),
plan revisions, problems and recoveries. End with
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
