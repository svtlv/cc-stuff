# Final Report

Write the final report whenever a flow ends: done, failed after recovery,
stopped by the user, or rejected at the plan stage. The report has two
main parts, in this order:

1. **Decided without you:** every decision that the user did not make.
2. **Goal and what was done:** the goal of the work and the result.

Keep all other content short. Write the report in ASD-STE100 Simplified
Technical English ("Output Language" in `../SKILL.md`).

## Gather

```
AP report --flow <flow>
```

returns every session of the flow (plan, runs, recovery runs) with status,
duration, attempts, restarts, errors, `task_log`, `router_fallbacks` and
`models_used`; the full decision log in time order; `without_user`, every
decision that the user did not make (by `auto`, `agent`, `autopilot`,
`agrouter` or `ralphex`; user answers and notes are not in it);
`timeouts`, the subset taken on a timeout; and, on the run's branch, the
commits and a diffstat against the default branch. Also read the final
plan file (it may have moved to `<plans_dir>/completed/`).

For "what was done", summarize the branch changes with code-explainer's
`summarize` on the branch (`branch` or `changes` target) when it is
available; otherwise read the diff of the main files and summarize them
directly. Give the full walkthrough (`explain-summarize`) only when the
user asks for it.

## Layout

```
# Autopilot report: <task in a few words>

**Result:** done | partly done | failed | stopped · <branch> · <N> commits · <duration>
**Decided without you:** <N> decisions. Review them below.

## 1. Decided without you
| When | Decision | Made by | Why | How to change it |
Every entry of `without_user`, none skipped, oldest first. "Made by":
"autopilot, no reply in <N> min" for `auto` (from `waited_min`), "agent"
for the agent's own choices and fixes, "autopilot" for supervisor
choices, "model router" for `agrouter` fallbacks, "plan runner" for
`ralphex` limit waits. "Why" is the reason from the entry. When
`recommended` is false, say so ("no recommendation was ready: took the
first option") and mark the row "⚠ review". "How to change it" is one
short action: revise the plan, run the task again, revert a commit.
When the list is empty, write "None. You made all decisions."

## 2. Goal and what was done
**Goal:** one or two sentences: the task as the user gave it, and the
plan file path.

**What was done:** the `summarize` bullets for the branch changes, with
links to the code. Then the plan tasks as a table:
| Task | Status ✓/✗/– | Model | Minutes | Commit |
Take the last three columns from each run session's `task_log`.

## 3. Open items
Only items that need the user: failed or skipped tasks, problems that
are not solved, a missing key, tests that do not pass, sessions that are
still alive. Then one to three next steps (for example: review the
branch and open a PR). When nothing is open, write "None." and give the
one next step.
```

Do not add other sections. Problems that autopilot solved are already
rows in "Decided without you". Routine restarts need only a count in the
**Result** line when there are any. Give the executor and models used
only in the task table.

## Delivery

- Show the report in the chat.
- Save it as `report.md` in the run session directory (or the plan session
  when no run happened) and give the path. Offer once to copy it into the
  repository (for example next to the plan in `completed/`); do not commit
  it unasked.
