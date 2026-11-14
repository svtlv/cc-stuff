---
name: autopilot-setup
description: This skill should be used when the user runs "/autopilot-setup" or "$autopilot-setup", or asks to "set up autopilot", "install the plan runner", "install or upgrade ralphex.windows", "install agrouter", "configure model routing for this repo", "set the classifier API key", "choose models and effort for autopilot", "configure autopilot for this repository", "reconfigure autopilot", "change the models for this repo", "switch this repo to the Codex executor" or "update the routing docs". It guides the user through installing or upgrading the plan runner and model router, storing the classifier key in the global user config, and writing a repository-specific config with the chosen models, efforts or routing.
argument-hint: "[tools | key | repo | reconfigure] (default: all, or reconfigure when the repository is already set up)"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion
metadata:
  version: "0.1.0"
---

# Autopilot Setup

Guide the user through a complete setup of the autonomous plan runner
(ralphex.windows) and the model router (agrouter): install or upgrade both,
store the classifier key globally, and configure the current repository.

```
 1 check ─► 2 install/upgrade ─► 3 classifier + key ─► 4 repo config ─► (4b response times) ─► 5 verify
            (runner, router)      (global, user-only)   (models/routing)
```
**Note:** each stage is skipped when `doctor` shows it is already done and
the user does not want to change it. An argument (`tools`, `key`, `repo`)
runs only that stage plus the final check.

## Paths

Resolve from the directory holding this `SKILL.md`:

| Item | Path |
|---|---|
| Driver | `../autopilot/scripts/autopilot.py` (`AP` below = `python3 <path>`, `py -3` on Windows) |
| Install commands | `references/install.md` |
| Config recipes | `references/config-recipes.md` |
| Reconfiguring | `references/reconfigure.md` |
| Codex wrapper templates (installed by `AP install-wrapper`) | `assets/codex-agrouter.sh`, `assets/codex-agrouter.cmd`, `assets/codex-agrouter.py` |
| Windows scripts and PowerShell commands | `../autopilot/references/windows.md` |

Read `install.md` before Stage 2 and `config-recipes.md` before Stages 3–4.

## Interaction Rules

- Ask one decision at a time. Use the host's question tool when it has one
  (AskUserQuestion in Claude Code; recommended option first, marked
  "(Recommended)"); otherwise ask in plain text with numbered options.
- Show every install command and get a yes before running it.
- Never print a secret. Use `AP config-show` (redacts) to display configs;
  never `cat` them.
- Explain each stage in one or two sentences before acting.

## Stage 1 — Check

Run `AP doctor --json --latest` from the repository root. Show a status table:

| Item | Installed | Latest | Action |
|---|---|---|---|
| Plan runner | `cli.version` | `latest.cli` | install / upgrade / ok |
| Model router | `router.version` | `latest.router` | install / upgrade / ok |
| claude / codex CLIs | `reviews.claude`, `reviews.codex` | — | usable / missing / logged out / disabled |
| Classifier key | `router_config.key_set` (+ provider) | — | set / missing |
| Repo config | `cli_config.local_exists`, `uses_router` | — | configured / missing |
| Routing docs in sync | `cli_config.doc_drift` | — | ok / resync `claude_args` |

When `latest` is null (offline), say so and continue with installed versions.

**Already configured?** When the repository has a `.ralphex/config`
(`cli_config.local_exists`) and the argument is empty or `reconfigure`,
switch to **reconfigure mode** (next section) instead of running Stages 2–4
in full. Tools and key problems from the table above are offered as
properties there.

## Reconfigure Mode

