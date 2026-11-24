# Response Times

Every question autopilot asks has a recommended answer and a response time.
Trivial questions get the recommendation at once. When the time of a
critical question passes without a reply, the recommendation is taken,
logged and the work continues. The time matches how long a careful answer takes
and how costly a wrong default is.

## Critical or Trivial

Ask the user only the critical questions. Answer the trivial ones at once
with the recommendation, so the flow does not stop on each small choice.

```
PROMPT ─► pick the recommendation ─► triage ─┬─ trivial  ─► answer now (--by agent) ─► post one ⏩ line
                                             └─ critical ─► ask with the response time (table below)
```
**Note:** the triage happens after the recommendation is known. Only a
critical prompt waits for the user.

| Always critical: ask the user | Trivial: answer at once |
|---|---|
| The implement-or-stop decision (`decide`) | The plan draft: accept, or revise with the gaps found |
| The architecture, the data model, a public API or a storage format | The plan review: which reviewer, which findings to apply |
| A new dependency, a library or a service to use | A name: of a file, a class, a branch, a flag or a test |
| Security, credentials, data loss, or changes to shared or remote state | A file location or a code style that the codebase already shows |
| The scope: what is in or out, behavior the user sees | A choice the codebase, the task or an earlier answer settles |
| Two or more good options with real trade-offs and no clear winner | Yes/no questions with one safe answer that keeps work moving |
| A recommendation that goes against an earlier wish of the user | A runtime prompt with one safe answer |
| Recovery steps, long rate limits, plan redesign, active sessions | Preflight: a worktree for a dirty tree, doc drift, a model-spec fix |
| Free text without a safe default, and the plan to run (`plan_pick`) | |

When in doubt, the question is critical. A trivial answer must be easy to
change later: the user still sees the full plan at the `decide` step.

To answer a trivial prompt:

1. Register the recommendation as usual (`AP recommend ...`).
2. Answer it: `AP answer <session> <answer flags> --by agent --reason
   "trivial: <why>"`. For an agent-level question, act on it and log it with
   `AP log <session> --kind decision --by agent --reason "trivial: <why>"`.
3. Post one line: `⏩ <question, short> → <answer> (<why>)`. Do not ask
   for a reply. A later wish of the user applies at the next chance ("Late
   Replies").

## Policy

`AP policy` prints the effective table. Defaults:

| Key | Decision point | Wait | Reminder | Default action |
|---|---|---|---|---|
| `question` | Planner clarifying question | 10 min | — | recommended option |
| `choice`, `yesno` | Other planner prompts | 10 min | — | recommended / safe answer |
| `draft` | Plan draft review | 30 min | 10 min before | accept, or revise with the gaps found |
| `decide` | Implement or stop, after the plan explanation | 60 min | 15 min before | implement in the place `AP place` recommends |
| `run_start` | Start a standalone run | 30 min | 10 min before | start |
| `runtime` | Runner prompt during execution (the run is blocked) | 10 min | — | safe answer |
| `limit` | Long rate limit: switch executor or wait | 30 min | — | recommended (waiting continues meanwhile) |
| `recovery` | Recovery step that needs a choice | 15 min | — | recommended safe fix |
| `preflight` | Worktree for a dirty tree, model-spec override, doc drift, active session, incomplete config, newer tool release (timeout keeps the installed versions) | 10 min | — | recommended |
| `plan_pick` | Which plan to run when several exist | never | — | waits |
| `plan_review` | "Any changes?" after plan-only planning | 30 min | — | no changes |
| `review_pick` | The runner's "Review the plan before implementation?": External reviewer, Same agent or Skip review | 10 min | — | External reviewer, else Same agent (after a revision: Skip review) |
| `review_apply` | The runner's "Which findings should be applied?" | 15 min | — | the findings the agent's assessment accepts; with no recommendation, the CRITICAL and MAJOR ones |
| `plan_check` | Redesign a plan the runner cannot use (or run one with warnings) | 15 min | — | redesign (or run as is, for minor warnings) |
| `text` | Free-text prompt without a safe default | never | — | waits |

Never timed, always waiting for the user: the task description, missing
tools, setup (installs, keys, logins) and anything the recovery guardrails
forbid.

Overrides, later wins:

```ini
# <repo>/.autopilot/config (committed, wins) or ~/.config/autopilot/config
# (the user's file, or the file in AUTOPILOT_CONFIG)
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
| The runner (planner questions, draft, plan review, runtime prompts) | The driver's supervisor: fires even when the chat is idle | `recommend` (what to send), `extend`, `answer` |
| The agent (`decide`, `run_start`, `limit`, `recovery`, `preflight`, `plan_pick`) | `ask-timer` | `start`, `wait`, `cancel`, `extend` |

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
  1. Implement in a new worktree (recommended: plan accepted, 6 tasks, tests
     included; planned on master, so the run gets its own worktree)
  2. Implement here, on a new branch
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
