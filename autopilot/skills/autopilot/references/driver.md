# Driver Reference

`scripts/autopilot.py` (in the `autopilot` skill directory) wraps the plan
runner CLI (`ralphex.windows` or `ralphex`, found on `PATH`; override with
`AUTOPILOT_CLI`). It needs Python 3.9+ and only the standard library.

```
agent ──► autopilot.py plan|run ──► detached supervisor ──► runner CLI
  ▲                                   │  stdin pipe, fzf/editor shims
  │                                   ▼
  └──── autopilot.py next ◄──── state.json + output.log (session dir)
  └──── autopilot.py answer ───► inbox/  ──► supervisor ──► CLI stdin
```
**Note:** the agent never holds the CLI open. The supervisor keeps it alive
between tool calls; the agent watches with `next` and replies with `answer`.

Invoke it as `python3 <autopilot-skill-dir>/scripts/autopilot.py <command>`
(`py -3` on Windows when `python3` is missing). Every command prints one JSON
object, except `config-show` and plain `doctor`. Errors print
`{"error": "..."}` and exit 1.

On Windows, follow `windows.md` for PowerShell commands, the native
wrapper pair, Python selection and review-skip recovery.

## Commands

| Command | Purpose |
|---|---|
| `doctor --json [--latest]` | `cli.plan_review` (the runner reviews plans: v1.7.0-windows.4 or newer), `cli_config.executor` and `cli_config.spec_problems` (phase specs naming the other executor's models), `host` hint (name, sandboxed, network_disabled) and `state_dir`, tools, versions, config locations, effective runner settings, router key presence (never the key), repo cleanliness, active sessions, plus `missing_tools` (runner or router not installed) and `setup_incomplete` (no key, no repo config) for the setup gate in `preflight.md`. `--latest` asks GitHub for the newest releases (`latest`). `--updates` does the same at most once per `[updates] check` interval (24h, cached in the state directory) and adds `updates` (installed tools with a newer release: `tool`, `installed`, `latest`, `via` = brew/go/script/null, `other_copies` later on `PATH`), `updates_skipped` and `updates_check` (`fresh`, `cached`, `offline` or `off`). `--skip-updates` stops offering the releases reported now. `cli.via` and `router.via` tell how each tool was installed. |
| `plan --desc TEXT [--flow ID] [--max-restarts N] [-- CLI ARGS]` | Start plan creation (`--plan TEXT`), with the runner's plan review after the plan file is written. Default 2 restarts. Prints `session`, `flow` and `plan_review` (false: the runner is too old to review plans). |
| `plan-review PLAN [--flow ID] [--max-restarts N] [-- CLI ARGS]` | Start the runner's review of an existing plan file (`--plan-review PLAN`): no planner questions, no drafts, no branch. Relayed like a `plan` session. Refuses to start when the runner has no plan review. |
| `run --plan PATH [--new-worktree] [--worktree-path DIR] [--branch NAME] [--flow ID] [--max-restarts N] [-- CLI ARGS]` | Start plan execution. Default 5 restarts. Reuse the flow id of the plan and of earlier runs. `--new-worktree` makes `<parent>/workspaces/<repo>/<Summary>` (the plan title, else the branch with a capital first letter) with `git worktree add` (`[worktree] path` changes it) or reuses the branch's existing worktree, copies and commits an uncommitted plan and repository configs there, and runs inside it. With `--flow`, every file changed, created or deleted since the flow started (its first session keeps a `baseline` of the files that had changes before) moves too: it is committed there in one "add plan and flow changes" commit, and this checkout gets it back as in HEAD. A file that had changes before the flow is copied but kept here. The run session logs both lists as autopilot decisions; the session keeps `source_repo`, so this folder's chat, `list` and the hooks still find it. A runner `--worktree` flag is turned into `--new-worktree`. Refuses a place `place` marks unusable. `--branch` names the run branch (the runner's `--branch`). Refuses a plan that git ignores: the plans folder must not be in `.gitignore`, because the run commits the plan. |
| `next SESSION [--timeout S] [--catch-up] [--json]` | The chat's watch call. `--catch-up` (reattach) also returns an open prompt an earlier watch reported. Blocks until a prompt the chat has not seen, a notable event, the end, or `S` seconds (default 6900, under the 2 h background limit). Continues after the last reported event on its own. Prints short text (see "The `next` Report"); `--json` prints the `wait` report. |
| `wait SESSION [--since SEQ] [--timeout S] [--any]` | The full JSON report, for diagnosis and tests. Blocks like `next` or until `S` seconds pass (default 240). `--any` also returns on phase changes. |
| `answer SESSION ... [--text-file F] [--by user\|agent] [--reason TEXT]` | Answer the prompt the session waits on (see below). `--text-file` reads the text from a file, so `$`, quotes and backticks reach the runner unchanged. |
| `recommend SESSION <answer flags> --reason TEXT` | Register the answer to send when nobody replies before the prompt's deadline (policy in `response-times.md`). |
| `extend SESSION (--by 30m \| --never)` | More time for the open prompt, or no auto-answer for it. |
| `ask-timer start\|wait\|cancel\|extend --id ID [--decision KEY] [--seconds 1h] [--max S] [--by 30m] [--never]` | Timers for agent-level questions; `wait` returns `reminder`, `pending` (chunk over), `expired`, `cancelled` or `never`. |
| `policy` | Effective response-time policy after the config file and `AUTOPILOT_TIMEOUTS`. |
| `log SESSION --kind decision\|recovery\|fix\|note --text TEXT [--reason TEXT] [--by user\|agent\|auto] [--question TEXT] [--waited MIN]` | Append an agent decision to the session's `decisions.jsonl`. `--by auto` marks a choice taken on a timeout without a reply; add the question and the minutes waited. |
| `timer SECONDS` | Sleep, for timing agent-level questions. |
| `stop SESSION --after-task \| --cancel [--by user\|agent] [--reason TEXT]` | Stop once the current task is done and committed (in a review phase: when the review round ends), or cancel that. Events `stop-queued`, `stop-cancelled`, then `stopped` with `after`. |
| `resume SESSION` | Start a stopped or failed run again: same plan, flags and flow, from the first open task. Returns the new session. |
| `wait SESSION --catch-up` | JSON form of `next --catch-up`. Start after the last event any wait reported (`seen.json`), reading the full timeline (`events.jsonl`) when it is older than the last 40 events. Every wait holds the run's watcher lease (`watcher.json`, with the chat id); a newer wait takes it over and the older returns `{"superseded": true}`. |
| `watch-status [SESSION]` | Per run: `alive`, `status` (`lost` when the supervisor died), `watched`, `owner`, `seen`, `seq`, `unseen_updates`, `reported`. Without SESSION: the repository's live runs and its ended, unreported ones. |
| `mark-reported SESSION` | The final report was posted: hooks and the watchdog stop asking for it. |
| `hook prompt\|stop\|session-start` | Plugin hook entry (stdin: hook input JSON). See `control.md`. `stop` and `session-start` (source `compact`) also drive `compact`. |
| `compact --flow ID --handoff FILE [--focus TEXT]` | Compact the chat before the run. Needs a terminal tab it can type into and the chat session id; otherwise `ok: false` with `skipped` (also with `[compact] mode = off` or `AUTOPILOT_COMPACT=off`). A detached helper waits for the Stop hook (the turn ended, up to 15 min), types `/compact <focus>`, waits for the SessionStart `compact` hook (up to 10 min; 3 min in a host without it), then types `resume_line`. Without a turn end it types only the resume line. The request file in `<state>/compact/` holds `status`. |
| (supervisor) | Records the owner's terminal tab (`tab` in `session.json`). Unwatched for `AUTOPILOT_WAKE_AFTER_S` (600) with news, it types the wake line into that tab or notifies, every `AUTOPILOT_WAKE_EVERY_S` (1200); after the end it lingers up to `AUTOPILOT_LINGER_S` (6 h) until reported. `AUTOPILOT_WAKE=type\|notify\|off` or `[wake] mode`. Event `wake` (`how`, `why`). |
| `report [SESSION] [--flow ID]` | Every session of a flow (with executor, `models_used` call counts and `steps`: each step's section, model and `how`), the merged decision log, `timeouts` (every choice taken without a reply: question, chosen, `waited_min`, `recommended` false when the driver fell back to the first option or a blind accept, reason), `without_user` (every decision the user did not make: by `auto`, `agent`, `autopilot`, `agrouter` or `ralphex`, notes left out), and the branch's commits and diffstat: input for the final report. |
| `stop SESSION` | Interrupt the CLI gracefully, then kill its process tree. |
| `list [--all]` | Sessions of this repository (`--all`: every repository). |
| `config-set FILE KEY [--section S] (--value V \| --value-stdin) [--private]` | Set one INI key, uncommenting a template line when one exists. `--private` makes the file 0600 and its directory 0700. Use `--value-stdin` for secrets. Refuses a secret key (`api_key`, tokens, passwords) in a file inside the repository. |
| `config-set ... --comment TEXT` | Also write full-line `# TEXT` comments right above the key (once). |
| `config-init [DEST] [--tool ralphex\|agrouter\|autopilot] [--force]` | Write the tool's full commented default config to `.ralphex/config` (runner: only `config`, not `agents/` or `prompts/`), `.agrouter/config` (router: its embedded defaults, made in a temporary config directory with no key) or `.autopilot/config` (every key with its default). Keeps an existing file; `--merge` upgrades a partial (hand-written) one to the template with its current values and comments, backing up the old file in the state directory. `doctor.repo_configs` shows each file as `missing`, `partial` or `full`. |
| `install-wrapper --docs F... --doc-dirs D... [--windows] [--force]` | Install the Codex-through-router wrapper into `.ralphex/scripts/` and print the `codex_command` value. |
| `config-show FILE` | Active INI lines with secret values shown as `<set>`. |
| `plan-rev snapshot\|diff\|supersede PLAN [--by NEW]` | Revisions of a finished plan: snapshot before a change; diff against the last snapshot (with task counts, `checked_boxes_lost` and the check verdict); supersede an old plan by a re-planned one (backup kept, `git rm` when committed). See `plan-revision.md`. |
| `publish RUN_SESSION [--mode pr\|push\|off] [--base B] [--title T] [--body-file F] [--draft]` | Push the run branch (from its worktree when it has one; never forced) and open a pull request with `gh` (GitHub) or `glab` (GitLab), or reuse an open one. Base: the branch the run started from (`start_branch`); for a run on a feature branch, the branch it was created from (reflog); the default branch when that base is not on the remote. Draft when the run did not finish. Returns `branch`, `folder`, `base`, `base_why`, `commits`, `uncommitted`, `pushed`, `pr` (`url`, `created`, `draft`) or `skipped` with the reason, and logs the outcome. Mode is `pr` unless the user asks for `push` or `off` in the chat. The title is the plan title (`Add the user service`) or `[worktree] pr_title` in `.autopilot/config`; `--title` only for a title the user names. |
| `place [--plan PLAN] [--flow ID] [--branch NAME] [--summary TEXT]` | Where the implementation run should work: `checkout` (`main checkout` or `worktree`), `branch`, `default_branch`, `dirty` (without the plan), `operation`, `use_worktree`, `recommended` (`worktree`, `here` or null), `why`, and `options` (recommended first, each with `run_args` for `run`, `usable`, `reason`, `text`; the worktree option also has its `path`). `run` refuses a place that is not usable (for example a new worktree from a feature branch), unless `--force`. With `--flow`, the worktree option also lists `moves` (files changed during the flow: they go to the new worktree) and `keeps` (files with earlier changes: copied, kept here). `names`: the proposed `branch` and `path`, after the default convention `{type}/{slug}` or `[worktree] branch`/`path` (placeholders `{type}` = `feature` or `bugfix`, `{slug}`, `{summary}`); `--branch` (a name the user asked for) wins over the pattern; `note` says when a configured pattern was not usable and the default was used; the options' `run_args` carry `--branch` and `--worktree-path` when they differ from the runner's defaults. |
| `skip-review SESSION [--by user\|agent] [--reason TEXT]` | Skip the run's external code review: SIGQUIT now if it runs, queued until it starts otherwise (restart without it on Windows). |
| `check-plan FILE` | Can the runner execute this file: `verdict` (`ready`, `warnings`, `redesign`), `status` (`not started`, `in progress`, `complete`), `problems`, `warnings`, the tasks with checkbox counts. Mirrors the runner's parser. See `plan-check.md`. |
| `config-summary` | The repository's setup property by property (executor, phase models with source local/global/default and mode, docs and wrapper style, router, external review, limits, local router limits, classifier, response-time overrides), issues and file git states. |
| `config-unset FILE KEY [--section S]` | Comment a key out so it inherits again. |
| `set-docs --docs F... --doc-dirs D... [--replace-wrapper]` | Rewrite the `--doc` part of `claude_args` and regenerate an autopilot wrapper; a hand-written wrapper is kept unless `--replace-wrapper`. |
| `catalog` | Models and efforts per CLI from the router config (global + local). |

`plan` and `run` add `--wait=900s` when no `wait_on_limit` is configured
(see `limits.md`), and refuse to start when ralphex.windows or agrouter is not
installed, with an error that names the missing tools and points to
`autopilot-setup`.

Pass extra runner flags after `--`, for example
`run --plan docs/plans/x.md -- --tasks-only` or `plan --desc "..." -- --plan-model=opus:high`.
Always run `plan` and `run` from the repository root.

Sessions live in `~/.local/state/autopilot/sessions/<id>/`
(`%LOCALAPPDATA%\autopilot` on Windows, `AUTOPILOT_STATE_DIR` to override):
`output.log` holds the full CLI output across attempts, `draft.md` the latest
plan draft and `draft-edited.md` an editable copy.

## The `next` Report

Short text, one fact per line. The chat reads it with one `cat`.

```
session 20261116-162759-plan-a8c1 · plan · waiting · plan iteration 4 · claude-opus-5-5 (high)
⚠ Router warning: ...
PROMPT draft (id 14) · auto-answer at 17:15 (in 30 min)
recommended: accept (covers all feedback)
draft: ~/.local/state/autopilot/sessions/<id>/draft.md
edit copy: ~/.local/state/autopilot/sessions/<id>/draft-edited.md
answer: AP answer <id> --action accept | --action revise --text "..." | --action edited | --action reject
seq 14
```
**Note:** the first line is the state, then the update lines to post, then
the open prompt with its options and the answer command. A `question` lists
numbered options under `question:`.

A plan review prompt adds its step to the `PROMPT` line and shows the plan,
the default and the findings:

```
PROMPT question (id 21) · plan review: which findings · auto-answer at 17:30 (in 15 min)
recommended: none registered; run AP recommend first
default on no reply: 1,3 (applies the critical and major findings)
plan: docs/plans/add-cache.md · round 1 · reviewer: External reviewer
question: Which findings should be applied?
findings:
  #1 CRITICAL: Task 1 - no test step
  #2 MINOR: Overview - wording
  #3 MAJOR: Task 2 - src/a.py does not exist, the code is in src/b.py
  1. All findings
  2. None
answer: AP answer <id> --text "1,3" | --text "2-4" | --text "All findings" | --text "None"
```
**Note:** `#N` is the finding number for the answer. `1.` and `2.` are the
options of the question.

| Last part | Meaning |
|---|---|
| `PROMPT <kind> (id N)` | The runner waits for an answer. `recommended:` shows the registered answer, or asks to register one. |
| `DONE · plan file: <path>` | The session ended well. |
| `FAILED`, `LOST`, `STOPPED` | The last error, the recent error lines and the log path. |
| `LIMIT ...` | A rate-limit episode with the reset time. |
| `NO NEWS · run AP next <id> again` | The timeout passed with nothing new. Post nothing. |

## The `wait` Report

| Field | Meaning |
|---|---|
| `status` | `running`, `waiting` (needs an answer), `restarting`, `done`, `failed`, `stopped`, `lost` (supervisor died) |
| `seq` | Event counter. Pass it back as `--since` on the next `wait`. |
| `new_events` | Events after `--since`, each with its fixed `line` (null when the chat skips it): `task-start` (task, tasks, title, model, how, iteration, continued, steps), `task-done` (task, title, minutes, models, commit, subject, steps), `phase-start` (section, phase, model, how, `via: agrouter log` when read from the router's log), `commit` (outside a task), `router-fallback` (agrouter could not route: `code`, `stage`, `message`, `fields` left to the CLI and their `defaults` with value and source), `router-warning` (other `agrouter: warning:` lines, not errors), `stalled` (no runner output and no write to the agent's transcript, `~/.claude/projects/<cwd>/` or `~/.codex/sessions/`, for `AUTOPILOT_STALL_S`, 20 min), `heartbeat` (a step without news for `AUTOPILOT_HEARTBEAT_S`, 30 min: minutes in the step, minutes since the last output, `active_min` since the last transcript write), `section`, `waiting`, `answered`, `auto-answered` (deadline passed), `auto-answer` (plan's "continue?"), `plan-review` (an outcome or warning line of the runner's plan review: `text`, `warning`), `needs-input` (free-text prompt without a default), `error`, `restart`, `limit`, `limit-cleared`, `failed`, `done`, `stopped`, `stale-answer` |
| `updates` | The non-null `line`s of `new_events`, in order: what the chat posts |
| `auto_answer_in_s`, `auto_answer` | Seconds until the open prompt is answered by default (null: never), and its policy key |
| `flow` | Flow id shared by the sessions of one full run |
| `phase`, `section` | Friendly phase (`planning`, `implementing tasks`, `code review`, `external review`, `finalizing`) and the raw section header, such as `task iteration 3` |
| `tasks` | `{done, total, current_task}` from the plan's checkboxes, or null |
| `waiting` | The open prompt (see below) |
| `plan_file` | Plan created by a `plan` session |
| `attempt`, `restarts`, `last_error`, `recent_errors` | Restart bookkeeping and the CLI's own error lines |
| `activity` | Last ten output lines without timestamps: the source for "what it is doing now" |
| `limit` | Active rate-limit episode (tool, message, `resets`, `resets_in_s`, `retries`, `next_retry_in_s`, `handled_by`), or null. See `limits.md`. |
| `executor`, `model` | The run's executor (`claude` or `codex`) and what runs right now (`cli`, `model`, `effort`, from agrouter's `<log_dir>/agrouter.log` for every step; the router's selection line in the output or the Codex banner only when the log has none). Values agrouter left to the CLI are filled from the selection's `effective` part; `fallback` is set when routing failed |
| `heartbeat` | True when the timeout passed with nothing new: post nothing |
| `log` | Path of `output.log` |

`failed` events carry `reason`: `needs a fix before it can continue` (a
fatal error such as an unknown flag, missing file, failed login, uncommitted
changes or an existing worktree), `restart limit reached`, or `same error three
times with no progress`.

## Prompts and Answers

| `waiting.kind` | Fields | Answer |
|---|---|---|
| `question` | `question`, `options`, `free_text: true` | `--choice N` (1-based over `options`) or `--text "own answer"` |
| `choice` | `question`, `options`, `free_text` | `--choice N`, or `--text` matching an option; other text only when `free_text` |
| `draft` | `draft_file`, `edited_file` | `--action accept`, `--action revise --text "feedback"`, `--action edited` (after changing `edited_file`), `--action reject` |
| `yesno` | `question` | `--yes` or `--no` |
| `text` | `prompt` | `--text "..."` |

A `question` or `choice` of the runner's plan review also has `review`:
`step` (`pick` for "Review the plan before implementation?", `apply` for
"Which findings should be applied?"), `round`, `plan_file`, `default`
(`answer` and `reason`: what the driver sends when nobody replies and no
recommendation is registered), and for `apply` the `reviewer` and the
`findings` (`n`, `severity`, `text`). Answer `pick` with `--text "External
reviewer"`, `"Same agent"` or `"Skip review"`, and `apply` with `--text
"1,3"`, `"2-4"`, `"All findings"` or `"None"`. Their response times are
`review_pick` and `review_apply`.

Answers are single lines: the driver joins line breaks with spaces. Revision
feedback that needs structure goes into `edited_file` instead, and
`--action edited` sends the diff to the planner as annotated feedback.

The plan session (also `plan-review`) answers "Continue with plan
implementation?" with **no** on its own, so planning never starts
execution by itself.

## How Prompts Reach the Driver

The CLI asks questions through `fzf` when it is on `PATH` and through a
numbered list otherwise. The supervisor puts a `fzf` shim first on the CLI's
`PATH`: it records the question and options, then exits as "no match", and
the CLI reads the typed answer from stdin. On Windows the shim is
`fzf.cmd`; the runner finds it through `PATHEXT` and Windows starts batch
files through `cmd.exe` on its own. The CI test launches it the same way
(resolved path, no shell). Draft review always uses the numbered list. `VISUAL`/`EDITOR` point at a shim that copies
`draft-edited.md` into the CLI's temporary file, which makes "Interactive
review" work without a terminal.

## Restart Policy

| Failure class | Detected by | Action |
|---|---|---|
| fatal | unknown flag, missing file, not logged in, authentication, uncommitted changes, existing worktree, wrong directory | stop, report `failed` |
| limit | the CLI's or router's limit wording, or an open limit episode | wait until the reported reset (+60 s) or 900 s, restart; resets over 6 h away go to the agent (`limits.md`). Not counted as restarts. |
| transient | anything else | back off 30 s, 60 s, 120 s ... (max 600 s; base from `AUTOPILOT_BACKOFF`), restart |

The runner resumes from the first unchecked task, so a restart repeats no
finished work. Three identical failures with no new checked boxes stop the
session.

Each attempt starts clean: an open prompt of the exited process is dropped
(event `prompt-dropped`), together with its recommendation and deadline.
The restarted process asks again under a new prompt id.

```
attempt 1: ralphex <plan> --worktree ─► creates branch + worktree, ticks Task 1, fails
attempt 2: branch holds the plan? ─yes─► cwd .ralphex/worktrees/<branch>, ralphex <plan> (no --worktree)
                                  └─no──► ralphex <plan> --worktree again
```
This applies to older sessions started with the runner's own `--worktree`.
New runs use `--new-worktree` instead and restart in place inside their
folder.

**Note:** with `--worktree` the runner copies an uncommitted source plan into
a new worktree, over the branch's ticked copy. So once the branch holds the
plan, the driver continues inside the branch's worktree (recreating it
when the runner removed it) and drops `--worktree`. The runner then works on
the checked-out branch and keeps the ticks. With `use_worktree = true` in a
runner config this cannot be turned off per run, so the driver stops with
`restarting would lose the progress on the run branch` (see `recovery.md`).
