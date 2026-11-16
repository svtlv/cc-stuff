---
name: autopilot-run
description: This skill should be used when the user runs "/autopilot-run" or "$autopilot-run", or asks to "run this plan with autopilot", "execute the plan", "implement docs/plans/x.md autonomously", "start the autopilot run", "resume the autopilot run", "watch the run", "pause after this task", or sends an "autopilot: reattach to run" wake line. It first checks that the plan file suits the runner (task sections, checkboxes, tests) and offers to redesign an unsuitable one with autopilot-plan, then executes it with the autonomous plan runner, monitors every phase, restarts failed attempts automatically, diagnoses and fixes blocking problems itself, shows errors as they happen, keeps the user posted on what the run is doing right now and ends with a short report: decisions made without the user, then the goal and what was done.
argument-hint: "<plan file> [-- extra runner flags such as --worktree or --tasks-only]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion, Agent, CronCreate, CronDelete
metadata:
  version: "0.4.0"
---

# Autopilot Run

Execute an implementation plan with the autonomous plan runner (ralphex) and
stay with it until it finishes: report progress, surface every error, and
restart automatically after failures.

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
| Mid-run control, watchdog, reattach | `../autopilot/references/control.md` |
| Final report | `../autopilot/references/report.md` |
| Rate limits | `../autopilot/references/limits.md` |
| Response times | `../autopilot/references/response-times.md` |

Read both references before the first driver call. Below, `AP` means
`python3 <absolute driver path>` (`py -3` on Windows if `python3` is
missing). If the `autopilot` directory is missing, stop and say that this
skill needs the whole `autopilot` plugin installed.

## Step 0 — Setup Gate

Before anything else, even before choosing a plan, follow `preflight.md`.
When ralphex.windows or agrouter is not installed, ask to run the
`autopilot-setup` skill and do not continue until setup succeeds or the user
stops.

## Step 1 — Pick the Plan

Use the first argument as the plan path. Text after `--` goes to the runner
unchanged.

Without a path, list `*.md` files in the plans directory
(`cli_config.effective.plans_dir` from `doctor`, default `docs/plans`),
skipping `completed/`, newest first. Ask which to run. With several plans
there is no default (`plan_pick` waits); with exactly one, recommend it
with the `preflight` response time. When none exist,
offer the `autopilot-plan` skill.

Check it before anything else: `AP check-plan <path>`, then act on the
verdict per `plan-check.md`:

| Verdict | Next |
|---|---|
| `ready` | Step 2. |
| `warnings` | List them; run as is or redesign (`plan_check` response time). |
| `redesign` | Say why the runner cannot use it; recommend redesigning with `autopilot-plan` (default after 15 min) or stop. Never run it as is. |
| `status: complete` | Offer review only (`-- --review`) or stop. |

A redesign follows `../autopilot-plan/SKILL.md` with `--redesign <path>`
and the same `--flow`, then checks the new plan again and comes back here
with it: show its digest (title, task table) and continue with Step 2. The
original file stays untouched.

## Step 2 — Preflight

Use the Step 0 `doctor` report (rerun `AP doctor --json` from the
repository root when setup ran in between).

| Finding | Action |
|---|---|
| `reviews.execution.usable` false | If `fallback` is Codex and `fallback_blocker` is empty, the driver switches this session to Codex and logs why. Otherwise fix the listed blocker through setup or log in. Use the actual executor in the start result and subsequent status. |
| `cli_config.spec_problems` not empty | The phase specs would send the wrong executor's models. Recommend starting with `-- --plan-model=: --task-model=: --review-model=:` (this run only) and fixing `.ralphex/config` later; `preflight` response time. |
| `active_sessions` has a live `run` for this plan | Attach to it: skip Step 3 and go to Step 4 with that session. |
| `reviews.code_review`: tool `codex`, not usable | The driver skips the external review for this run on its own; say why in one line (from `reason`) and how to fix it. |
| `repo_clean` false on the default branch | The runner creates a feature branch and refuses when files other than the plan are uncommitted. Show `git status --short` and recommend a worktree (`-- --worktree`), which leaves the user's changes untouched; committing them is the alternative. Never stash or discard them. |

