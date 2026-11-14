---
name: autopilot-run
description: This skill should be used when the user runs "/autopilot-run" or "$autopilot-run", or asks to "run this plan with autopilot", "execute the plan", "implement docs/plans/x.md autonomously", "start the autopilot run", "resume the autopilot run" or "watch the run". It first checks that the plan file suits the runner (task sections, checkboxes, tests) and offers to redesign an unsuitable one with autopilot-plan, then executes it with the autonomous plan runner, monitors every phase, restarts failed attempts automatically, diagnoses and fixes blocking problems itself, shows errors as they happen, keeps the user posted on what the run is doing right now and ends with a full report.
argument-hint: "<plan file> [-- extra runner flags such as --worktree or --tasks-only]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion, Agent
metadata:
  version: "0.1.0"
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
start decision on the run session once it exists.

## Step 3 — Start

```
AP run --plan <plan path> [--flow <id>] [--max-restarts N] [-- <extra flags>]
```

Pass `--flow` when the `autopilot` skill provides one; otherwise keep the
`flow` this first run returns and reuse it for every recovery restart. Tell
the user it has started, with the session id, and that updates follow every
few minutes.

## Step 4 — Monitor Until It Ends

Follow `monitoring.md`: loop on `AP wait <session> --since <seq> --timeout
240`, in the way the host supports (`hosts.md`).

| Report | Response |
|---|---|
| heartbeat | Two-line update: elapsed time, task N of M and its title, phase, and one plain sentence from `activity` on what it is doing now. |
| `section` event | Fold into the next update ("Tasks done. Code review 1 started."). |
| `error` event | Quote the error line, say whether the runner is retrying it. |
| `restart` event | Show the error, the restart number and the delay. Finished tasks are kept. |
| `waiting` | The runner needs input. Register a recommendation, relay the prompt verbatim with its auto-answer time (`runtime`, 10 min: the run is blocked meanwhile), answer with the user's reply ("Unattended Defaults"). |
| `auto-answered` | One line: what was chosen without a reply, and why. |
| `limit` / `limit-cleared` | Rate limit: say which tool, when it resets and that the runner retries on its own; later, how long it took. Switch to `--timeout 900` while limited. See `limits.md`. |
| `failed`, category `limit` | Reset is hours away: pick and recommend an option per "Long Limits" in `limits.md`; `ask-timer` with `--decision limit` (30 min). |
| `failed` / `lost` | Show the error in one or two lines, then recover on your own per `recovery.md`: known fix or diagnosis (a read-only subagent may help), log every step, start again with the same `--flow`. Stop only when recovery needs the user or 3 rounds are used. |
| `review-skipped` / `review-skip-queued` | The external code review was ended (or will be when it starts). One line; note it for the report. |
| User: "skip the external review" | `AP skip-review <session>` ("External Code Review" in `reviews.md`); before the start, `-- --external-review-tool=none` instead. |
| `stopped` | Confirm, with tasks done so far, then Step 5. |
| `done` | Step 5. |

The user may ask questions mid-run. Answer from the latest report or the log
without stopping the run. When they ask to stop, run `AP stop <session>`.
When they ask to change the plan mid-run, follow "Changing a Plan During a
Run" in `plan-revision.md`: stop at a safe point, edit directly, show the
diff, start again with the same `--flow`.

## Step 5 — Final Report

Always, whatever the outcome. When invoked by the `autopilot` skill,
return control to it (it writes the report for the whole flow). Otherwise
follow `report.md` with `AP report --flow <id>`: summary, plan tasks and
their status, critical decisions (user, default after the response time, autopilot,
agent), a full explanation of what was built (explain-summarize on the
branch changes when available), problems and recoveries, state now, next
steps. Note where the plan went when the runner moved it to `completed/`.

## Rules

- Keep the run supervised by the driver. Never start the runner directly in
  the foreground: it blocks the agent and loses restarts.
- Never edit repository files while the run is active. Recovery fixes
  happen only between sessions, on the run's branch, within the guardrails
  of `recovery.md`.
- Do not hide failures. Every `error`, `restart` and `failed` event reaches
  the user, quoted.
