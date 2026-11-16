# Reviews by the Other Model

Two independent reviews can check the work. A different model reviews what
the main one produced, so it catches what that model missed.

| Review | Owner | Reviews | Default |
|---|---|---|---|
| **External code review** | The runner (ralphex), after the tasks | The branch's code changes | `external_review_tool = codex` in the Claude setup; skipped in the Codex setup |
| **Plan review** | autopilot (`AP review-plan`), once, on the finished plan file | The plan, read-only | Offered after planning; `[review] plan` sets the recommended reviewer (`off` recommends no review) |

## Can Either CLI Review Here?

`doctor` and `config-summary` report `reviews`:

| Field | Meaning |
|---|---|
| `codex.usable`, `codex.reason` | Installed, ≥ 0.130.0, logged in (`codex login status`), not disabled in the agrouter config |
| `claude.usable`, `claude.reason` | Installed, logged in (`claude auth status --json`), not disabled in the agrouter config |
| `execution` | Configured executor, whether it is usable, its reason, possible Codex fallback and any configuration blocker |
| `code_review` | `tool` (`codex`, `custom`, `none`), `usable`, `reason` |
| `plan_review` | `setting` (`off`, `auto`, `codex`, `claude`), `reviewer`, `usable`, `cross_model` (the other model for this setup) and whether it is usable |

`auto` picks the other model: Codex reviews plans in the Claude setup,
Claude in the Codex setup.

When only Codex is usable, keep planning and executing on Codex and skip
Claude plan review. Report the reason once. Do the same for Codex reviews
when only Claude is usable. Never substitute a review by the producing
model and describe it as a review by the other model.

If a Claude-configured session cannot start, the driver switches that
session to Codex when Codex is usable and configured for implementation.
It also recovers this way when Claude reports a missing executable or
login failure during planning or execution. Phase model flags are reset
to `:` so Claude model names do not reach Codex; the change is logged and
the repository config stays unchanged. Completed checkboxes are kept.
Quota limits still follow `limits.md`, rather than switching immediately.

An explicit `codex_sandbox = read-only`, a missing Codex command, or a
missing routing wrapper blocks this fallback. Use setup's Codex recipe
to configure execution first. Do not silently relax a configured sandbox.
Explicit external-only or external-review flags also block fallback:
honor the requested review, or let the user skip it before switching.

## External Code Review: Skipping and Recovering

```
run starts ─▶ code review = codex but Codex not usable? ─yes─▶ add --external-review-tool=none, log why
     │
tasks ─▶ claude review ─▶ codex iteration (external review) ─▶ review 2 ─▶ done
                               │
                               ├─ user: "skip it" ─▶ skip-review ─▶ SIGQUIT ─▶ review ends, run goes on
                               └─ Codex fails (missing, logged out) ─▶ restart without it, log why
```
**Note:** the runner treats SIGQUIT as "end the external review loop" only
while that loop runs; during tasks it pauses instead. That is why
`skip-review` sends it only in the review phase and queues the request
otherwise. On Windows (no SIGQUIT) the attempt restarts without the review;
finished tasks are kept either way.

| Situation | What happens | Who acts |
|---|---|---|
| Codex not installed, too old, logged out, or disabled for routing | `run` adds `--external-review-tool=none` and logs the reason | driver, automatically |
| Codex fails during the review | The run restarts with the review skipped (`restart`, category `review-skipped`) | driver, automatically |
| Codex hits a long rate limit during the review | Decide per "Long Limits" in `limits.md`; skipping is one option | agent, with `limit` response time |
| User: "skip the external review" before the run | Start with `-- --external-review-tool=none` | agent |
| User asks the same during the run | `AP skip-review <session>`: now if the review runs, else when it starts (`review-skip-queued` event) | agent |
| For good | Reconfigure: `external_review_tool = none` (setup, property 6) | setup |

Tell the user every skip in one line and include it in the final report:
a run without the external review has had one check fewer.

## Plan Review

Ralphex plan mode has no plan review of its own (backlog item
`plan-mode-has-no-external-plan-review` in ralphex.windows). Autopilot adds
it as one step after ralphex writes the plan file, as `/planning:make`
does with "Auto review". Drafts get no review by another model.

```
ralphex: questions ─► drafts ─► accept ─► plan file
                                              │
autopilot: which review? ─► review-plan ─► findings + assessment ─► which to apply? ─► edit, check
              (review_pick)                                          (review_apply)
```
**Note:** every step waits for the one before it. The chat runs no review
beside a ralphex prompt, so the user always sees one open question.

| Reviewer | `--reviewer` | Who reviews |
|---|---|---|
| External | `external` | The other model: Codex in the Claude setup, Claude in the Codex setup |
| Same agent | `same` | The executor model, read-only |
| By name | `codex`, `claude` | That CLI |

1. `AP review-plan <plan> --reviewer external|same --session <session>
   --task "<task description>"`. It runs the reviewer read-only (Codex:
   `<codex_command> exec --sandbox read-only`; Claude: `--permission-mode
   plan`), routed by agrouter when the repository routes. `--session`
   takes the session's actual executor (after a fallback) and logs the
   outcome in its decision log. It takes minutes: run it in the background
   where the host allows, and start nothing else until it returns.
2. Read `status` and `findings`: each has `n`, `severity` (`CRITICAL`,
   `MAJOR`, `MINOR`) and `text`. Only the reviewer's stdout counts; its
   stderr goes to a `.err` file beside the review file.
3. Show every finding with your own assessment. The reviewer did not see
   this chat: a finding can be settled already by the user's answers, or
   be noise. Recommend applying the CRITICAL and MAJOR findings that your
   assessment accepts.
4. Ask which to apply, with the `review_apply` time. Apply them as a
   direct edit per `plan-revision.md`, then `check-plan`. Log the outcome:
   `AP log <session> --kind decision --text "Applied plan review findings
   1,3" --reason "..."`.

A failed or timed-out review (`ok: false`) never blocks the plan. An
unavailable reviewer returns `skipped: true` with its `reason` and no model
call. Say why in one line and offer the other reviewer or no review. An
unverified Claude auth status is reported as unverified; offer `claude auth
status` to check it.

Honor "skip the plan review", "skip the Codex review" and "skip the Claude
review": go on without it and log the user's choice. For no review offer at
all, set `[review] plan = off` through setup. Keep external code-review
skips separate: `skip-review` controls the runner's external review only.

The user can also ask any time: "review the plan with codex" →
`AP review-plan <plan> --reviewer codex`, for a finished plan or a plan
passed to `autopilot-run`. Use `--reviewer claude` for the same request
naming Claude.
