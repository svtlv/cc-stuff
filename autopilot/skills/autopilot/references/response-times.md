# Response Times

Every question autopilot asks has a recommended answer and a response time.
When the time passes without a reply, the recommendation is taken, logged
and the work continues. The time matches how long a careful answer takes
and how costly a wrong default is.

## Policy

`AP policy` prints the effective table. Defaults:

| Key | Decision point | Wait | Reminder | Default action |
|---|---|---|---|---|
| `question` | Planner clarifying question | 10 min | — | recommended option |
| `choice`, `yesno` | Other planner prompts | 10 min | — | recommended / safe answer |
| `draft` | Plan draft review | 30 min | 10 min before | accept, or revise with the gaps found |
| `decide` | Implement or stop, after the plan explanation | 60 min | 15 min before | implement |
| `run_start` | Start a standalone run | 30 min | 10 min before | start |
| `runtime` | Runner prompt during execution (the run is blocked) | 10 min | — | safe answer |
| `limit` | Long rate limit: switch executor or wait | 30 min | — | recommended (waiting continues meanwhile) |
| `recovery` | Recovery step that needs a choice | 15 min | — | recommended safe fix |
| `preflight` | Worktree for a dirty tree, model-spec override, doc drift, active session, incomplete config | 10 min | — | recommended |
| `plan_pick` | Which plan to run when several exist | never | — | waits |
| `plan_review` | "Any changes?" after plan-only planning | 30 min | — | no changes |
| `review_pick` | Review the finished plan: external, same agent, edit, or none | 10 min | — | the reviewer from `[review] plan` (`off`: no review) |
| `review_apply` | Which plan review findings to apply | 15 min | — | the findings the agent's assessment accepts |
| `plan_check` | Redesign a plan the runner cannot use (or run one with warnings) | 15 min | — | redesign (or run as is, for minor warnings) |
| `text` | Free-text prompt without a safe default | never | — | waits |

Never timed, always waiting for the user: the task description, missing
tools, setup (installs, keys, logins) and anything the recovery guardrails
forbid.

Overrides, later wins:

```ini
# ~/.config/autopilot/config  (or the file in AUTOPILOT_CONFIG)
[timeouts]
decide = 2h
draft = 45m
draft.remind = 15m
question = never
```

`AUTOPILOT_TIMEOUTS="decide=2h,question=15m"` overrides for one shell.
Values: `90s`, `10m`, `1h`, `1h30m`, `never`. A session keeps the policy it
started with.

## Two Kinds of Timers

| Asked by | Timer | Commands |
|---|---|---|
| The runner (planner questions, draft, runtime prompts) | The driver's supervisor: fires even when the chat is idle | `recommend` (what to send), `extend`, `answer` |
| The agent (`decide`, `run_start`, `limit`, `recovery`, `preflight`, `plan_pick`, `review_pick`, `review_apply`) | `ask-timer` | `start`, `wait`, `cancel`, `extend` |

Agent timer flow:

```
ask in plain text ─► AP ask-timer start --id <flow>-decide --decision decide
        │
        ▼
AP ask-timer wait --id <flow>-decide ─┬─ reminder ─► post reminder ─► wait again
   (≤ 25 min per call)                ├─ pending  ─► wait again (chunk ended)
                                      ├─ expired  ─► act on the recommendation, log it --by auto
                                      └─ never    ─► no timer: wait for the user
user replies ─► AP ask-timer cancel --id ... ─► act on the reply, log it
```
**Note:** each `wait` call returns within 25 minutes (`--max 1500`), so
background commands in hosts with a 30-minute limit never time out. In
hosts without completion notices, use `--max 60` so a reply is noticed
between calls.

## Asking

Always state the recommendation and the time:

```
Plan explained above. Implement it now?
  1. Implement now (recommended: plan accepted, 6 tasks, tests included)
  2. Implement in a worktree
  3. Revise the plan
  4. Stop here
I'll start implementing at 16:40 (in 60 min) if you don't answer.
Say "wait" for more time.
```

Use the clock time and the duration. For runner prompts, read
`auto_answer_in_s` from the report; it is null when auto-answer is off.

## Reminders

Decisions with a reminder (`draft`, `decide`, `run_start`) get one message
before the deadline: "⏰ 15 min left to review the plan; then I'll start
implementing." Runner prompts raise a `reminder` event; agent timers return
`reminder` from `wait`. When the host can notify outside the chat (for
example a push notification tool), use it for reminders and for the first
ask of `draft` and `decide`.

## More Time, or None

| User says | Action |
|---|---|
| "wait", "give me more time", "I'm reading" | `AP extend <session> --by 30m` (runner prompt) or `AP ask-timer extend --id ... --by 30m`; confirm the new time |
| "I'll answer later", "don't decide for me" | `--never`: no auto-answer for this question |
| "don't wait for me", "decide yourself" | Answer now with the recommendation (`--by agent`), log it |
| "for this run, wait an hour on everything" | Pass `AUTOPILOT_TIMEOUTS` on the next `plan`/`run`, and use `--seconds` on agent timers |

## Late Replies

A reply after the default was taken cannot change that prompt. Say what
was chosen and when, then apply the wish at the next chance: revise the
draft, edit the plan before the run, stop the run, or note it for the
report. A late "stop" stops the run (`AP stop`).
