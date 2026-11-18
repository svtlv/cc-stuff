---
name: autopilot-plan
description: This skill should be used when the user runs "/autopilot-plan" or "$autopilot-plan", or asks to "plan this with autopilot", "create an autopilot plan", "make an implementation plan for autonomous execution", "start planning only", "draft a plan for the runner", "plan it but don't implement yet", "turn this design doc into a plan" or "redesign this plan for autopilot". It starts the autonomous plan runner's interactive plan creation, relays every clarifying question and plan draft to the user in order with a recommended answer that is taken when its response time passes without a reply, offers one external or same-agent review of the finished plan, and finishes with a plan file in docs/plans/ without starting implementation.
argument-hint: "[task description | --redesign <file>] [-- extra runner flags]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion, Agent
metadata:
  version: "0.5.0"
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
`active_sessions`, ask whether to resume watching it (`AP next` on that
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
and reset phase models to `:`. `review-plan --session` uses that executor
for the plan review in Step 5.

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
```
**Note:** one `AP next` is in flight at a time. The chat does nothing else in
parallel: no extra waits, no reviews and no timers during planning.

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
model: that review comes once, on the finished plan (Step 5).

| Option | Answer |
|---|---|
| Accept | `--action accept` |
| Revise with feedback | `--action revise --text-file <file>` |
| Edit the draft directly | tell the user to edit the `edit copy:` file (or make the edits they dictate there), then `--action edited` |
| Reject | `--action reject` (ends planning without a plan) |

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

## Step 5 — Review the Finished Plan

Review the plan file once, after ralphex wrote it, as `/planning:make` does
with "Auto review". Follow "Plan Review" in `reviews.md`. Always ask the
user which review to run, and always give a recommended option, so the
flow continues when nobody answers:

1. Run `AP review-plan <plan> --session <session> --options`. It returns
   the `options` (with `usable` and `reason` for each reviewer for the
   session's actual executor), the `recommended` option and
   `recommended_why`. The recommendation comes from `[review] plan`
   (default `auto`: the other model, else the same agent when the other
   model is not usable; `off`: no review).
2. Ask in plain text, with the recommended option first and marked
   "(recommended: <recommended_why>)", and the clock time at which it is
   taken. Show an unusable reviewer with its reason, not as a choice. Start
   `AP ask-timer start --id <flow>-review --decision review_pick` (10 min)
   and wait with `ask-timer wait`. Do not use a blocking question tool:
   its dialog stops the timer, and the flow then waits for the user.

   ```
   Plan written: docs/plans/<name>.md (5 tasks). Review it before we go on?
     1. External review by Codex (recommended: codex reviews plans here)
     2. Same-agent review by Claude, read-only
     3. Edit the plan yourself
     4. No review
   I'll start option 1 at 16:40 (in 10 min) if you don't answer.
   ```

   | Option | Command |
   |---|---|
   | External review (the other model, for example Codex) | `AP review-plan <plan> --reviewer external --session <session> --task "..."` |
   | Same-agent review (the executor model, read-only) | `AP review-plan <plan> --reviewer same --session <session> --task "..."` |
   | Edit the plan yourself | the user edits the file; then `AP check-plan <plan>` |
   | No review | go on |

   On a reply, `ask-timer cancel` and act on it. When the timer expires,
   take the recommended option and log it:
   `AP log <session> --kind decision --by auto --question "Review the plan?" --waited 10 --text "External review (codex)" --reason "<recommended_why>"`.
   The final report lists it under "Decided without you".
3. Say "Codex reviews the plan (about 3 min)" and run the review (Claude
   Code: in the background). Start nothing else until it returns.
4. Show the result: the status line, then each finding as a table row
   (number, severity, text, your assessment). The reviewer did not see this
   chat, so a finding can be settled already, or be noise: say so.
5. Ask which findings to apply ("1,3", "all", "none"). Recommend the ones
   your assessment accepts. Use `ask-timer --decision review_apply`
   (15 min).
6. Apply the chosen findings: `AP plan-rev snapshot <plan>`, edit the plan,
   `AP plan-rev diff <plan>`, `AP check-plan <plan>`. Log the outcome with
   `AP log <session> --kind decision --text "Applied findings 1,3"`.
7. Offer the remaining options again without the review just done, as
   `/planning:make` does. When a review was skipped or failed, keep it in
   the list.

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
