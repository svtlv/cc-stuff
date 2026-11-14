# Reviews by the Other Model

Two independent reviews can check the work. A different model reviews what
the main one produced, so it catches what that model missed.

| Review | Owner | Reviews | Default |
|---|---|---|---|
| **External code review** | The runner (ralphex), after the tasks | The branch's code changes | `external_review_tool = codex` in the Claude setup; skipped in the Codex setup |
| **Plan review** | autopilot (`AP review-plan`), at the draft | The plan, read-only | Off until setup turns it on (`.autopilot/config`, `[review] plan`) |

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

When plan review is enabled and the reviewer is usable, review each draft
before recommending an answer. Use the session's actual `executor` from
`AP wait` or the start result, including any fallback, to choose the other
model. Pass `--executor <executor>` to `review-plan` so `auto` uses that
executor rather than the repository's configured one:

1. `AP review-plan <draft_file> --executor <executor> --task "<task description>"`. It runs the
   reviewer read-only (Codex: `<codex_command> exec --sandbox read-only`;
   Claude: `--permission-mode plan`), routed by agrouter when the repository
   routes. It takes minutes, so run it in the background where the host
   allows, inside the draft's 30-minute window.
2. Read `findings` (`[high|medium|low] finding -> change`) or `no_findings`.
3. Recommend **revise** with the high and medium findings as feedback, or
   **accept** when nothing important came up. Show the findings to the user
   with the draft digest, labelled with the reviewer.
4. Log it: `AP log <session> --kind decision --text "Plan review (codex): 2
   findings, recommended revise"`.

A failed or timed-out review (`ok: false`) never blocks planning. An
unavailable reviewer returns `skipped: true` with its `reason` and no model
call. Say why in one line, log the skip, and recommend from the agent's
own reading. An unverified Claude auth status is reported as unverified;
offer `claude auth status` to check it, or keep the optional review skipped.

Honor "skip the Claude review", "skip the Codex review", or "skip the
plan review" for the current draft. Do not invoke `review-plan`; log the
user's choice and continue. For a permanent plan-review skip, set
`[review] plan = off` through setup. Keep external code-review skips
separate: `skip-review` controls the runner's external review only.

The user can also ask any time: "review the plan with codex" →
`AP review-plan <plan> --reviewer codex`, for a draft, a finished plan
or a plan passed to `autopilot-run`. Use `--reviewer claude` for the same
request naming Claude.
