# Config Recipes

## Reference Layout

Two setups, chosen by the executor (setup Stage 4, question 1):

| Setup | Planning and tasks go through | External review | Recipe |
|---|---|---|---|
| Claude | Claude Code via `claude_command = agrouter` | Codex via the wrapper, read-only | "Recipe: Claude Executor" |
| Codex | Codex via the wrapper and agrouter | none (skipped by the runner) | "Recipe: Codex Executor" |

In both setups every runner call goes through agrouter: plan creation
(`plan_model`), tasks (`task_model`), reviews and finalize (`review_model`)
and, in the Claude setup, the external Codex review. So the routing docs
and the phase specs shape planning as much as the run.

Both share the same files:

On Windows, replace each `.sh` wrapper path below with the `.cmd` path
returned by `AP install-wrapper`, and commit its companion `.py` file too.
Use the PowerShell commands in `../../autopilot/references/windows.md`.

```
repo/
├── .ralphex/
│   ├── config                    full commented template, routed keys changed, committed
│   ├── .gitignore                written by ralphex (progress/, worktrees/)
│   └── scripts/codex-agrouter.sh Codex through the router, committed, executable
├── .autopilot/
│   └── config                    full commented template: [timeouts] only, committed
├── .agrouter/
│   ├── config                    full commented template: provider, quota prefix, CLIs, limits; no key, committed
│   └── .gitignore                written by agrouter (logs/)
```
**Note:** everything in the repository is shareable and complete. A user
with no global config gets the same runs after a clone. The classifier key
lives only in the user's global router config or the environment.

| File | Scope | Holds |
|---|---|---|
| `~/.config/agrouter/config` (`AGROUTER_CONFIG_DIR`) | user | **API key** (and the provider, for repositories without their own config) |
| `.agrouter/config` | repository | classifier provider, Codex quota prefix, disabled CLIs, limits for this project's docs. **Never a key.** |
| `~/.config/autopilot/config` (`AUTOPILOT_CONFIG`) | user | personal response times |
| `.autopilot/config` | repository | response times for this project (the only autopilot setting) |
| `~/.config/ralphex/config` (`RALPHEX_CONFIG_DIR`) | user | personal defaults (often `plan_model`), notifications |
| `.ralphex/config` | repository | command, executor, models, review tool, limit waiting |
| `.ralphex/scripts/codex-agrouter.sh` (`.cmd` + `.py` on Windows) | repository | Codex wrapper |

Layering: router = embedded < global < local < environment; runner =
embedded < global < local < flags; autopilot = built in < global < local <
`AUTOPILOT_TIMEOUTS`. A key missing from the local file keeps
its global value: `doctor` shows the effective values. When the global
config pins a key the recipe routes (for example `plan_model = opus:high`),
show it and ask whether this repository inherits it or overrides it.

## Driver Commands for Config

```
AP config-init --tool ralphex                    # .ralphex/config from the runner's template
AP config-init --tool agrouter                   # .agrouter/config from the router's template
AP config-init --tool autopilot                  # .autopilot/config with the response times
AP config-init --tool agrouter --merge           # upgrade a hand-written file, keep its values
AP config-set .ralphex/config <key> --value <v> --comment "<why>"
AP config-set .agrouter/config <key> --section <name> --value <v> --comment "<why>"
AP install-wrapper --docs AGENTS.md README.md --doc-dirs docs/architecture
AP config-show <file>                            # check, secrets redacted
```

`config-init` writes each tool's full template with every value commented
out, so the file documents every key and overrides only the keys setup
sets. For the runner it copies only `config` (its `--init` would also copy
the whole `agents/` and `prompts/` trees). For the router it runs
`agrouter` once in an empty temporary config directory with no key: the
router writes its commented embedded defaults there and sends no classifier
request. The user's global router config is not read or changed.

`doctor` reports `cli_config.doc_drift` when `claude_args` lists a doc that
no longer exists, or misses one the wrapper globs from its folders. Fix it by
rewriting the `--doc` part of `claude_args`. `config-set`
uncomments the template line and puts `--comment` lines right above the
key. Comments must sit on their own lines in both tools: an inline
`# ...` after a value becomes part of the value.

## Recipe: Claude Executor, Routed, With Codex Review

Every Claude call (plan creation, tasks, reviews, finalize) and the
external Codex review go through the router.

