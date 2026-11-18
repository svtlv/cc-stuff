# Claude Code and Codex integration

## Invocation and tools

Invoke `/partner-agents:partner` in Claude Code or `$partner` in Codex. The same arguments, scripts, transcript, and baton protocol apply. Load another skill with Claude's Skill tool when available; otherwise read its installed `SKILL.md` and follow it in the current session. Ask missing configuration with the host's question tool or a direct question. Honor choices already supplied by the user.

Resolve the script from the active skill catalog entry: `<skill-directory>/scripts/partner.py`. Do not assume a cache directory or that hook environment variables are exported in shell calls. Hook commands receive `CLAUDE_PLUGIN_ROOT` in Claude Code and `PLUGIN_ROOT` plus the compatibility alias in Codex. The shared hook entry point also resolves its own location when neither is set.

## Session identity and hooks

Pass `--me-provider claude` or `--me-provider codex` when starting a session. The runtime detects the host when possible, but an explicit provider wins. `init` and `resume` bind the starting participant to `CLAUDE_CODE_SESSION_ID` or `CODEX_THREAD_ID`; hooks compare their payload's `session_id` to that binding. Another session in the checkout must not adopt the starting participant's tab or open its turn. Old rosters containing `cc_session` still identify their owning Claude session.

The shared `Stop`, `UserPromptSubmit` and `PostToolUse` hooks use the event/input/output schema supported by both hosts. Codex requires the installed hooks to be reviewed and trusted before execution. Install the full plugin, then restart the host session. Hook support depends on the client and configuration; an installed skill alone does not guarantee hooks are active. When hooks are unavailable, run `read` at the start of every turn and answer what it shows before ending the turn.

Treat launcher prompts, automated wake-ups, and hook continuation prompts as automation. They never authorize claiming the baton. A real user instruction does: run `claim` before editing.

## Listening without polling

No agent polls. An agent that owes nothing ends its turn and costs nothing until input arrives. A busy agent hears new messages through the hooks: `UserPromptSubmit` adds them to the prompt, and `PostToolUse` adds them after each tool call.

## Listening in Claude Code

Run one `wait --timeout 7000` with Bash `run_in_background: true` and `timeout: 7200000`, then end the turn. The command returns only for a message addressed to you, or just before the 2-hour cap on a background command. Its task notification re-invokes the session. Answer, re-arm one wait, and end the turn again. This is the same for the starting session and for Claude tab partners. Never run it in the foreground. Never kill processes by a shared name to cancel a wait; use `unwait`.

The `Stop` hook holds a Claude agent that ends a turn with no background `wait` armed, because that agent is otherwise deaf. The sleeping wait costs nothing, so the hook can insist on it.

## Listening in Codex

Do not run `wait`. Codex does not re-invoke the model when a background process ends, so a polled wait costs a model turn per poll. End the turn when nothing is owed. When a message leaves the session owing a reply, the waker types a wake-up into its tab, if the tab can be typed into (Orca, tmux, WezTerm, kitty). Otherwise only the user wakes it, by typing in its own session. `read` says which applies.

The `UserPromptSubmit` and `Stop` hooks mark the turn open and closed, so the waker never types into a Codex tab that is mid-turn. The `PostToolUse` hook context in Codex is not yet verified; without it, a busy Codex agent sees new messages at its next `read` or when the `Stop` hook blocks for an owed reply.

See [Claude manifests](https://code.claude.com/docs/en/plugins-reference), [Codex plugin packaging](https://developers.openai.com/plugins/build/plugins), and [Codex hooks](https://learn.chatgpt.com/docs/hooks) for installation and hook support.
