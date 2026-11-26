# Staying in Charge of a Run

The chat that starts a run owns it. The user can ask that chat anything
about the run and have it act, at any time, while the run continues in its
own process. This file covers what the chat does for each request, and how
it keeps watching through usage limits, restarts and new chats.

```
user ──► owner chat ──► AP commands ──► supervisor ──► ralphex
              ▲                            │
              ├── one background next ◄────┤
              └── wake line, if unwatched ◄┘   (+ hooks, + Claude cron)
```
**Note:** the supervisor runs the build without the chat. The chat keeps
exactly one `AP next` in flight to hear about it, and answers the user in
between.

## Mid-Run Requests

The runner commits on the branch while it works, so the chat never edits
repository files during a run. Changes go through a safe point: `AP stop
<session> --after-task` lets the current task finish and commit (during a
review, the current review round), then stops. `AP resume <session>` starts
again with the same plan, flags and flow, from the first open task.

| User asks | Action |
|---|---|
| "What is it doing?", "how far is it?" | Answer from the last report: `tasks`, the last `updates`, and `activity` in plain words |
| "What did it build so far?", "explain task 3" | Read the run branch: `git log --oneline <base>..HEAD`, `git show <commit>`, the plan. Explain with code-explainer's `explain-summarize` on the branch when available. Read only; the run continues |
| "Run the tests", "does it build?" | Not in the working tree while the runner uses it. Say what the last task's tests reported (from the log), or pause after the task and run them |
| "Fix X", "change this code" | Offer: add X as a new task at the end of the plan (the runner builds it; plan edits wait for a safe point), or pause after this task, fix it on the branch, commit, resume |
| "Change task N", "skip task N", "add a task" | Pause after this task, revise per `plan-revision.md` (mark a skipped task's boxes done with a note, never delete finished work), show the diff, resume |
| "Use another model for the rest" | Pause after this task, change the router or runner setting (`autopilot-setup` reconfigure, or a runner flag on resume), resume |
| "Pause", "stop after this task" | `AP stop <session> --after-task`; "never mind" → `AP stop <session> --cancel` |
| "Resume", "continue" | `AP resume <session>`, then watch the new session |
| "Stop now" | `AP stop <session>`: immediate; the current task may be left half done (the next resume redoes it) |
| "Skip the external review" | `AP skip-review <session>` |
| A question the runner is waiting on | Answer it (`AP answer`), per "Unattended Defaults" in `monitoring.md` |

Log every action the user asked for with `AP log <session> --kind decision
--by user --text ... --reason ...` (the driver logs `stop --after-task` by
itself). Each stop and resume shows up as a fixed line (`⏸ Will stop after
...`, `■ Stopped after ...`).

## One Watcher

- Keep exactly one `next` running per live run. Start the new one before
  posting the updates of the last one, so a limit mid-turn still leaves a
  watcher behind.
- A new `next` (or `wait`) takes over from an older one; the older exits
  with "a newer watch took over" (`superseded: true` from `wait`). Ignore
  such a result: do not post it, do not start another one for it. Never
  `pkill` watchers.
- Answering the user does not touch the watcher: the background `next`
  keeps running.

## Waking the Chat

A usage limit can end the chat's turn with no watcher left, and after the
reset most hosts never wake an idle chat by themselves. The run does not
need the chat, but the user does. The supervisor is always alive, so it
does the waking, for any provider:

```
run unwatched ≥ 10 min with news (task done, failure, question, end)
    │
    ├─ owner tab can be typed into ─► types one line into it ─► the agent reads a user message ─► reattach
    │    (Orca tab, tmux, WezTerm, kitty or iTerm2)
    │
    └─ otherwise ─► desktop notification (macOS, Linux) ─► the user opens the chat ─► hook or skill ─► reattach
```
**Note:** typing works with Claude Code, Codex, Gemini or any other agent
CLI, because the wake line is an ordinary user message. While the agent is
still limited the message fails or queues; the supervisor repeats it every
20 minutes until a chat watches again. After the run ends it keeps trying
for up to 6 hours, until the end is reported.

The wake line is `autopilot: reattach to run <session> (<why>): ...`. On
receiving it, follow "Reattach"; it is automation, not a user instruction.

| Host or terminal | What wakes the chat after its limit |
|---|---|
| Any agent CLI in an Orca tab, tmux, WezTerm, kitty (remote control on) or iTerm2 | The typed wake line |
| Any agent CLI in another terminal (Terminal.app, VS Code, Windows Terminal) | The notification |
| Orca chat sessions | Orca's own "Continue where you left off" |
| Claude Code, also | Optional watchdog cron (below) |

Waking is always on and is not configurable.

The tab is recorded from the terminal's own variables (`ORCA_TERMINAL_HANDLE`,
`TMUX_PANE`, `WEZTERM_PANE`, `KITTY_WINDOW_ID`, `ITERM_SESSION_ID`) by `run`
and by every `next` or `wait`, so it always points at the chat that watched last.

### Watchdog Cron (Claude Code, Optional)

Where the tab cannot be typed into, Claude Code can still wake itself:
create a session cron job with `CronCreate` when the run starts (recurring,
every 20 minutes at an off minute, for example `7,27,47 * * * *`):

```
autopilot watchdog: run `python3 "<driver>" watch-status <session>`. If it is watched, or
nothing is unseen and the run is alive, end the turn with no message. Otherwise reattach
per "Reattach" in control.md.
```

Delete it with `CronDelete` after the final report. Cron jobs fire only
while the chat is idle, live only as long as this chat, and expire after 7
days. Skip it when the owner tab can be typed into.

### Hooks (Claude Code, Codex)

The plugin's `UserPromptSubmit` and `Stop` hooks (the schema both hosts
share) run `autopilot.py hook` and stay silent unless a run in this
repository needs attention:

| Hook | Says or does |
|---|---|
| `UserPromptSubmit` | Adds context to the user's message: which run is live with no watcher (or ended unreported), how many updates were missed, and whether this chat started it |
| `Stop` | Only in the owner chat: blocks ending the turn while its live run has no watcher, or its finished run has no report, and names the command to run. It also blocks once when update lines that `AP next` gave the chat are not in the last message of the turn, and lists them: write them as the last message |

`AP next` keeps the update lines it gave the chat in `to-post.json` until
the `Stop` hook compares them with the last message of the turn
(`last_assistant_message` from the host, else the text after the last tool
call in the Claude Code transcript). Only that message stays in the chat
history. Each check clears the
list, so it never blocks in a loop. When the host gives neither, the check
passes.

The owner is the chat that started the run or last watched it
(`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`). Another chat answers the user
first and offers to take charge; it never takes over unasked. Codex runs
hooks only after the user trusts them. Hosts without hooks rely on the
typed wake line, and on every autopilot skill starting with `AP
watch-status`.

## Reattach

After a limit, a restart of the chat, or a new chat taking charge:

1. `AP watch-status` (all runs of the repository) or `AP watch-status <session>`.
2. Start `AP next <session> --catch-up` in the background: it continues
   after the last reported event and also returns an open prompt that an
   earlier chat saw, so it returns at once with everything missed.
3. Post the missed lines as one block, headed `While I was away (<from>–<to>):`.
   Then continue the normal loop. Answer any open runner prompt; act on an
   expired agent timer per `response-times.md`.
4. A run that ended meanwhile: post the missed lines, write the final report
   (`report.md`), then `AP mark-reported <session>`. A `stopped` run waits for
   "resume"; a `failed` one goes through `recovery.md`.

The wake line, the watchdog and the hooks all lead here, so a run is never
left without its chat for long after a limit resets.
