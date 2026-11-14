# Changing a Finished Plan

Once a plan file exists, the user can ask for changes at any checkpoint
before the run starts, as many times as they like. Each round: snapshot,
change, show the diff, check, ask again.

## Where the User Can Ask

| Moment | How it is offered |
|---|---|
| End of `autopilot-plan` (plan only) | "Plan ready. Any changes?" (`plan_review`, 30 min; no reply = no changes) |
| `autopilot` decide step | The **Revise the plan** option of implement-or-stop (`decide`, 60 min) |
| `autopilot-run` before the start | "Change the plan first" option of the start question (`run_start`) |
| Any time the user says so | Free text such as "drop task 3", "use Redis instead" |
| During a run | Only after stopping it (below) |

## One Revision Round

```
AP plan-rev snapshot <plan>
   │
   ▼
small change? ──yes──► edit the file directly
   │no
   ▼
re-plan: autopilot-plan --redesign <plan> + the change request ──► <new> ──► AP plan-rev supersede <plan> --by <new>
   │                                                                              (plan := <new>)
   ▼
AP plan-rev diff <plan>  ─►  show the change  ─►  ask: more changes? (same response time as the question that led here)
```
**Note:** the snapshot is taken before every round, so the diff always shows
exactly what this round changed. After a re-plan, `supersede` removes the old
file and stores its last content as the new file's snapshot, so the diff
runs on the **new** path and compares old against new. From then on the new
file is the plan for the next rounds.

## Choosing the Way

| Change request | Way | Why |
|---|---|---|
| Wording, order of tasks, add/remove/merge/split steps or tasks, a test or validation command, a file name | **Edit directly** | Fast, keeps everything else, keeps checked boxes |
| A different approach or technology, a new or removed feature, scope larger than one task, "rethink" | **Re-plan** | The planner explores the code again and can ask its own questions |
| The plan is `in progress` (some boxes checked) | **Edit directly**, even for bigger changes | A re-plan starts over and loses the progress record |

When unsure, say which way and why, and let the user switch.

Direct edits keep the runner's format: `### Task N:` headings with the
English keyword, checkboxes only inside tasks, a test step in every new
task, renumbering tasks in order. Never uncheck a checked box unless the
user asks to redo that work.

The re-plan description:

```
Revise the plan <path>. Keep everything not mentioned below. Requested
changes: <the user's request, verbatim>. Keep "### Task N:" sections with
checkboxes, a test step in every task, and the validation commands.
```

## Showing the Change

From `plan-rev diff`:

```
Revision 2 · +9 −3 lines · tasks 5 → 6 · check: ready
  + Task 4: Add Redis cache (new)
  ~ Task 2: test step now runs the integration suite
  − Task 5 step "write migration" (removed)
```

Summarize the diff as task-level changes (as above), and give the full
unified diff when the user asks or when it is short. Warn when
`checked_boxes_lost` is above zero, and fix the plan if the check verdict
is not `ready`.

Log each round on the flow's plan session (or the run session):

```
AP log <session> --kind decision --text "Plan revision 2: added Redis cache task" --reason "<user's request>"
```

## Coming Back

| Where the round started | Continue with |
|---|---|
| End of `autopilot-plan` | Ask for more changes; on "no" (or no reply), hand over |
| `autopilot` decide | A short summary of what changed (not the full explanation again, unless the user asks or the plan changed a lot), then the decide question with a fresh timer |
| `autopilot-run` start | The new digest, then the start question with a fresh timer |

## Changing a Plan During a Run

The runner reads the plan file while it works, so never edit it under a
running session. When the user asks for a change mid-run:

1. Say what the change means for the run (which tasks are done, which
   are left) and ask: stop now and revise, or revise after the current task.
2. After the current task: wait for the next `section` event of a new task
   iteration, then stop.
3. `AP stop <session>`, revise with a direct edit (the plan is in progress),
   show the diff, then start the run again with the same `--flow`: it
   resumes at the first unchecked box.
