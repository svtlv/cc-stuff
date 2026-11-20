# Monitoring a Session

Stay with the session until it ends. Post updates only when something
happened: the driver turns every milestone into one fixed line, and the
chat shows those lines, never a status on a schedule.

## The Loop

```
start ─► AP next ─┬─► NO NEWS ─► post nothing ─────────────────────────┐
                  ├─► update lines ─► post each line as is ────────────┤
                  ├─► PROMPT ─► recommend once, ask the user ──────────┤
                  ├─► FAILED/LOST/stalled ─► recover ──────────────────┤
                  └─► DONE/STOPPED ─► final report                     │
           ▲                                                           │
           └───────────────────────────────────────────────────────────┘
```
**Note:** each pass is one `AP next` call. It remembers what it already
reported (`seen.json`), so the chat passes no counter. One prompt wakes the
chat once. "NO NEWS" comes only when the timeout passes.

| Host capability (`hosts.md`) | How to run `next` |
|---|---|
| Background command with completion notice (Claude Code) | Run `AP next <session>` with `run_in_background` and the Bash `timeout` at 7200000 ms. Post the lines, end the turn, and continue when the notice arrives. The user can talk in between. |
| Async process polling (Codex) | Start `AP next <session>`, poll that process with long yields until it exits, post the lines, loop. Keep the turn active. |
| Neither (other hosts) | Run `AP next <session> --timeout 60` in the foreground, post the lines between calls, loop. |

Keep exactly one `next` per run: start the next one after posting, never
start a second one beside it, never kill it ("One Watcher" in
`control.md`). A result "a newer watch took over" needs no action.

### Token Budget

Every wake of the chat reads the whole context again (100 k tokens and
more). Wake it only for news:

- Never pass a short `--timeout` to `next` in a host with completion
  notices. The default (115 min) stays under the 2 h background limit.
- Read the `next` output file with one plain `cat`. Do not parse it with
  scripts: it is short text, not JSON.
- Do not run other background commands (reviews, timers, `sleep`) while a
  prompt is open. The driver owns the auto-answer timer.
- Register a recommendation once per prompt. Do not repeat it with the
  same text.
- `AP wait` still gives the full JSON report for diagnosis. Use it in
  the foreground with `--timeout 1` when the text is not enough.

## Unattended Defaults

Every question gets a recommended answer and a response time from the
policy in `response-times.md` (10 min for planner questions, 30 min for the
draft, 60 min for implement-or-stop, ...). Without a reply in that time,
the recommendation is taken and the work continues to the end. Read
`response-times.md` before the first question.

**Runner prompts** (planner questions, draft review, plan review, prompts during a run)
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
   clock time of the auto-answer from the `PROMPT` line ("Asking" in
   `response-times.md`).
4. Keep `AP next` running: an open prompt wakes the agent only once, then
   `next` blocks until a `reminder`, the reply's effect, or an
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
own restarts. Log the rest as it happens. Every agent-level choice taken
on an expired `ask-timer` uses `--by auto` with `--question` (what was
asked), `--waited` (minutes, `waited_min` from `ask-timer`) and `--reason`
(why that option was the recommendation): the report lists these in their
own section.

```
AP log <session> --kind decision --by auto --question "Implement the plan now?" --waited 60 --text "Implement in a new worktree" --reason "plan accepted, 6 tasks, tests included; planned on master"
AP log <session> --kind recovery --text "Committed WIP in the worktree, resumed its branch" --reason "worktree already exists"
AP log <session> --kind fix --text "a1b2c3d: pin protobuf to 4.x" --reason "codegen broke on 5.x"
```

## Update Formats

