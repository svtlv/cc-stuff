# autopilot

Take a task from description to implemented code, with you in the loop.
autopilot drives the [ralphex](https://github.com/svtlv/ralphex) plan runner
(the `ralphex.windows` build) and the
[agrouter](https://github.com/svtlv/agrouter) model router from Claude Code
or Codex: it plans with you, explains the plan, asks whether to build it,
then builds it under supervision.

```
 task ─► autopilot-plan ─► explain-summarize ─► implement? ─┬─► autopilot-run ─► report
            (Q&A, drafts)     (plan report)                 └─► stop: plan kept
```
**Note:** `autopilot` chains the other skills. Each one also works on its own.

## Skills

| Skill | Claude Code | Codex | What it does |
|---|---|---|---|
| Full run | `/autopilot:autopilot [task \| plan.md]` | `$autopilot` | Plan → explain → implement or stop. A file path goes to the run, which checks it first. Asks for a description when none is given. |
| Plan only | `/autopilot:autopilot-plan [task \| --redesign file]` | `$autopilot-plan` | Starts the runner's interactive planning and relays every question and draft (accept, revise, edit, reject). Ends with `docs/plans/<name>.md`. `--redesign` turns a design doc or an unsuitable plan into a runnable plan, leaving the original untouched. |
| Run only | `/autopilot:autopilot-run <plan.md> [-- flags]` | `$autopilot-run` | Checks the plan against the runner's format (tasks, checkboxes, tests); an unsuitable one is redesigned with autopilot-plan first. Executes it, reports phase, task and current activity every few minutes, quotes every error and restarts failed attempts automatically. |
| Setup | `/autopilot:autopilot-setup [tools\|key\|repo]` | `$autopilot-setup` | Installs or upgrades both tools, stores the classifier key globally, and writes this repository's config with routed, pinned or fixed models. |

Flags after `--` go to the runner unchanged, for example
`/autopilot:autopilot-run docs/plans/cache.md -- --tasks-only`. `-- --worktree`
means a new worktree, which autopilot makes in `<parent>/workspaces/<repo>/<Summary>`.

## How It Works

Agents run commands without a terminal and cannot keep a process open
between calls, while the runner asks questions in a terminal picker and runs
for hours. A small Python driver (`skills/autopilot/scripts/autopilot.py`,
standard library only) sits between them:

```
agent ──► autopilot.py plan|run ──► detached supervisor ──► runner CLI
  ▲                                   │  stdin pipe, fzf/editor shims
  │                                   ▼
  └──── autopilot.py next ◄──── state.json + output.log
  └──── autopilot.py answer ───► inbox ───► supervisor ───► runner stdin
```
**Note:** planning and the run both go through this path, so agrouter
picks the model for plan creation as well as for tasks and reviews (unless
`plan_model` pins it). The supervisor keeps the runner alive between agent turns. The
agent keeps one `next` in flight (it returns on milestones: a task starts or
finishes, with the model agrouter picked, a review, a commit, a router
fallback, a problem, or a runner prompt it has not shown yet) and replies
with `answer`. Each milestone is one fixed chat line; nothing is posted on a
timer. `next` prints a few lines of plain text and returns only on news, so
a waiting chat costs no tokens.

| Situation | Behavior |
|---|---|
| Planner asks a question | Shown to you with its options; your answer goes back as typed text. |
| Plan draft ready | Digest shown; accept, revise with feedback, edit the draft file directly, or reject. |
| Plan finished, you want changes | Ask any time before the run ("drop task 3", "use Redis instead"), as many rounds as you like. Small changes are edited in place, keeping checked boxes; bigger ones are re-planned. Each round shows a task-level diff and is checked against ralphex's format. A replaced plan is kept as a backup. Mid-run changes stop the run at a safe point, edit, and resume. |
| "Continue with implementation?" | Answered "no" by the plan step; implementation starts only after you choose it. |
| Run fails (crash, kill, network) | Restarted after 30 s, 60 s, 120 s … up to 5 times; finished tasks are kept. |
| Codex can't do the external code review (not installed, logged out) | Setup tells you up front; runs skip the review automatically and say why. If Codex fails mid-review, the run restarts without it, keeping finished tasks. |
| You want to skip the external review | Say so: before the run it starts without it; during the run it ends the review at once (ralphex's manual break) or as soon as the review starts. |
| Plan review | ralphex reviews the plan itself after it writes it (ralphex.windows v1.7.0-windows.4 or newer). It asks which reviewer to use: External reviewer (Codex in the Claude setup), Same agent (read-only), or Skip review. Autopilot relays the question with a recommendation (External reviewer by default, Same agent when ralphex does not offer it) and takes it after 10 minutes without a reply. You see each finding with an assessment and pick which to apply (after 15 minutes: the critical and major ones). The ralphex planner then edits the plan. Drafts are not reviewed by another model, so planning stays one question at a time. `/autopilot-plan --review <file>` reviews an existing plan the same way. |
| Where the implementation runs | Autopilot checks the checkout first. When you planned on the default branch, it recommends a new worktree in the shared workspaces folder (`~/Projects/workspaces/<repo>/<Summary>`, named after the plan title; an existing worktree for the branch is reused, whatever tool made it), so your checkout never switches branch and stays free for more planning. The plan, the repository configs and every file changed during the flow move there in one commit, and your checkout gets those files back as committed. Files you changed before the flow stay in your checkout. When you are already in a worktree or on a feature branch, it recommends staying there, because ralphex makes worktrees only from the default branch. No reply in 60 minutes means the recommended place. A plans folder in `.gitignore` stops the run with a message: plans are committed with the run. |
| Planning ends, the run starts | Autopilot writes a handoff file and compacts the chat (`/compact`, typed into its terminal tab after the turn ends), then wakes it to start the run with a clean context. It needs a terminal autopilot can type into (Orca, tmux, WezTerm, kitty, iTerm2 or a `[wake] command`); otherwise the run starts without compaction. `[compact] mode = off` in `.autopilot/config` turns it off. |
| Which model runs each step | Every task and review line names the model, the effort and who chose them: agrouter, a config key (for example `review_model = :high in .ralphex/config`), a run flag, or the CLI default. The model and effort come from agrouter's log (`.agrouter/logs/agrouter.log`) for every step; ralphex's output is only a fallback. The final report has the same data per step. |
| Branch, folder and pull request names | Autopilot follows a convention and never asks for a ticket. The branch is `{type}/{slug}`: `feature/user-service`, or `bugfix/login-timeout` when the plan names a fix. The pull request title is the plan title, for example `Add the user service`. To use your own convention, set `[worktree] branch`, `path` and `pr_title` in `.autopilot/config` (placeholders `{type}`, `{slug}`, `{summary}`). To use one specific name, say it in the chat. Autopilot shows the names in the decide question. |
| A long step | Every 30 minutes without news, a step gets a "still running" line with the time since the last output and since the agent last wrote its transcript. A run is stalled only when both are old. |
| Only Codex is available | Setup recommends Codex. Under the Codex executor, ralphex offers no External reviewer, so the plan review is a same-agent review. Claude availability includes login and router checks. A Claude-configured session can fall back to Codex when its sandbox and routing support execution; otherwise setup explains what to change. |
| You want to skip a plan review | Say "skip the Claude review", "skip the Codex review", or "skip the plan review". The plan is kept as written and the skip is logged. A missing or logged-out reviewer is skipped automatically. |
| Usage limit | ralphex retries the same step every 15 minutes (autopilot adds `--wait` when your config lacks `wait_on_limit`); if ralphex exits instead, autopilot waits until the reported reset time and restarts. You see which tool is limited and when it resets, never a false "hung" or error. A reset hours away gets a decision with a 5-minute default: continue on Codex, skip the limited external review, or wait. |
| Same error three times with no progress | The driver stops; the agent diagnoses and fixes. |
| Driver gives up (fatal error, restart limit) | The agent recovers on its own: known fixes (worktree instead of a dirty tree, stale worktree cleanup, config fixes, router bypass when the key fails) or a diagnosed code/plan fix committed on the run's branch, then starts again. Up to 3 rounds; never destructive git commands. |
| You don't answer | Each question has a recommended answer, chosen with a reason, and a response time that fits it: 10 min for planner questions, 30 min for the plan draft, 60 min to decide whether to implement (reminders before the long ones; say "wait" for more). Then the recommendation is taken and the work continues to the end. Times are configurable in the repository's `.autopilot/config` (committed) or your `~/.config/autopilot/config`. Installing software and credentials always wait for you. |
| You ask about or change the run mid-way | The chat that started it stays in charge: it answers from the log and the branch's commits while the run continues. Code, plan or model changes go through a safe point: "pause after this task", change, "resume". |
| The chat hits its own usage limit, or you open a new chat | The run continues by itself. When no chat has watched for 10 minutes and there is news, the supervisor types a wake line into the chat's terminal tab (Orca tab, tmux, WezTerm, kitty, iTerm2, or your own `[wake] command`), which works for Claude Code, Codex or any other agent CLI; otherwise it shows a desktop notification. The chat then posts the updates you missed. |
| Run finished | Autopilot pushes the run branch and opens a pull request into the branch the run started from (for example `master` for a worktree run planned on `master`), or into the default branch when that branch is not on the remote. An open pull request is reused, and a run that did not finish opens a draft. It needs `gh` or `glab`; without them it pushes only. No config changes this: tell the chat "push only", "keep it local" or "open a draft" during the flow. |
| Flow ends (any outcome) | A short report in two main parts: first every decision made without you (with why and how to change it), then the goal and what was done. Open items follow only when you must act. Saved next to the session state. |

In hosts that notify on background commands (Claude Code) the monitor runs
in the background, so you can keep chatting during a run; elsewhere it runs
in the foreground with commentary updates (see the table under Install).
When the host supports subagents, autopilot may use read-only subagents to
diagnose failures while the run continues.

### Hooks

The plugin ships `UserPromptSubmit` and `Stop` hooks (`hooks/hooks.json`,
the schema Claude Code and Codex share). They print nothing unless a run in
the repository has no chat watching it, or ended without a report. Then they
tell the chat, and keep the chat that started the run from ending its turn
without a watcher. Codex runs them after you trust them in its hook review.
Hosts without hooks still get the typed wake line. See
[control.md](skills/autopilot/references/control.md).

Session state lives in `~/.local/state/autopilot/` (`%LOCALAPPDATA%\autopilot`
on Windows; `AUTOPILOT_STATE_DIR` overrides), outside the repository.

## Setup

The other three skills check for ralphex.windows and agrouter first. When
either is missing, they ask to run setup and continue your original request
afterwards; nothing starts without both tools. A missing key or repository
config gets an offer to run setup, with the option to continue anyway.
A newer ralphex.windows or agrouter release gets an offer to upgrade (setup
`tools`), continue, or skip that version. The lookup runs at most once a
day; set `[updates] check` in `.autopilot/config` to change it (`off`
disables it).

Run `/autopilot:autopilot-setup` (or `$autopilot-setup`) directly. It walks through:

1. **Check** — installed and latest versions, configs, key presence.
2. **Install or upgrade** — `ralphex.windows` via Homebrew, the install
   script, the Windows installer or `go install`; `agrouter` via
   `go install` (Go 1.27+).
3. **Classifier and key** — TypeSafe (default) or OpenAI. The key goes
   into `~/.config/agrouter/config` with user-only permissions, never into
   the repository.
4. **Repository config** — executor (Claude Code or Codex), model selection
   (full routing, pinned model with routed effort, fixed models, or no
   router), project docs for routing, external review and limit waiting.
   The plan review and publishing are not settings: you steer them in the
   chat. Setup always writes full configs for all three tools, so
   a clone works for a user with no global config:

   ```
   .ralphex/config                     runner template, routed keys explained
   .ralphex/scripts/codex-agrouter.sh  Codex through agrouter
   .agrouter/config                    router template: classifier provider, Codex quota prefix,
                                       disabled CLIs, long-context limits for large doc sets
   .autopilot/config                   autopilot template: update check, worktree path, response times
   ```
   **Note:** on Windows the wrapper is `codex-agrouter.cmd` plus
   `codex-agrouter.py`. These files are safe to commit; the classifier key
   stays in your global config or environment, and setup refuses to write
   it into the repository. Setup recommends committing them, because
   teammates need them and ralphex will not start on the default branch
   with uncommitted config.
5. **Verify** — one routing request to confirm the key and config.

On a repository that is already set up, setup switches to **reconfigure
mode**: it shows every property with its current value and where it comes
from (this repository, your global config, or built in), lists issues such
as doc drift, and changes only the properties you pick — executor, phase
models, routing docs, router on/off, external review, limits, large-doc
routing, routable CLIs, classifier and key, response times, tool versions.
A missing `.agrouter/config` or `.autopilot/config` is listed as an
issue and offered first. It then shows the diff and offers to commit or undo. Hand-written wrapper
scripts are never overwritten without asking.

## Requirements

| Need | For |
|---|---|
| Python 3.9+, git | the driver |
| `ralphex.windows` or `ralphex` | planning and execution |
| `agrouter` + classifier key | model routing (optional: runs work with fixed models too) |
| `claude` and/or `codex` CLI, logged in | the models doing the work |
| [code-explainer](../code-explainer/) (optional) | the plan report in the full run and the "what was done" bullets in the final report; a compact built-in summary is used without it |

## Install

Claude Code:

```bash
claude plugin marketplace add svtlv/cc-stuff
claude plugin install autopilot@svtlv-cc-stuff
claude plugin install code-explainer@svtlv-cc-stuff   # optional, for plan reports
```

Codex:

```bash
codex plugin marketplace add svtlv/cc-stuff
codex plugin add autopilot@svtlv-cc-stuff
codex plugin add code-explainer@svtlv-cc-stuff        # optional
```

Local development: `claude --plugin-dir ./autopilot`.

Any other agent host that loads `SKILL.md` skills (Gemini CLI, OpenCode,
Cursor, Copilot, ...): copy or symlink the four directories in
`autopilot/skills/` side by side into the host's skills directory (they
reference each other with `../`), plus `code-explainer/skills/*` for plan
reports. Invoke them by name ("use the autopilot skill to add caching").

The skills adapt to what the host can do, not to its name:

| Host can... | Then |
|---|---|
| run shell commands (required) | the driver does all terminal work |
| notify when a background command ends (Claude Code) | monitoring runs in the background and you can chat during a run |
| poll an async process (Codex) | monitoring polls inside the turn |
| neither | monitoring loops in the foreground with short waits |
| load skills by name, ask with a question tool, start subagents | used when present; otherwise files are read directly, questions go in plain text, work is done inline |

Sandboxed shells need full access (network and long-lived processes) for
autopilot commands; session state falls back to `.git/autopilot` when the
home directory is not writable. The host and the executor are independent:
a Codex or Gemini host can drive runs executed by Claude Code, and the
other way round.

## Limitations

- Native Windows scripts use Python (`py -3` or `python`) and the
  `codex-agrouter.cmd` + `.py` wrapper. See the
  [Windows guide](skills/autopilot/references/windows.md). CI runs the driver
  suite on Windows and Linux; real ralphex/model calls still need an
  integration run.
- Planning runs through the runner's numbered-answer fallback, so the
  planner's questions arrive as plain text, not a fuzzy picker.
- Waking by typing needs a terminal that can be typed into from outside
  (or a `[wake] command`); elsewhere you get a desktop notification on macOS
  and Linux, none on Windows. A closed terminal leaves the run going; the
  hooks or `/autopilot-run` pick it up in the next chat in the repository.
- Restarts rely on the runner resuming from the first unchecked task. A run
  in a new worktree restarts in place in its folder; the folder is kept
  after the run until you remove it.
