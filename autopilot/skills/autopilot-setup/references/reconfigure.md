# Reconfiguring a Configured Repository

When `.ralphex/config` exists, setup changes only what the user picks and
leaves every other property as it is.

```
AP config-summary ─► show properties ─► user picks numbers ─► change each ─► diff ─► verify ─► commit?
```
**Note:** each property maps to a fixed set of keys or commands (table
below), so a change never rewrites unrelated settings. Issues found by the
summary are offered first.

## 1. Show the Current Setup

Run `AP config-summary` and present one numbered table. Show where each
value comes from: `local` (this repository), `global` (the user's runner
config, applies to every repository), `default` (built in).

| # | Property | Now | From |
|---|---|---|---|
| 1 | Executor | Claude setup (external Codex review) | default |
| 2 | Phase models | plan `opus:high` fixed · task `:` routed · review `:` routed | global · local · local |
| 3 | Codex model / effort | routed / routed | local |
| 4 | Routing docs | 12 in `claude_args`; wrapper: AGENTS.md, README.md + docs/architecture/ (hand-written) | local |
| 5 | Router | Claude ✓ · Codex ✓ | local |
| 6 | External code review | codex, read-only sandbox · Codex usable ✓ | local |
| 7 | Limits and timeouts | wait 15m · idle off · session off | local |
| 8 | Large-doc routing | long context, 128000 tokens, 2.5 bytes/token, 120s | `.agrouter/config` |
| 9 | CLIs the router may pick | all enabled | |
| 10 | Classifier and key | typesafe, key set | global |
| 11 | Response times | defaults | global |
| 12 | Tool versions | installed vs latest (`doctor --latest`) | |
| 13 | Plan review | auto · recommends external (codex, usable) | `.autopilot/config` |
| 14 | Publish | pr · `gh` logged in | `.autopilot/config` |

List `issues` above the table with the recommended fix (for example doc
drift → property 4). For a missing repository config ("this repository
has no router config" or "no autopilot config"), recommend writing it:
`AP config-init --tool agrouter|autopilot`, then the keys from setup
Stage 4 steps 4–5. For a partial one ("is partial"), recommend
`AP config-init --tool <tool> --merge`: it keeps every current value and
its comments, and backs up the old file. A missing
`[agrouter] classifier_provider`, `[review] plan` or a wrong Codex quota
prefix is fixed with `config-set`. Copy the values the user has in the
global configs, so the repository keeps working the same way without
them. Mention the file states from `files`
(committed / modified / untracked).

## 2. Ask What to Change

Ask for the numbers in plain text ("Which to change? e.g. 2, 4 — or fix
the issues"). Question tools often allow only four options, too few here.
Then go through each picked property, one question at a time, showing the
current value as the first option ("keep").

## 3. Change Each Property

| # | Change | How |
|---|---|---|
| 1 | Executor | Apply the other recipe from `config-recipes.md`: `executor`, `codex_sandbox` (`danger-full-access` for the Codex executor, `read-only` for review), `pass_claude_md`, all three phase keys (`:` or the new executor's models, since the old ones no longer fit), for the Codex executor `codex_model` and `codex_reasoning_effort` (empty when routed, as the recipe writes them), the wrapper when it is missing (`AP install-wrapper`), and the Codex checks. Keep the Claude keys either way. |
| 2 | Phase models | Per phase: routed `:`, pinned model `opus:`, fixed `opus:high` (models from `AP catalog` for the executor's CLI), or **inherit global** (`AP config-unset .ralphex/config <key>`). Warn when inheriting would bring in the other executor's model (`spec_problems`). Under the Codex executor, `:` keeps `codex_model` and `codex_reasoning_effort`: the runner starts from them and only overrides the half a spec names. So when the user picks routed and either key has a value (any source, see property 3), say it still pins every routed phase and offer to write both empty locally (`--value ""`), as the recipe does. |
| 3 | Codex model / effort | Empty (routed) or a catalog value: `AP config-set .ralphex/config codex_model --value ...`. Empty must be written locally (`--value ""`) to override a global value; unsetting falls back to it. A value here applies to every phase whose spec leaves that half empty. |
| 4 | Routing docs | Ask the full new list (files and folders), then `AP set-docs --docs <files> --doc-dirs <folders>`: rewrites the `--doc` part of `claude_args` (everything after ` -- ` stays) and regenerates an autopilot wrapper. A hand-written wrapper is left alone; offer `--replace-wrapper` after showing its current content, or edit its docs list by hand with the user's consent. |
| 5 | Router on/off | Off: `claude_command = claude`, `claude_args = --dangerously-skip-permissions --output-format stream-json --verbose`, `codex_command = codex`, and fixed phase models (routing specs mean nothing without the router). The router's CLI switches (property 9) then no longer apply: the CLIs run directly. On: the recipe's `claude_command`/`claude_args` and the wrapper. |
| 6 | External code review | `external_review_tool` = `codex`, `none` or `custom` (+ `custom_review_script`); `codex_sandbox = read-only` for review. Not available with the Codex executor. Show `reviews.code_review` first; when Codex is configured but not usable, offer to fix it or set `none`. |
| 7 | Limits and timeouts | `wait_on_limit` (`15m`, or unset to inherit), `idle_timeout`, `session_timeout`; warn that `idle_timeout` must exceed the router timeout plus time to first output. |
| 8 | Large-doc routing | Set or unset the "Large Doc Sets" keys in `.agrouter/config` (`AP config-set`/`config-unset` with `--section`). Never an API key there. |
| 9 | CLIs for routing | Disable: `[cli.<name>] enabled = false` in `.agrouter/config`. Re-enable: unset the local key when only this repository disabled it; when the global router config disables it (`router_local.disabled_clis` lists it, the local file does not), write `enabled = true` locally, since unsetting falls back to the global `false`. Keep the executor's CLI enabled when it is routed. These switches affect routed calls only; a CLI run directly (No router) ignores them. |
| 10 | Classifier and key | Setup Stage 3. |
| 11 | Response times | Setup Stage 4b (this repository's `.autopilot/config`, or the user's global file). |
| 12 | Tool versions | Setup Stage 2. |
| 13 | Plan review | `AP config-set .autopilot/config plan --section review --value auto\|off\|codex\|claude`; recommend `auto`. Autopilot always asks which review to run; this key only sets the recommended option, taken after `review_pick` (`plan_review.recommended` shows it now). With `auto`, an unusable other model downgrades the recommendation to the same agent. Show both CLIs' login/routing status. Honor manual skips (`../../autopilot/references/reviews.md`). |
| 14 | Publish | `AP config-set .autopilot/config mode --section publish --value pr\|push\|off`; `draft = true\|false`. Check `gh auth status` (or `glab auth status`) for `pr`. |

Write with `--comment` explaining each new value, as the first setup did.
Do not touch keys of properties the user did not pick, even when they look
odd; mention them instead.

## 4. Review, Verify, Commit

1. Show what changed: `git diff -- .ralphex .agrouter .autopilot` for tracked files,
   `AP config-show` for untracked ones.
2. `AP doctor --json`: `spec_problems` empty, `doc_drift` null, executor as
   chosen.
3. Offer to commit (`git add` the changed files; message such as
   "Reconfigure autonomous runs: routed plan model, docs synced"). Offer to
   undo with `git checkout -- <files>` when the user does not like the
   result.

Never change config while an autopilot session runs in this repository
(`doctor.active_sessions`): the runner reads it at start, and a half-changed
config can break the next restart. Offer to wait for the session or stop it
first.