Follow `references/reconfigure.md`: run `AP config-summary`, show the
current setup as one numbered table (value and where it comes from: this
repository, the user's global config, or built in), offer the summary's
issues first, ask which properties to change, change only those, show the
diff, verify with `doctor`, and offer to commit. Every property not picked
stays exactly as it is.

Stages 2, 3 and 4b serve as the property steps for tools, classifier and
key, and response times. A full reconfiguration from scratch is still
available as `repo` (Stage 4 asks to keep or replace each existing value).

## Stage 2 — Install or Upgrade

Follow `install.md` for the platform (`doctor.os`). Prefer Homebrew on macOS
when `brew` exists, the installer script on Linux, the PowerShell installer
on Windows, and `go install` as the fallback. The router needs Go 1.27+;
check `go version` first and offer to install Go when missing.

After installing, rerun `doctor` and confirm the versions. When the command
is still not found, fix `PATH` as `install.md` describes.

When `claude` or `codex` is unusable, explain `reviews.<cli>.reason` and
offer installation or login (`claude auth login`, `codex login`), or
continue with the usable CLI and skip the unavailable model's reviews.
Offer to disable a missing or unwanted CLI for routing in Stage 4.

## Stage 3 — Classifier and Key (global)

1. Ask which classifier to use: **TypeSafe (Recommended, default)** or
   **OpenAI**. When a key is already set for that provider, ask whether to
   keep it or replace it, and skip ahead on keep.
2. Ask for the API key. Offer three ways:
   - paste it here: store it right away with `--value-stdin --private`;
   - set `AGROUTER_CLASSIFIER_API_KEY` in the shell profile or secret
     manager (show the line; nothing is stored);
   - store it later (skip; routing will not work until then).
3. Write the global config per `config-recipes.md` ("Classifier and Key").
   For OpenAI also set `[agrouter] classifier_provider = openai`. Never write
   the key into `.agrouter/config` or any file inside a repository.
4. Do not echo the key back. Confirm with `AP doctor --json` that
   `router_config.key_set` is true.

## Stage 4 — Repository Config

Skip with a note when not inside a git repository. The target is the
"Reference Layout" in `config-recipes.md`. When `.ralphex/config` already
exists, prefer reconfigure mode; in a full `repo` pass, offer each current
value (from `AP config-summary`) as the "keep" option. Ask, in order:

1. **Executor** — picks the recipe ("Reference Layout" in
   `config-recipes.md`):
   - **Claude setup**: Claude Code plans and executes through agrouter,
     Codex does the external review (Recommended when both are usable);
   - **Codex setup**: Codex plans and executes every phase through the
     wrapper and agrouter, no external review (Recommended when only Codex
     is usable, or the user prefers Codex). Skip Claude plan review when
     Claude is unavailable; regular Codex code reviews still run.
   Offer only usable CLIs, based on `reviews.claude` and `reviews.codex`.
   When neither is usable, offer login or installation before continuing.
   An existing `executor` line is the current
   choice. The runner plans and executes through these two protocols whatever agent
   runs this skill ("The Work Itself" in `../autopilot/references/hosts.md`).
   For the Codex setup, run the Codex checks from the recipe now (version,
   `codex login status`, the `~/.codex/AGENTS.md` link offer).
2. **Model selection** ("Variant: Pinned or Fixed Models"):
   - Full routing — the classifier picks model and effort per call
     (Recommended);
   - Pin the model, route the effort;
   - Fixed models and efforts for each phase;
   - No router — run the CLI directly with fixed models.
3. For pinned or fixed modes, run `AP catalog` and offer only the
   executor's models (`claude` or `codex` entries) for each phase (plan,
   task, review) and, for fixed mode, the effort from that model's catalog
   `efforts` (Codex models may list `max`/`ultra`; a phase spec may use
   `ultra`, but `max` only works as `codex_reasoning_effort`).
   Offer sensible pairs first: for Claude `opus:high` / `opus:medium` /
   `sonnet:medium`; for Codex the strongest catalog model at `high` for
   planning and the everyday one at `medium` for tasks and reviews.
4. **Inherited globals**: when `doctor` shows a model key pinned by the
   global runner config (often `plan_model`), ask whether this repository
   inherits it or follows the chosen mode.