| Key | Value | Comment to write |
|---|---|---|
| `claude_command` | `agrouter` | |
| `claude_args` | `exec claude --doc=<doc> ... -- --dangerously-skip-permissions --output-format stream-json --verbose` | routed through agrouter: options before "--" are agrouter's, the rest go to claude. --doc feeds agrouter's project-complexity stage only (never passed to claude); keep this list in sync when a doc is added or renamed. |
| `task_model` | `:` | |
| `review_model` | `:` | |
| `plan_model` | `:` or inherit global | ask (see Layering) |
| `codex_command` | `.ralphex/scripts/codex-agrouter.sh` | ralphex runs this as a single executable (no word splitting), so agrouter is wrapped in a script that adds the same --doc set and forwards codex's options after "--". |
| `codex_model` | *(empty)* | |
| `codex_reasoning_effort` | *(empty)* | |
| `codex_sandbox` | `read-only` | |
| `external_review_tool` | `codex` | |
| `codex_limit_patterns` | default list with `You've hit your usage limit` replaced by `hit your usage limit` | |
| `wait_on_limit` | `15m` | |
| `use_worktree` | leave unset (`false`) | autopilot picks a new worktree or the current checkout for each run (`AP place`); `true` breaks runs started inside a worktree |

`claude_args` keeps everything after `--` exactly as shown: those are the
native Claude flags the runner needs. `claude_args` cannot glob, so list
every doc explicitly; the wrapper globs its `--doc-dirs` itself.

Empty `codex_model`/`codex_reasoning_effort` are required for routing: the
runner's own defaults would otherwise pin both.

`codex_limit_patterns` uses the shorter `hit your usage limit` so the
router's Codex quota line counts as a limit (wait and retry), not an error.

## Recipe: Codex Executor, Routed

The runner executes plan creation, tasks, reviews and finalize through
Codex, routed by agrouter. Taken from a project already running this way.

```
ralphex ─ executor = codex ─► codex_command (.ralphex/scripts/codex-agrouter.sh)
                                   └─► agrouter exec codex --doc=... -- <codex options>
phases: plan ─► task iteration N ─► review N ─► finalize      (no external review)
```
**Note:** with the Codex executor every phase runs through the wrapper, so
the wrapper's docs drive all routing. The Claude keys stay configured so
the repository can switch back by changing one line.

| Key | Value | Comment to write |
|---|---|---|
| `executor` | `codex` | |
| `pass_claude_md` | `true` | let codex read the project CLAUDE.md |
| `codex_command` | `.ralphex/scripts/codex-agrouter.sh` | ralphex expects one executable, so the wrapper adds agrouter's --doc options, drops ralphex's leading exec, and forwards the remaining codex options after --. |
| `codex_model` | *(empty)* | keep empty so ralphex emits no -c model override; agrouter selects the model. |
| `codex_reasoning_effort` | *(empty)* | keep empty so ralphex emits no -c effort override; agrouter selects the effort. |
| `plan_model`, `task_model`, `review_model` | `:` each | ":" clears inherited phase selections; agrouter chooses model and effort per call. |
| `codex_sandbox` | `danger-full-access` | implementation tasks need write access (read-only is only for an external review). |
| `codex_timeout_ms` | `3600000` | |
| `claude_command`, `claude_args` | as in the Claude recipe | kept so switching `executor` back needs no other change |
| `codex_limit_patterns` | default list with `hit your usage limit` | |
| `wait_on_limit` | `15m` | |

The phase keys are the trap: under the Codex executor they become `-c
model=`/`-c model_reasoning_effort=` overrides. A global
`plan_model = opus:high` would ask Codex for a model named "opus". Always
write all three locally; `doctor` reports `cli_config.spec_problems` when
one names the other executor's model, uses a phase-level `max` (the runner
drops `:max` in a phase spec for Codex and keeps `codex_reasoning_effort`),
or names an effort the router catalog does not list for that model. Codex
itself accepts `max` and `ultra` where the model supports them: write them
as `codex_reasoning_effort = max`, or `:ultra` in a phase spec.

`doctor` also reports the executor as unusable when `codex_command` is
missing or not executable, or `codex_sandbox = read-only` is set in any
runner config (an explicit value wins over the Codex executor's
`danger-full-access` default).

Codex specifics to check during setup:

| Check | How |
|---|---|
| Codex CLI new enough | runner needs ≥ 0.130.0 (`doctor` flags older); the router's quota probe needs ≥ 0.162.1 |
| Logged in | `codex login status` |
| User-level instructions | the runner prints a hint when `~/.claude/CLAUDE.md` exists but `~/.codex/AGENTS.md` does not; offer `ln -s ~/.claude/CLAUDE.md ~/.codex/AGENTS.md` on Unix, or a PowerShell link/copy per `windows.md` on Windows |
| Quota line prefix | router `[cli.codex.availability.limits] error_prefix = "ERROR: agrouter: "` (the Codex executor only reads severity-prefixed lines) |

## Variant: Pinned or Fixed Models

The runner appends model flags from `model[:effort]`; the router routes only
what is left unset.

Specs must name the executor's own models: Claude names (`opus`,
`sonnet`, `claude-...`) for the Claude executor, Codex names
(`gpt-6.1-sol:high`) for the Codex executor, from `AP catalog` filtered to
that CLI. For Codex, efforts are `low`, `medium`, `high`, `xhigh`.

