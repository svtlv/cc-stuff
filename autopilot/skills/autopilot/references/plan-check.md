# Checking a Plan Before a Run

Every run starts with `AP check-plan <file>`. It applies the runner's own
parser rules, so a plan that passes is one the runner can execute task by
task.

```
plan file ─► check-plan ─┬─ ready ────────────► run
                         ├─ warnings ─────────► show them ─► run, or redesign (user's choice)
                         ├─ redesign ─────────► ask ─► autopilot-plan --redesign <file> ─► new plan ─► check again ─► run
                         └─ status: complete ─► nothing to do: review only (-- --review), or stop
```
**Note:** a redesign never edits the original file. The planner reads it
and writes a new plan into the plans directory, and the run uses the new
plan.

## What It Checks

| Rule (the runner's parser) | Result when broken |
|---|---|
| Tasks are `### Task N:` or `### Iteration N:` with the English keyword; N may be `2.5` or `2a` | **redesign**: no task found, or task-like headings in another format (`## Task 1`, `### Step 1`, `### Задача 1`) |
| Every task has `- [ ]` / `- [x]` checkboxes; a `##` heading ends a task | **redesign**: tasks without checkboxes never finish |
| A document with no tasks at all (design doc, notes, spec) | **redesign** |
| Unchecked checkboxes only inside tasks | warning: boxes in Overview or Success criteria cause extra loop iterations |
| `## Validation Commands`, or test steps in the tasks | warning: nothing checks the work between tasks |
| Tasks of at most ~12 steps, plans of at most ~20 tasks | warning: split |
| Unique task numbers | warning |

Checkboxes inside fenced code blocks are ignored, as the runner does.
`status` is `not started`, `in progress` (the run resumes at the first
unchecked box) or `complete`.

## Acting on the Verdict

| Verdict | Action |
|---|---|
| `ready` | Continue to the run. Mention `in progress` plans resume where they stopped. |
| `warnings` | List them. Recommend **run as is** when they are minor (stray boxes, missing validation section on a small plan); recommend **redesign** for missing tests on a large plan or oversized tasks. `plan_check` response time. |
| `redesign` | Explain in one or two lines why the runner cannot use it, then ask: **Redesign with autopilot-plan** (recommended, the default after the `plan_check` time, 15 min) or Stop. Never run a `redesign` plan. |
| `status: complete` | Offer review only (`-- --review`) or stop; no default run. |

Log the outcome: `AP log <session> --kind decision --text "Redesigned <file>:
<reason>"` once a session exists (the redesign's plan session).

## Redesigning

Follow `../autopilot-plan/SKILL.md` with the redesign description below and
the same `--flow`. All its rules apply: questions and draft review with
their response times.

```
Turn the existing document <path> into an implementation plan for this
repository. Read it first and keep its goals, scope, decisions and
constraints; do not add features it does not ask for. Restructure it into
"### Task N:" sections with checkboxes, a test step in every task, and a
"## Validation Commands" section with this project's test and lint
commands. Problems found by the check: <problems and warnings>.
```

When planning ends, run `check-plan` on the new file. It should be
`ready`; if not, show what is still wrong and offer one more redesign or
running with warnings. Then continue where the redesign was asked:

| Asked from | Continue with |
|---|---|
| `autopilot` (full run) | Explain the new plan and the implement-or-stop decision (Steps 3–4), then the run |
| `autopilot-run` | Show the new plan's digest, then the start question (`run_start`) |

Mention in the final report that the plan was redesigned from the original
file, and keep the original untouched.
