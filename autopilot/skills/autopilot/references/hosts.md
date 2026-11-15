# Running Under Any Agent Host

The skills work in any agent that can load a `SKILL.md`, read files and run
shell commands: Claude Code, Codex, Gemini CLI, OpenCode, Cursor, Copilot
and others. Decide by **capability**, not by host name. `doctor` reports a
`host` hint (`claude-code`, `codex`, `gemini-cli`, `opencode` or
`unknown`); trust the tools actually available over the hint.

## Required

For native Windows shells, read `windows.md`: use `py -3` or `python`,
PowerShell syntax and the `.cmd` + `.py` routing wrapper. Git Bash and WSL
are optional. The driver uses the same Python implementation on all platforms.

| Capability | Why | When missing |
|---|---|---|
| Shell commands | Every step calls the driver (`python3`, `git`, the runner) | The skills cannot work; say so and stop. |
| Long-lived processes with network | The driver's supervisor outlives the command that starts it; models need the network | Run autopilot commands with full access (outside the sandbox, or with the host's escalation/approval). `doctor` adds a `setup_incomplete` item when the shell has no network. |
| Reading files | Sibling skills and references are loaded by path | — |

State goes to `~/.local/state/autopilot` (`%LOCALAPPDATA%\autopilot`), or
to `<repo>/.git/autopilot` when a sandbox blocks the home directory. The
driver strips the host's own session variables (`CLAUDECODE`,
`CODEX_THREAD_ID`, `CODEX_SANDBOX`, ...) from the runner's environment, so
a nested `claude` or `codex` never believes it runs inside the host.

## Optional, With Fallbacks

| Capability | Use | Fallback |
|---|---|---|
| Background command with completion notice (Claude Code: `run_in_background`) | Run `wait`/`timer` in the background, end the turn, continue on the notice; the user can talk meanwhile | — |
| Async process polling (Codex: `exec_command` yielding a session, polled with `write_stdin`) | Start `wait`, poll the same process until it exits; keep the turn active | — |
| Neither | Run `wait --timeout 60` and `ask-timer wait --max 60` in the foreground in a loop, posting updates between calls | Runner prompts still auto-answer on the driver's timer |
| Question tool (AskUserQuestion and similar) | Only for setup, which never runs unattended | Numbered options in plain text |
| Terminal that can be typed into (Orca tab, tmux, WezTerm, kitty, iTerm2, or `[wake] command`) | The supervisor wakes the owner chat after its usage limit by typing one line: any provider (`control.md`) | Desktop notification |
| Scheduled prompts (Claude Code: `CronCreate`) | Optional watchdog when the tab cannot be typed into | Typed wake line, notification |
| Plugin hooks (`UserPromptSubmit`, `Stop`: Claude Code, Codex after trust) | Tell a chat about unwatched runs; keep the owner chat watching | `AP watch-status` at the start of any autopilot skill |
| Loading a skill by name (Skill tool, `$name`) | Sibling skills, explain-summarize | Read the `SKILL.md` file and follow it |
| Subagents | Diagnosis, large report walkthroughs | Do the same work inline |

Never block on a question tool while a session runs: it stops monitoring
and recovery. Ask runtime questions in plain text (`monitoring.md`).

A turn that ends while a session runs is fine only when something will wake
the agent again (a completion notice). Without one, keep the turn active
with foreground `wait` calls until the session ends; the run itself
continues either way, and `list`/`wait` reattach later.

## Naming the Commands

Write next-step commands in the form the current host uses:

| Host | Full run | Plan only | Run only | Setup |
|---|---|---|---|---|
| Claude Code (plugin) | `/autopilot:autopilot` | `/autopilot:autopilot-plan` | `/autopilot:autopilot-run` | `/autopilot:autopilot-setup` |
| Codex | `$autopilot` | `$autopilot-plan` | `$autopilot-run` | `$autopilot-setup` |
| Other hosts | the host's skill invocation, or a request such as "use the autopilot-run skill on docs/plans/x.md" |

## Finding explain-summarize

1. The host lists a skill named `explain-summarize` (in Claude Code
   `code-explainer:explain-summarize`): invoke it the host's way.
2. Otherwise read `code-explainer/skills/explain-summarize/SKILL.md` when it
   exists next to this plugin (`<autopilot-skill-dir>/../../../code-explainer/`)
   and follow it.
3. Otherwise write the compact built-in report.

## The Work Itself

The runner plans and executes through Claude Code (`claude_command`, any
Claude-compatible stream-json command) or Codex (`executor = codex`,
`codex_command`), both routed by agrouter in the recipes. The router can choose among every CLI its config
describes, but the executor seen by the runner must speak one of those two
protocols. The host running these skills does not need to match the
executor: a Codex host can drive Claude-executed runs and the other way
round.

## Installing Outside the Plugin Hosts

Copy or symlink the four directories `autopilot`, `autopilot-plan`,
`autopilot-run` and `autopilot-setup` (from `autopilot/skills/`) into a
directory the host loads skills from. Keep them side by side: the skills
reference each other with `../`. Add `code-explainer/skills/*` the same way
for the plan report.
