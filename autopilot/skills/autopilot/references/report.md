# Final Report

Write the final report whenever a flow ends: done, failed after recovery,
stopped by the user, or rejected at the plan stage. It explains what was
asked, what was decided and by whom, what was built, what went wrong and
how it was handled, and what is left.

## Gather

```
AP report --flow <flow>
```

returns every session of the flow (plan, runs, recovery runs) with status,
duration, attempts, restarts, errors and phases; the full decision log in
time order; `timeouts`, every choice taken without a reply; and, on the run's branch, the commits and a diffstat against
the default branch. Also read the final plan file (it may have moved to
`<plans_dir>/completed/`).

For "what was built", explain the branch changes with code-explainer's
`explain-summarize` on the branch (`branch` or `changes` target) when it is
available; otherwise read the diff of the main files and explain them
directly. Large diffs may go to a read-only subagent for the walkthrough.

## Layout

Keep sentences short; prefer tables and diagrams; put a **Note:** under
every diagram.

```
# Autopilot report: <task in a few words>

**Result:** done | partly done | failed | stopped · <branch> · <N> commits · <duration>
**Executor:** Claude setup | Codex setup · models used, planning and run separately: <model (effort) × calls, from each session's models_used>

## Summary
3–6 bullets: the goal, the outcome, the most important decisions,
anything the user must do now.

## What was asked
The task description and the plan file path, with the plan's tasks as a
table: task, status ✓/✗/–, model(s), minutes, commit. Take the last three
from each run session's `task_log` (models per task as the router picked
them, `iterations` when a task took more than one). List plan revisions (who asked, what changed)
and superseded plan backups.

## Critical decisions
| When | Decision | By | Why |
Every entry of the decision log that changed the outcome: planner answers,
draft accept/revise, implement or stop, auto-defaults (mark "auto, no reply
in <N> min"), recoveries, bypasses, code fixes. Skip routine noise.

## Decided without you
| When | Question | Chosen | Waited | Why |
Every entry of `timeouts`, none skipped, even routine ones: planner
questions, draft review, runner prompts, implement or start, limits,
recovery, preflight. "Why" is the reason the option was the recommendation.
When `recommended` is false, say so plainly ("no recommendation was ready:
took the first option" / "accepted the draft unreviewed") and flag it for
review. End with one line on what can still be changed (revise the plan,
re-run a task, revert a commit). "None: every decision was yours" when
the list is empty.

## What was built
The explain-summarize output for the branch: summary, then the walkthrough
with diagrams and file:line links. Then the commit list.

## Problems and recovery
| Time | Problem | What autopilot did | Outcome |
Restarts, failures, fixes with commit hashes. Every entry of each session's
`router_fallbacks` too: the task or step, why agrouter failed (`message`,
`code`), and the defaults it ran on (model and effort, verified or
unknown). "None" when clean.

## State now
Branch, worktree (if any), plan location, tests/validation status from the
last run, sessions still alive (should be none).

## Next steps
1–3 concrete actions: review and open a PR, re-run a failed task, enter a
missing key, follow-ups the planner deferred.
```

## Delivery

- Show the report in the chat.
- Save it as `report.md` in the run session directory (or the plan session
  when no run happened) and give the path. Offer once to copy it into the
  repository (for example next to the plan in `completed/`); do not commit
  it unasked.
- When any decision was taken on a timeout, start the report with one
  line saying how many and pointing to "Decided without you", so the user
  sees which choices to review.
