# Preflight: Setup Gate

The `autopilot`, `autopilot-plan` and `autopilot-run` skills run this gate
before anything else: before asking for a task description, before
starting a session, before reading the plan.

```
doctor ─► missing_tools? ──yes──► ask: run setup? ──yes──► autopilot-setup ─► doctor again ─► continue
             │                         └──no───► stop (nothing started)
             no
             ▼
       setup_incomplete? ──yes──► offer setup, or continue
             │
             no ─► continue
```
**Note:** missing tools always stop the flow until setup runs. An incomplete
configuration only gets an offer, because runs can still work with fixed
models.

## Steps

1. `cd` to the repository root and run `AP doctor --json`.
2. **`missing_tools` is not empty** (ralphex.windows or agrouter is not
   installed). List what is missing, in one line each, then ask one question:

   | Option | Effect |
   |---|---|
   | Run setup now (Recommended) | Run the setup skill, then continue the original request. |
   | Stop | End without starting anything. |

   Do not offer to continue without the tools: the runner cannot start, and
   a config that calls the router fails on the first task.
3. **`setup_incomplete` is not empty** (no classifier key, a missing
   repository config: `.ralphex/config`, `.agrouter/config` or
   `.autopilot/config`, or a runner older than v1.7.0-windows.4, which has
   no plan review: `cli.plan_review: false`). Mention each item in one line and ask: run setup now
   (Recommended), or continue with the current settings. Without a reply
   within the `preflight` response time (10 min, `response-times.md`),
   continue with the current settings and log it; setup needs
   the user and never runs unattended. Missing tools (step 2) have no
   timeout: nothing can run without them.
4. **`cli_config.doc_drift` is set**: the `--doc` list in `claude_args`
   names files that no longer exist (`missing`) or misses docs the Codex
   wrapper picks up from its folders (`unlisted`), so Claude and Codex calls
   route on different docs. Name the files in one line and offer to fix
   `claude_args` now (`config-set`, keeping everything after ` -- `
   unchanged), or continue.
5. Otherwise continue silently.

## Running Setup From Another Skill

Read `../autopilot-setup/SKILL.md` (relative to the `autopilot` skill
directory) in full and follow it, or load it by name where the host can
(see "Naming the Commands" in `hosts.md`). Pass the stage that matters when only part is missing:

| Missing | Setup argument |
|---|---|
| Any tool | none (all stages) |
| Only the key | `key` |
| Only a repository config (any of the three files) | `reconfigure` when `.ralphex/config` exists (it offers the missing files first), else `repo` |

After setup finishes, run `AP doctor --json` again. When `missing_tools`
is now empty, continue the original request with its original arguments
and say so in one line ("Setup done. Back to planning: add caching.").
When something is still missing, show what and stop.
