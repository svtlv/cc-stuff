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
| `doctor --json [--latest]` | `cli_config.executor` and `cli_config.spec_problems` (phase specs naming the other executor's models), `host` hint (name, sandboxed, network_disabled) and `state_dir`, tools, versions, config locations, effective runner settings, router key presence (never the key), repo cleanliness, active sessions, plus `missing_tools` (runner or router not installed) and `setup_incomplete` (no key, no repo config) for the setup gate in `preflight.md`. `--latest` asks GitHub for the newest releases. |
| `plan --desc TEXT [--flow ID] [--max-restarts N] [-- CLI ARGS]` | Start plan creation (`--plan TEXT`). Default 2 restarts. Prints `session` and `flow`. |
| `run --plan PATH [--flow ID] [--max-restarts N] [-- CLI ARGS]` | Start plan execution. Default 5 restarts. Reuse the flow id of the plan and of earlier runs. |
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
| `hook prompt\|stop` | Plugin hook entry (stdin: hook input JSON). See `control.md`. |
| (supervisor) | Records the owner's terminal tab (`tab` in `session.json`). Unwatched for `AUTOPILOT_WAKE_AFTER_S` (600) with news, it types the wake line into that tab or notifies, every `AUTOPILOT_WAKE_EVERY_S` (1200); after the end it lingers up to `AUTOPILOT_LINGER_S` (6 h) until reported. `AUTOPILOT_WAKE=type\|notify\|off` or `[wake] mode`. Event `wake` (`how`, `why`). |
| `report [SESSION] [--flow ID]` | Every session of a flow (with executor and `models_used` call counts), the merged decision log, `timeouts` (every choice taken without a reply: question, chosen, `waited_min`, `recommended` false when the driver fell back to the first option or a blind accept, reason), `without_user` (every decision the user did not make: by `auto`, `agent`, `autopilot`, `agrouter` or `ralphex`, notes left out), and the branch's commits and diffstat: input for the final report. |
| `stop SESSION` | Interrupt the CLI gracefully, then kill its process tree. |
| `list [--all]` | Sessions of this repository (`--all`: every repository). |
| `config-set FILE KEY [--section S] (--value V \| --value-stdin) [--private]` | Set one INI key, uncommenting a template line when one exists. `--private` makes the file 0600 and its directory 0700. Use `--value-stdin` for secrets. |
| `config-set ... --comment TEXT` | Also write full-line `# TEXT` comments right above the key (once). |
| `config-init [DEST] [--force]` | Copy the runner's commented default config (only `config`, not `agents/` or `prompts/`) to `.ralphex/config`. Keeps an existing file. |
| `install-wrapper --docs F... --doc-dirs D... [--windows] [--force]` | Install the Codex-through-router wrapper into `.ralphex/scripts/` and print the `codex_command` value. |
| `config-show FILE` | Active INI lines with secret values shown as `<set>`. |
| `plan-rev snapshot\|diff\|supersede PLAN [--by NEW]` | Revisions of a finished plan: snapshot before a change; diff against the last snapshot (with task counts, `checked_boxes_lost` and the check verdict); supersede an old plan by a re-planned one (backup kept, `git rm` when committed). See `plan-revision.md`. |
| `review-plan PLAN [--task TEXT] [--reviewer external\|same\|codex\|claude] [--session S] [--executor codex\|claude] [--timeout S]` | Read-only review of a finished plan, routed when the repository routes: `status`, numbered `findings` (`n`, `severity` CRITICAL/MAJOR/MINOR, `text`, from stdout only), `no_findings`, the output file. `--session` uses that session's executor and logs the outcome. An unavailable reviewer returns `ok: false`, `skipped: true`, and `reason`. See `reviews.md`. |
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
`run --plan docs/plans/x.md -- --worktree` or `plan --desc "..." -- --plan-model=opus:high`.
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
| `new_events` | Events after `--since`, each with its fixed `line` (null when the chat skips it): `task-start` (task, tasks, title, model, iteration, continued, steps), `task-done` (task, title, minutes, models, commit, subject, steps), `phase-start` (section, phase, model), `commit` (outside a task), `router-fallback` (agrouter could not route: `code`, `stage`, `message`, `fields` left to the CLI and their `defaults` with value and source), `router-warning` (other `agrouter: warning:` lines, not errors), `stalled` (no output for `AUTOPILOT_STALL_S`, 20 min), `section`, `waiting`, `answered`, `auto-answered` (deadline passed), `auto-answer` (plan's "continue?"), `needs-input` (free-text prompt without a default), `error`, `restart`, `limit`, `limit-cleared`, `failed`, `done`, `stopped`, `stale-answer` |
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
| `executor`, `model` | The run's executor (`claude` or `codex`) and what runs right now (`cli`, `model`, `effort`, from the router's selection line or the Codex banner). Values agrouter left to the CLI are filled from the selection's `effective` part; `fallback` is set when routing failed |
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

Answers are single lines: the driver joins line breaks with spaces. Revision
feedback that needs structure goes into `edited_file` instead, and
`--action edited` sends the diff to the planner as annotated feedback.

The plan session answers "Continue with plan implementation?" with **no**
on its own, so planning never starts execution by itself.

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
**Note:** with `--worktree` the runner copies an uncommitted source plan into
a new worktree, over the branch's ticked copy. So once the branch holds the
plan, the driver continues inside the branch's worktree (recreating it
when the runner removed it) and drops `--worktree`. The runner then works on
the checked-out branch and keeps the ticks. With `use_worktree = true` in a
runner config this cannot be turned off per run, so the driver stops with
`restarting would lose the progress on the run branch` (see `recovery.md`).
