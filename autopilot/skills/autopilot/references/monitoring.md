# Monitoring a Session

Stay with the session until it ends. Keep the user informed with short
updates and never go silent for longer than one `wait` timeout.

## The Loop

```
start ─► wait ─► report ─┬─► heartbeat ─► short "still working" update ─┐
                         ├─► waiting   ─► ask the user ─► answer ───────┤
                         ├─► error/restart ─► show the error ───────────┤
                         └─► done/failed/stopped/lost ─► final report   │
                  ▲                                                     │
                  └─────────────────────────────────────────────────────┘
```
**Note:** each pass is one `wait` call. Pass the previous report's `seq` as
`--since` so every event is reported exactly once.

| Host capability (`hosts.md`) | How to run `wait` |
|---|---|
| Background command with completion notice (Claude Code) | Run `wait --timeout 240` in the background. Post the update, end the turn, and continue when the notice arrives. The user can keep talking in between. |
| Async process polling (Codex) | Start `wait --timeout 240`, poll that process until it exits, post the update, loop. Keep the turn active. |
| Neither (other hosts) | Run `wait --timeout 60` in the foreground, post updates between calls, loop. |

While a prompt is open (`status: waiting`), use a short timeout such as 60 s
for the next call after answering, so follow-up questions arrive quickly.

## Unattended Defaults

Every question gets a recommended answer and a response time from the
policy in `response-times.md` (10 min for planner questions, 30 min for the
draft, 60 min for implement-or-stop, ...). Without a reply in that time,
the recommendation is taken and the work continues to the end. Read
`response-times.md` before the first question.

**Runner prompts** (planner questions, draft review, prompts during a run)
are timed by the driver, so they fire even when the chat is idle:

1. On a new `waiting` report, pick the recommended answer before asking:
   - question/choice: the option that best fits the task, the codebase and
     the user's earlier answers (read the relevant code briefly when the
     choice depends on it); prefer the simplest option that fully meets the
     task;
   - draft: `accept`, unless reading the draft shows a concrete gap (a
     requirement from the task missing, a wrong file, no tests): then
     `revise` with that gap as feedback;
   - yes/no: the answer that keeps work safe and moving.
2. Register it: `AP recommend <session> <answer flags> --reason "<one line why>"`.
3. Ask in **plain text**, not a blocking question tool, so the monitor keeps
   running: question, numbered options, the recommended one marked, and the
   clock time of the auto-answer from `auto_answer_in_s` ("Asking" in
   `response-times.md`).
4. Keep calling `wait --since <seq>`: an open prompt wakes the agent only
   once, then `wait` blocks until a `reminder`, the reply's effect, or an
   `auto-answered` event. A reply → `AP answer <session> ...` (add
   `--reason` when the user explained); "more time" → `AP extend`. An
   `auto-answered` event → tell the user what was chosen and why.
5. Late replies: see `response-times.md`.

Without a registered recommendation the driver falls back to the first
option, accepting the draft, or "no", and logs that as the reason. Prompts
whose policy is `never` (free text, plan choice) wait for the user.

**Agent-level questions** (`decide`, `run_start`, `limit`, `recovery`,
`preflight`) use `AP ask-timer` with the policy key: start it when asking,
`wait` in chunks, `cancel` on a reply, act on `expired`, post a reminder on
`reminder` ("Two Kinds of Timers" in `response-times.md`). Log the outcome
with `AP log <session> --kind decision ...`.

## Logging Critical Decisions

Everything the final report explains must be in the decision log. The
driver logs runner answers (by user, by auto default, by autopilot) and its
own restarts. Log the rest as it happens:

```
AP log <session> --kind decision --text "Implement now (auto: no reply in 60 min)" --reason "plan accepted, 6 tasks"
AP log <session> --kind recovery --text "Committed WIP in the worktree, resumed its branch" --reason "worktree already exists"
AP log <session> --kind fix --text "a1b2c3d: pin protobuf to 4.x" --reason "codegen broke on 5.x"
```

## Update Formats

Keep every update to two or three lines. Paraphrase `activity` in plain
words; quote the CLI only for errors.

Heartbeat:

```
▶ Still working · 23 min · task 3 of 7: "Add login endpoint"
  Phase: implementing tasks (iteration 4)
  Now: adding the handler test and running go test · on gpt-6.1-sol (high)
```

Add the `model` field when known, so the user sees what the router chose.
Phases depend on the executor: the Claude setup runs `claude review N` then
the external `codex iteration N`; the Codex setup runs `review N` and has
no external review.

Phase change (only with `--any`, or when a `section` event comes along
with another event):

```
▶ Tasks done (7 of 7). Starting code review 1.
```

Error with an automatic restart:

```
⚠ Attempt 2 failed: ERROR: claude execution: signal: killed
  Restarting automatically in 60 s (restart 2 of 5). Finished tasks are kept.
```

Limit wait (see `limits.md`; use `--timeout 900` while limited):

```
⏸ Claude usage limit (weekly quota) · resets Mon 15:00, in 2 d 3 h
  ralphex retries every 15 min on its own; finished tasks are kept.
```

Final report: always, whatever the outcome, per `report.md`.

## Failure Triage

| Report | Action |
|---|---|
| `limit` event / `limit` field set | Not a failure: the runner or the driver is waiting on purpose. Report it per `limits.md`; never stop or restart a quiet limited run. |
| `failed` with category `limit` | The reset is far away: decide per "Long Limits" in `limits.md`. |
| `failed` or `lost` | Show `last_error` and `recent_errors` in one or two lines, then recover on your own per `recovery.md`: apply the known fix or diagnose, log it, start again with the same `--flow`. Stop only when recovery needs the user or the 3 rounds are used. |
| `stale-answer` event | An answer arrived for an old prompt and was dropped. Call `wait` and answer the current prompt. |
| `needs-input` event | See "Unattended Defaults". |

Never edit files in the repository while a run is active: the runner commits
its own work, and parallel edits collide with it. Make fixes after the
session has stopped.

## Delegating to Subagents

When the host supports subagents, delegate work that does not need the
user's attention and can run beside the monitor loop:

- **Failure diagnosis.** After a `failed` report, or when the same error
  appears twice, start a read-only subagent with the log path, the plan path
  and the error lines. Ask for the root cause and a fix proposal. Keep
  monitoring meanwhile.
- **Progress digests.** For very long runs, a subagent can read the recent
  commits (`git log`) and summarize what changed so far.

Keep subagents read-only while the run is active. Without subagent support,
do the same work inline between `wait` calls.
