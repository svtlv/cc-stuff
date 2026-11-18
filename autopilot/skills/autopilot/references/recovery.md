# Self-Recovery

When a session ends `failed` or `lost`, do not stop at reporting. Diagnose,
fix what is safe to fix, log it, and start again. Ask the user only for what
cannot be done without them (credentials, logins, choices that lose work).

```
failed ─► classify ─► known fix? ──yes──► apply ─► log ─► start again (same flow) ─┐
                         │                                                          │
                         no ─► diagnose (subagent or inline) ─► safe fix? ─yes─► ───┤
                                                   │                                │
                                                   no ─► stop: report + what is needed
        ◄──────────────── up to 3 recovery rounds per flow ─────────────────────────┘
```
**Note:** the driver already retries crashes, and the runner and driver wait out rate limits on their own (`limits.md`); a limited run is never a failure to recover from. This
playbook starts where the driver gives up: fatal errors, the restart limit,
or the same error three times.

## Known Problems

| Signal in `last_error` / `recent_errors` | Fix | Safe without asking |
|---|---|---|
| `uncommitted changes` / `cannot create branch` | Start again with `--new-worktree` (the new folder ignores the uncommitted files here). Never stash or discard the user's changes. | yes |
| `worktree already exists` | Check that no session runs it (`AP list`). Keep the worktree: when `git -C <worktree> status --porcelain` shows changes, commit them inside it (`git -C <path> add -A && git -C <path> commit -m "wip: autopilot recovery"`). Then start the same run again with `-- --worktree`. The driver sees the branch already holds the plan and continues inside that worktree without `--worktree`, so ticked tasks stay ticked. Never remove the worktree to let the runner recreate it: it would copy the unticked source plan over the branch's progress and redo finished tasks. | yes |
| `restarting would lose the progress on the run branch` | `use_worktree = true` is set in a runner config, so the driver cannot resume the branch safely. Ask before changing config: set `use_worktree = false` in `.ralphex/config` (or remove it), then start again with `-- --worktree`. | no |
| `unfinished git operation` (merge, rebase) | Show `git status`. Never abort another person's merge or rebase. | no |
| `not a git repository` / `repository root` | `cd` to `git rev-parse --show-toplevel` and start again. | yes |
| `plan file ... not found` (plan moved to `completed/`) | Look in `<plans_dir>/completed/`. When every task is checked, the run is done: go to the report. | yes |
| `spec_problems` in `doctor`, or Codex rejecting a model (`model ... not found/supported`) under the Codex executor | Phase specs name the other executor's models (often inherited globally). Start again with `-- --plan-model=: --task-model=: --review-model=:` for this run, log it, and recommend the permanent fix in `.ralphex/config` ("Recipe: Codex Executor"). | yes (run flags only) |
| `codex` too old, or `codex login status` not logged in | Needs the user: show the upgrade or login command. | no |
| Codex missing or logged out during the external review | Handled by the driver: it restarts the run without the review (`review-skipped`). Report it; offer `codex login` or `external_review_tool = none` for later. | yes (automatic) |
| `unknown flag` / `unknown option` | Remove the offending flag from the extra arguments or the config line it came from; start again. | yes |
| `agrouter: ... config` / exit 2 from the router | `AP config-show` the router configs, fix the key named in the error per `config-recipes.md`. | yes, except the API key |
| `authentication`, `invalid api key`, classifier `401` | The classifier key is wrong or missing; only the user can fix it (`autopilot-setup key`). To keep going meanwhile, bypass the router for this run only: start again with `-- --claude-command=claude "--claude-args=--dangerously-skip-permissions --output-format stream-json --verbose" --task-model=opus:medium --review-model=opus:medium --external-review-tool=none`. Log it as a critical decision and say so in the report. | bypass yes, key no |
| Claude missing or logged out during planning or execution | The driver restarts with Codex when usable and already configured for execution, keeping completed steps. Otherwise fix the reported fallback blocker through setup, or ask for `claude auth login`. See `reviews.md`. | yes (compatible fallback); login needs user |
| The runner's plan review fails, times out or trips its read-only guard (`⚠ Plan review:`) | Ralphex ends the review and keeps the plan; planning goes on. Post the line. After a guard warning, check `git status` for files the reviewer changed and tell the user. Offer `AP plan-review <plan>` for one more try. | yes |
| `Not logged in` for the only usable executor | Needs the user to log in. Stop and say which CLI. | no |
| Category `limit` (reset far away) | Decide per "Long Limits" in `limits.md`: continue on the other executor, skip the limited external review, or wait for the reset. | yes, with the `limit` response time |
| `lost` (supervisor died) | Start the same `run` again with the same flags; the runner resumes at the first unchecked task (a worktree run continues in its worktree, as above). | yes |
| Same task fails 3× (validation/tests fail) | Diagnose (below). | per fix |

## Diagnosing Unknown Failures

1. Read the last 120 lines of the session `log`, the failing task in the
   plan, and `git log --oneline -5` / `git status` on the run's branch.
2. When the host has subagents, give one read-only subagent the log path,
   plan path, error lines and branch, and ask for the root cause and the
   smallest fix. Otherwise do it inline.
3. Decide:

| Root cause | Action |
|---|---|
| Environment: missing tool, dependency, service, generated file | Install/generate it when it is a normal project step (package install, codegen) and start again. |
| The plan is wrong or impossible (task contradicts the code) | Edit the plan: reword the task, split it, or mark it blocked with a note. Commit the plan change on the run's branch. |
| A bug the runner keeps failing to fix | Fix it directly with the smallest change, run the validation commands from the plan, commit on the run's branch with a message starting `fix(autopilot):`. |
| Test failing for reasons unrelated to the change | Check it on the base branch in a temporary worktree (`git worktree add <tmp> <base>`, run the test, `git worktree remove <tmp>`). Failing there too means it is pre-existing: note it in the plan's task as a known failure, commit, start again. Passing there means the change broke it: treat it as a bug. |

Make fixes only while no session runs. Then start the run again with the
same `--flow`.

## Guardrails

- At most **3 recovery rounds** per flow. After that, stop and report.
- Never: `git reset --hard`, `git clean`, `git push --force`, deleting
  branches with unmerged commits, discarding or stashing the user's
  uncommitted work, editing the user's global configs without saying so,
  writing secrets.
- Work only on the run's branch or worktree. Never commit fixes to the
  default branch.
- Every recovery step is logged before the restart:

```
AP log <session> --kind recovery --text "<what was done>" --reason "<error and why this fix>"
AP log <session> --kind fix --text "<commit sha: what changed>" --reason "<root cause>"
```

- Tell the user in one or two lines what failed, what was done, and that the
  run is starting again.