Post the update lines of `next` exactly as written, in order. Task updates
have a second line: `Plan:` (the task's planned steps) on start, `Done:`
(the ticked steps and the files the commit changed) on finish:
the same event always reads the same way, so a run's chat is a clean
timeline. Do not paraphrase `activity`, add "still working" lines or
re-post the task list. A sample run:

```
▶ Task 1/13 started: Data model and schema migration · claude-opus-5-5 (xhigh) · chosen by agrouter · steps 0/67
   Plan: add DigestSetup model; add migration 0042; write model tests (+3 more)
✓ Task 1/13 done in 9 min: Data model and schema migration · claude-opus-5-5 (xhigh) · steps 6/67 · 3f2a1c9 Add digest setup model
   Done: add DigestSetup model; add migration 0042; write model tests (+3 more) · 4 files (+212 -3): models.py, 0042_digest.py, test_models.py, admin.py
▶ Task 2/13 started: Seed migration for today's digest · claude-opus-5-5 (xhigh) · chosen by agrouter · steps 6/67
   Plan: seed today's digest from settings; test the seed (+1 more)
⚠ Attempt failed (transient): ERROR: claude execution: signal: killed. Restarting in 60 s (restart 1 of 5); finished tasks are kept.
⚠ Router fallback on Task 2/13: Seed migration for today's digest: Model routing timed out (classifier_timeout, classify stage). Not chosen by agrouter; runs on claude defaults: model claude-opus-5-5 (verified default), effort unknown (claude did not report it).
▶ Task 2/13 continued (iteration 3): Seed migration for today's digest · claude-opus-5-5 (default effort) [router fallback] · claude default (router fallback) · steps 8/67
✓ Task 13/13 done in 4 min: Docs · claude-sonnet-5-5 (high) · steps 67/67 · 9d0e4b2 Update docs
▶ Code review: claude review 1 · claude-opus-5-5 (high) · model chosen by agrouter, effort set by review_model = :high in .ralphex/config
• Commit 1b7c0de: fix: handle empty destinations
… Code review: claude review 1 still running (30 min) · last output 12 min ago · agent active now
▶ External review: codex iteration 1 · gpt-6.2-sol (high) · chosen by agrouter
▶ External review: codex iteration 2 · gpt-6.2-sol (xhigh) · chosen by agrouter
✓ Run finished. Writing the final report.
```
**Note:** every task and every review step gets a start line with its
model and effort and who chose them: "chosen by agrouter", "set by
<key> = <spec> in <config file>" (or a run flag), or the CLI default. When
the model and the effort have different sources, the line names both.
The runner prints the Codex banner only on the first Codex call, so for
later Codex steps autopilot reads the choice from agrouter's log. A task
start line also says what the task is going to do, and a done line with its time, models, commit
and what it did. The plan file is not counted in the changed files: the
runner commits its ticked boxes with the code. When agrouter could not
route a step, a fallback line comes first: why routing failed, and the CLI
defaults it ran on instead. Reviews get one line
each, and commits outside tasks (review fixes) get their own line.

| Event | Line starts with | Extra from the agent |
|---|---|---|
| `task-start`, `phase-start` | `▶` | None |
| `task-done`, `commit` | `✓`, `•` | None |
| `restart`, `error` | `⚠` | None; recovery follows only on `failed` |
| `router-fallback`, `router-warning` | `⚠` | None. A fallback is not a failure: the step runs on the CLI's defaults. When the same code repeats (`classifier_no_key`, `classifier_unauthorized`), say once that routing setup needs `autopilot-setup` after the run |
| `stalled` (no output for 20 min) | `⚠` | Read the last lines of `log` and add one plain sentence on what it is stuck on; recover only when the process is gone |
| `limit`, `limit-cleared` | `⏸`, `▶` | The reset as a clock time, per `limits.md` |
| `auto-answered` | `⏱` | None |
| `failed` | `✗` | One or two lines of diagnosis, then recovery (`recovery.md`) |
| `waiting` | (no line) | Ask the question per "Unattended Defaults" |

When the user asks "what is it doing now?", run `AP wait <session> --timeout 1`
in the foreground and answer from its `activity` in plain words. That is the only time `activity` reaches the chat.

Final report: always, whatever the outcome, per `report.md`.

## Failure Triage

| Report | Action |
|---|---|
| `limit` event / `limit` field set | Not a failure: the runner or the driver is waiting on purpose. Report it per `limits.md`; never stop or restart a quiet limited run. |
| `failed` with category `limit` | The reset is far away: decide per "Long Limits" in `limits.md`. |
| `failed` or `lost` | Show `last_error` and `recent_errors` in one or two lines, then recover on your own per `recovery.md`: apply the known fix or diagnose, log it, start again with the same `--flow`. Stop only when recovery needs the user or the 3 rounds are used. |
| `stale-answer` event | An answer arrived for an old prompt and was dropped. Run `next` and answer the current prompt. |
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
do the same work inline between `next` calls.