5. **Project docs for routing** ("Choosing Docs"): list the agent
   instructions, `README.md` and architecture/design folders that exist;
   ask which to pass (multi-select). Folders stay folders: the wrapper
   globs them.
6. **Reviews by the other model** — read `reviews` from `AP doctor --json`
   first and say what is possible for both CLIs (installed, logged in,
   disabled, or unavailable with the reason) per
   `../autopilot/references/reviews.md`:
   - **External code review** (Claude setup only; the Codex setup skips
     it): Codex through the router (Recommended when `codex.usable`) or
     none. When Codex is configured but not usable, explain that runs skip
     it automatically, and offer to fix it (install, `codex login`) or set
     `none`.
   - **Plan review**: the other model (`plan_review.cross_model`) reviews
     each plan draft read-only. Recommend `auto` when that model is usable,
     otherwise `off`. Write it with
     `AP config-set .autopilot/config plan --section review --value auto`.
     Honor a request to skip either model's review. An existing enabled
     review whose CLI is unavailable is skipped without blocking the run;
     offer to fix that CLI or set plan review to `off`.
7. **Wait on limits**: wait 15 minutes and retry (Recommended) or stop.

Then write the files, explaining each change with `--comment`:

1. `AP config-init` — `.ralphex/config` from the runner's commented
   template (never the runner's `--init`, which also copies `agents/` and
   `prompts/`).
2. Set the keys of the matching recipe in `config-recipes.md` with
   `AP config-set .ralphex/config <key> --value <v> --comment "<why>"`.
   List folder docs file by file in `claude_args`. Use `:` for routed specs,
   empty values for routed Codex keys. Always write all three phase keys
   locally, in both setups.
3. When Codex runs through the router (the Codex setup, or the Claude
   setup's external review):
   `AP install-wrapper --docs <files> --doc-dirs <folders>` (adds the
   Windows pair on Windows), then set `codex_command` to the returned path.
4. When the chosen docs exceed about 60 KB in total (sum file sizes),
   offer the long-context limits for this repository ("Large Doc Sets") and
   write them to `.agrouter/config`. Write `[cli.<name>] enabled = false`
   there too when a CLI is missing or unwanted. Never a key.
5. Add `.ralphex/progress/` to `.gitignore` when missing.
6. Show the result with `AP config-show` for each written file, then run
   `AP doctor --json`: `cli_config.executor` must match the choice,
   `cli_config.spec_problems` must be empty and `reviews.execution.usable`
   must be true (it checks login, `codex_command` and the sandbox); fix any
   listed key.

Finally recommend committing the files ("Committing"), and explain why: an
uncommitted config blocks runs on the default branch. Keeping them local is
the alternative.

## Stage 4b — Response Times (optional, global)

Show `AP policy` as a short table (decision, wait, reminder) and ask:
keep the defaults (Recommended), or change some. For changes, write each
key to the user's config, which applies to every repository:

```
AP config-set ~/.config/autopilot/config decide --section timeouts --value 2h
```

Values: `90s`, `10m`, `1h`, `never`; `<key>.remind` sets the reminder
(details in `../autopilot/references/response-times.md`).

## Stage 5 — Verify

1. `AP doctor --json`: runner and router found, key set, `uses_router`
   matches the chosen mode.
2. With the user's consent (one paid classifier request), run the routing
   check from `install.md` and show the chosen CLI, model and effort.
3. Summarize what was configured in one table (item, value, file) and point
   to the next step in the host's form (see "Naming the Commands" in
   `../autopilot/references/hosts.md`): the full run to plan and build, or
   plan only.

## Parallel Work

When the host supports subagents, the release lookups and the install of
both tools are independent: one subagent per tool may check versions and
prepare commands while the user answers the classifier questions. Run the
installs themselves only after the user approves them. Without subagents, do
the stages in order.
