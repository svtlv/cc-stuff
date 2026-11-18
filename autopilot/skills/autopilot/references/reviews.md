# Reviews by the Other Model

Two independent reviews can check the work. A different model reviews what
the main one produced, so it catches what that model missed.

| Review | Owner | Reviews | Default |
|---|---|---|---|
| **External code review** | The runner (ralphex), after the tasks | The branch's code changes | `external_review_tool = codex` in the Claude setup; skipped in the Codex setup |
| **Plan review** | The runner (ralphex), after it writes the plan file; autopilot relays its questions | The plan, read-only | Ralphex always asks; `[review] plan` (default `auto`) sets the recommended answer, taken after `review_pick` |

## Can Either CLI Review Here?

`doctor` and `config-summary` report `reviews`:

| Field | Meaning |
|---|---|
| `codex.usable`, `codex.reason` | Installed, ≥ 0.130.0, logged in (`codex login status`), not disabled in the agrouter config |
| `claude.usable`, `claude.reason` | Installed, logged in (`claude auth status --json`), not disabled in the agrouter config |
| `execution` | Configured executor, whether it is usable, its reason, possible Codex fallback and any configuration blocker |
| `code_review` | `tool` (`codex`, `custom`, `none`), `usable`, `reason` |
| `plan_review` | `setting` (`off`, `auto` (default), `codex`, `claude`), `external_tool`, the runner's `options` (`External reviewer`, `Same agent`, `Skip review`, each with `usable` and `reason`) and the `recommended` option with `recommended_why` |

`auto` picks External reviewer: the runner's external review tool, Codex
in the Claude setup. Under the Codex executor the runner offers no
External reviewer, so the plan review is a same-agent review there.

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

Ralphex reviews the plan itself (ralphex.windows v1.7.0-windows.4 or
newer). After it writes the plan file, it asks which reviewer to use. The
reviewer reads the plan and the code read-only and prints findings. Ralphex
asks which findings to apply, and a planner session edits the plan file.
Autopilot does not review the plan itself: it relays these questions like
every other ralphex prompt.

```
ralphex: questions ─► drafts ─► accept ─► plan file
                                              │
ralphex: "Review the plan before implementation?" ─► reviewer ─► findings
            (review_pick, 10 min)                                    │
ralphex: "Which findings should be applied?" ─► planner edits the plan ─► ask again
            (review_apply, 15 min)                                   (max 5 rounds)
chat:    relays each question with a recommendation; no reply: the driver answers
```
**Note:** ralphex leads every step and waits for each answer. The chat runs
no review of its own, so the user always sees one open question. Each
question has a default answer, so the flow never blocks on it.

The runner's reviewers:

| Option | Who reviews | Offered |
|---|---|---|
| `External reviewer` | The external review tool (`external_review_tool`: codex, or the custom script) | When that tool is set up and usable; never under the Codex executor |
| `Same agent` | The planning executor, read-only | Always |
| `Skip review` | Nobody | Always |

The default answer when nobody replies and no recommendation is
registered (`default on no reply:` in the `next` text):

| Question | `[review] plan` | Default |
|---|---|---|
| Which reviewer, round 1 | `auto` (default, also with no config) | `External reviewer`; `Same agent` when it is not offered |
| Which reviewer, round 1 | `codex` / `claude` | The option that runs that CLI (External reviewer is Codex, Same agent is the executor), else the other one |
| Which reviewer, round 1 | `off` | `Skip review` (the review is still offered) |
| Which reviewer, after a revision | any | `Skip review`: one round is enough without the user |
| Which findings | any | The CRITICAL and MAJOR findings (`1,3`), else `None` |

1. Relay the reviewer question (`autopilot-plan` Step 4). Register a
   recommendation with `AP recommend <session> --text "<option>" --reason
   "..."`. The driver takes it after `review_pick`.
2. Ralphex runs the reviewer. Its section is `plan review round N`. It
   parses only the reviewer's stdout, in the form `CRITICAL|MAJOR|MINOR:
   <section> - <text>` or `NO ISSUES FOUND`.
3. The findings question shows the numbered findings (`#N SEVERITY:
   text`). Show every finding with your own assessment. The reviewer did
   not see this chat: a finding can be settled already by the user's
   answers, or be noise. Recommend the CRITICAL and MAJOR findings that
   your assessment accepts. Answer with numbers (`1,3`, `2-4`), `All
   findings` or `None`. The driver takes the recommendation after
   `review_apply`.
4. A planner session applies the chosen findings to the plan file.
   Ralphex then asks the reviewer question again.

Ralphex ends the review on `Skip review`, no issues, no findings applied,
5 rounds, or a warning. A failed or timed-out review never blocks the
plan. A read-only guard checks the repository before and after each
review: on a change it warns, restores the plan file, drops that round's
findings and ends the review. Post every `⚠ Plan review:` line. Ralphex
does not revert other changed files: check them with `git status` and
tell the user.

The driver logs each round (`Plan review round 1 (External reviewer): 3
findings (...)`) and each answer in the session's decision log. The final
report lists the answers taken without the user under "Decided without
you".

Honor "skip the plan review": answer `Skip review` and log the user's
choice. For "skip the Codex review" or "skip the Claude review", do not
pick the reviewer that runs that CLI. Pick the other reviewer when ralphex
offers it, else `Skip review`. For no review by default, set `[review] plan =
off` through setup: `Skip review` is then the default. Keep external code
review skips separate: `skip-review` controls the runner's external code
review only.

The user can also ask any time: "review the plan with codex" for a plan
file, or for a plan passed to `autopilot-run`. Start `AP plan-review
<plan> [--flow ID]` and relay it as `autopilot-plan` Step 4 does. Answer
the reviewer question with the reviewer the user named. After the review,
the driver answers no to "Continue with plan implementation?": start the
run as usual.

An older runner (`doctor`: `cli.plan_review: false`) has no plan review.
`plan` then reports `plan_review: false`, and `plan-review` refuses to
start. Upgrade the runner with `autopilot-setup`. Do not review the plan
in another way.