State what will run in one short table: plan, branch (current or the one the
runner will create), setup (Claude or Codex, from `cli_config.executor`; a
`--codex` flag switches to Codex), models from `cli_config.effective`,
phases (Codex: no external review), extra flags. Starting is an outward action (it commits on a branch, may run for
hours and spends model usage), so ask unless the user already asked to
start right away or the `autopilot` skill obtained consent. Offer three
answers: start, **change the plan first** (revision rounds per
`plan-revision.md`, then this question again with a fresh timer), or stop.
Ask in plain text with the start time ("starting at 16:10 unless you say
stop"), run an
`ask-timer` with `--decision run_start` (30 min, reminder 10 min before) per
`response-times.md`, and start when it expires. Log the
start decision on the run session once it exists (`--by auto --question
... --waited <N>` when it started on the timeout, per "Logging Critical
Decisions" in `monitoring.md`).

## Step 3 — Start

```
AP run --plan <plan path> [--flow <id>] [--max-restarts N] [-- <extra flags>]
```

Pass `--flow` when the `autopilot` skill provides one; otherwise keep the
`flow` this first run returns and reuse it for every recovery restart. Tell
the user it has started, with the session id, and that a line follows for
every task start and finish, review, commit and problem.

## Step 4 — Monitor Until It Ends

Follow `monitoring.md`: loop on `AP next <session>`, in the way the host
supports (`hosts.md`; Claude Code: background, Bash timeout 7200000 ms).
Post its update lines as is ("Update Formats" in `monitoring.md`); the
table says what to add or do on top.

| Report | Response |
|---|---|
| `NO NEWS` | Post nothing; run `next` again. |
| `task-start`, `task-done`, `phase-start`, `commit`, `error`, `restart` | The line only. |
| `stalled` | The line, plus one sentence from the log's last lines on what it is stuck on. |
| `PROMPT` | The runner needs input. Register a recommendation, relay the prompt verbatim with its auto-answer time (`runtime`, 10 min: the run is blocked meanwhile), answer with the user's reply ("Unattended Defaults"). |
| `auto-answered` | The line (what was chosen without a reply, and why). |
| `limit` / `limit-cleared` | The line, with the reset as a clock time. See `limits.md`. |
| `failed`, category `limit` | Reset is hours away: pick and recommend an option per "Long Limits" in `limits.md`; `ask-timer` with `--decision limit` (30 min). |
| `failed` / `lost` | Show the error in one or two lines, then recover on your own per `recovery.md`: known fix or diagnosis (a read-only subagent may help), log every step, start again with the same `--flow`. Stop only when recovery needs the user or 3 rounds are used. |
| `review-skipped` / `review-skip-queued` | The external code review was ended (or will be when it starts). One line; note it for the report. |
| User: "skip the external review" | `AP skip-review <session>` ("External Code Review" in `reviews.md`); before the start, `-- --external-review-tool=none` instead. |
| `stopped` | Confirm, with tasks done so far, then Step 5. |
| `done` | Step 5. |

### Staying in Charge

This chat owns the run: the user can ask about it and have it act at any
time ("Mid-Run Requests" in `control.md`). Questions are answered from the
report, the log and the run branch's commits while the run continues.
Changes to code, the plan or models go through a safe point: `AP stop
<session> --after-task`, change, `AP resume <session>`. Keep exactly one
`next` in flight ("One Watcher"). If the chat goes quiet (its own usage
limit), the supervisor wakes it by typing a line into its terminal tab, for
any provider ("Waking the Chat"); on Claude Code in a terminal that cannot
be typed into, arm the watchdog cron when the run starts. On a wake line, in
a new chat, or after any interruption, reattach with `AP next <session>
--catch-up` ("Reattach").

## Step 5 — Final Report

Always, whatever the outcome. When invoked by the `autopilot` skill,
return control to it (it writes the report for the whole flow). Otherwise
follow `report.md` with `AP report --flow <id>`: first every decision
made without the user (`without_user`), then the goal and what was done
(summarize on the branch changes when available, plan tasks and their
status), then only the open items that need the user. Note where the plan went when the runner moved it to `completed/`.
Then `AP mark-reported <session>` (the supervisor stops waking) and delete
the watchdog cron job if one was made.

## Rules

- Keep the run supervised by the driver. Never start the runner directly in
  the foreground: it blocks the agent and loses restarts.
- Never edit repository files while the run is active. Code changes the
  user asks for, and recovery fixes, happen only after a safe stop
  (`--after-task`) or between sessions, on the run's branch, within the
  guardrails of `recovery.md`.
- Do not hide failures. Every `error`, `restart` and `failed` event reaches
  the user, quoted.

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
