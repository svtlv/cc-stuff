# Rate Limits and Waits

Three layers react to usage limits. Know which one is acting, so a waiting
run is never mistaken for a hung or failed one, and never waited on twice.

```
 claude / codex hits a limit
        │
        ▼
 agrouter (before starting the CLI): quota probe ─► blocked? ─► exit 127 + one line
        │                                      "agrouter: You've hit your limit; ...; resets at <ISO>"
        ▼
 ralphex matches its *_limit_patterns
        ├─ wait_on_limit set ─► "rate limit detected: ... waiting 15m0s before retry..."
        │                       sleep (silent) ─► retry the same step ─► ... until it passes
        └─ not set ──────────► "error: detected ..." ─► exits
                                        │
                                        ▼
 autopilot supervisor ─► reset known? wait until reset + margin, else 15 min ─► restart run
                       └─ reset more than 6 h away ─► `failed`, category `limit` ─► agent decides
```
**Note:** the runner's in-place retry is the preferred path: it resumes
the exact step and keeps the run's context. That is why the driver adds
`--wait=900s` whenever `wait_on_limit` is not configured.

From ralphex.windows v1.7.0-windows.5, a review round that waits on a
limit continues its own session after the wait. It does not repeat its
research. A restart by the supervisor starts a new runner, which starts
the review round again. See `reviews.md` for when the session continues.

## What the Driver Reports

| Field / event | Meaning |
|---|---|
| `limit` (in every `wait` report) | Active episode: `tool`, `pattern`, `message`, `resets` (text), `resets_in_s`, `retries`, `next_retry_in_s`, `waiting_min`, `handled_by` (`ralphex`, `autopilot` or `agent`) |
| `limit` event | An episode started (once). Wakes the agent. |
| `limit-retry` event | The runner retried and is still limited. Quiet: does not wake the agent. |
| `limit-cleared` event | Work resumed; carries the minutes waited and the step it resumed on: `section`, `title` (a task), `model`, `how`, and `before` when the router chose another model than before the limit. |
| `transient` event | A 5xx/529 API error the runner retries as a session timeout. Quiet. |
| `failed`, category `limit` | The reset is more than 6 h away: the agent decides. |

Limit lines are not counted as errors, and limit restarts do not use up
the restart budget or the "same error three times" rule.

| Fixed value | Effect |
|---|---|
| 15 min | `--wait` added to the runner, and the supervisor's wait when no reset time is known |
| 1 min | Margin added after a known reset time (and the shortest wait) |
| 6 h | Longest wait the supervisor takes alone |

## What to Tell the User

On the `limit` event, one update:

```
⏸ Claude usage limit (weekly quota) · resets Mon 15:00, in 2 d 3 h
  ralphex retries every 15 min on its own; finished tasks are kept.
```

While limited, post nothing on `NO NEWS`: the `limit` line already gave
the reset time, and `limit-cleared` posts the step, the model and effort it
resumed on, and who chose them:

```
▶ Limit cleared after 47 min; work continues on claude review 1: critical/major · claude-sonnet-5-5 (high) (was claude-opus-5-5 (high)) · chosen by agrouter
```

The limit ends when the router selects a model again or new output comes.
The router's "routing skipped: ... quota exhausted" lines (also when the
runner wraps them) belong to the limit: they are not errors and do not end it.

Never call a limited run hung, and never stop or restart it because it is
quiet: the runner is sleeping on purpose.

## Long Limits: the Agent Decides

When the reset is far (category `limit` failure, or `resets_in_s` above
6 hours while the runner waits in place), choose and recommend one option,
ask with the `limit` response time (30 min, `response-times.md`),
and log the choice:

| Situation | Options (recommended first) |
|---|---|
| Claude limited, Codex available (`doctor.tools.codex`, not limited) | 1. Continue on Codex: start again with `-- --codex` (Codex executor; skips the external review). 2. Wait for the reset. 3. Stop and report. |
| Codex limited during the external review only | 1. Finish without the external review: start again with `-- --external-review-tool=none`, noted in the report. 2. Wait. |
| Codex executor limited, Claude available | 1. Wait (switching away from `executor = codex` needs a config change the user should approve). 2. Stop and report. |
| Everything limited | Wait for the earliest reset: `AP timer` in steps (in the background where the host supports it), then start the same run again with the same `--flow`. Report the expected resume time. |

When waiting days, say so plainly in the report and leave the session
stopped rather than holding a process open: the run resumes from the first
unchecked task whenever it is started again.

## Planning Sessions

Plan creation uses the same policy: the runner waits in place, the
driver tracks the episode, questions keep their response times. A limit
during planning never auto-answers anything; a timer only runs while a
prompt is open.

## Configuration Checklist

| Setting | Why |
|---|---|
| `wait_on_limit = 15m` in the repo or global runner config | In-place retries; autopilot adds `--wait` when it is missing |
| `codex_limit_patterns` contains `hit your usage limit` | Matches the router's Codex quota line, so it waits instead of failing |
| Router `[cli.codex.availability.limits] error_prefix = "ERROR: agrouter: "` | The Codex executor only checks severity-prefixed lines |
| Leave `idle_timeout` unset or long | Not involved in limit sleeps, but kills routing pauses when too short |