| Mode | Spec example | Classifier asked |
|---|---|---|
| Full routing | `:` | model and effort |
| Pinned model | `opus:` | effort |
| Pinned effort | `:high` | model |
| Fixed | `opus:high` | nothing (availability and quota still checked) |
| No router | `claude_command = claude`, default `claude_args` | nothing |

Note: an empty `review_model` falls back to `task_model`; `:` routes it.
Names come from `AP catalog`. For Codex, specs resolve against
`codex_model`/`codex_reasoning_effort`.

## Choosing Docs

Docs describe the project to the classifier's complexity stage; the model
never sees them. Offer what exists:

| Kind | Typical files |
|---|---|
| Agent instructions | `AGENTS.md`, `CLAUDE.md` (pick one when one imports the other) |
| Overview | `README.md` |
| Architecture or design folder | `docs/architecture/`, `docs/design/` (pass as `--doc-dirs` to the wrapper, and list its files in `claude_args`) |

## Recipe: Repository Router Config

Both setups write these keys into `.agrouter/config`, on top of the
template:

| Section | Key | Value | Why |
|---|---|---|---|
| `[agrouter]` | `classifier_provider` | `typesafe` or `openai` (Stage 3) | every user of the repository routes with the same classifier; each keeps the key for it in the global config or `AGROUTER_CLASSIFIER_API_KEY` |
| `[cli.codex.availability.limits]` | `error_prefix` | `"ERROR: agrouter: "` | when Codex is routed: the runner then reads a Codex quota block as a limit and waits; an old global config may hold `"agrouter: "` |
| `[cli.<name>]` | `enabled` | `false` | only for a CLI that is missing or unwanted for this repository |
| `[agrouter]`, `[classifier.typesafe]` | "Large Doc Sets" keys | see below | only for large doc sets |

When the user's global router config has other active non-secret values
(for example a longer `timeout` or a changed `routing_policy`), show them
and ask whether the repository takes them too. Never copy `api_key`.

## Large Doc Sets: Local Router Limits

Sum the chosen docs' sizes. One TypeSafe request holds about
`request_tokens × bytes_per_token` bytes (30000 × 3 ≈ 90 KB by default,
including the questions). Above roughly 60 KB, offer long-context routing
for this repository in `.agrouter/config` (TypeSafe only):

| Section | Key | Value | Why |
|---|---|---|---|
| `[agrouter]` | `timeout` | `120s` | long inputs take longer to route |
| `[classifier.typesafe]` | `long_context` | `true` | raises the provider limit to 260000 tokens |
| `[classifier.typesafe]` | `request_tokens` | `128000` | allowance per routing request |
| `[classifier.typesafe]` | `bytes_per_token` | `2.5` | code and docs pack more tokens per byte |

Write only these keys in addition to the recipe above; everything else
comes from the embedded defaults. Never add `api_key` here.

## Classifier and Key (global router config)

| Choice | Keys |
|---|---|
| TypeSafe (default) | `[classifier.typesafe] api_key` |
| OpenAI | `[agrouter] classifier_provider = openai`, `[classifier.openai] api_key` |

```
printf '%s\n' '<key>' | AP config-set ~/.config/agrouter/config api_key \
  --section classifier.typesafe --value-stdin --private
```

`AGROUTER_CLASSIFIER_API_KEY` overrides the config key, for users who keep
secrets in a manager.

The `printf` pipeline above is for Unix shells. Use the key-input example
in `../../autopilot/references/windows.md` for PowerShell.

## Quota Lines and Timeouts

Keep `wait_on_limit = 15m` in the recipe: the runner then retries the same
step in place. Without it autopilot adds `--wait=900s` to every run, but
setting it keeps plain `ralphex` runs safe too. Details in the autopilot
skill's `references/limits.md`.

The router's installed defaults print Codex quota blocks with
`error_prefix = "ERROR: agrouter: "`, which the Codex executor needs to
wait instead of stop. A global router config created before that default
may still hold `"agrouter: "` under `[cli.codex.availability.limits]`.
The repository router config sets the prefix ("Recipe: Repository Router
Config"), so such a global value does not change runs in this repository.

The router prints nothing while routing (up to its `timeout`). A runner
`idle_timeout` must exceed that plus the model's time to first output;
leave it unset or at several minutes, especially with a 120 s timeout.

## Committing

On the default branch the runner refuses to start when files other than
the plan are uncommitted, and new config files count. Teammates and CI
also need the files. Recommend committing:

```
git add .ralphex/config .ralphex/.gitignore .ralphex/scripts .agrouter/config .agrouter/.gitignore .autopilot/config
git commit -m "Add ralphex, agrouter and autopilot configs"
```

The `.gitignore` files inside `.ralphex/` and `.agrouter/` come from the
tools. The alternative is keeping the files local by listing them in
`.git/info/exclude`.
