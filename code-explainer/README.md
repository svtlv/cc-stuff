# code-explainer

Three read-only skills for **Claude Code, Codex and other agents that load `SKILL.md`**:

- **`summarize`** turns plans, designs, code files, folders, supplied text, links and chat context into short, understandable bullets linked to the original details. With no target, it asks what to summarize and suggests choices from the current context.
- **`explain`** gives a detailed walkthrough of a file, plan, uncommitted changes, branch, pull request, module, whole project, or question about code.
- **`explain-summarize`** combines the two in one report: a `## Summary` of linked bullets from `summarize`, then an `## Explanation` in the `explain` style. It loads the other two skills' instructions instead of copying them.

The detailed `explain` answer uses:

- the core idea first, in one or two plain sentences
- ASCII diagrams of flows, loops and timelines, each followed by a short **Note:** in plain words
- tables for "what goes where", before/after, settings and rules, instead of long bullet lists
- clickable `file:line` links to the exact code behind every claim
- only the important parts, with open or unverified items called out honestly

All skills only read. They never edit, commit or publish anything.

## Usage

### Short summaries

| You type | It summarizes |
|---|---|
| `/summarize` | asks what to summarize and suggests relevant choices |
| `/summarize docs/plans/playback.md` | the goal, major work, constraints and verification in a plan |
| `/summarize docs/design/` | the main ideas across the folder's design documents |
| `/summarize src/player.js` | the file's purpose and important behavior |
| `/summarize src/player/` | the main responsibilities and behavior across code files |
| `/summarize chat` | goals, decisions, completed work, open questions and next steps in this chat |
| `/summarize <pasted text or URL>` | the supplied material's main points and qualifications |

Each bullet starts with a bold phrase linked to the relevant source section or
code location. Text and chat without usable links get the same concise bullets
without invented links. Future work, verified results and unresolved questions
remain clearly distinguished.

**Claude Code:** `/code-explainer:summarize [target] [focus]`, or bare `/summarize` as a personal skill.

**Codex:** `$summarize [target] [focus]`

**Other agents:** load `skills/summarize/SKILL.md` using the host's skill mechanism or provide its contents as instructions. Tool names and link syntax adapt to the host; plugin installation support depends on that host.

### Detailed explanations

| You type | It explains |
|---|---|
| `/explain` | the subject of the current conversation, or asks you what to explain if that is not obvious |
| `/explain docs/plans/2026-plan.md` | a plan: goal, design, tasks with real status, decisions, risks |
| `/explain /plans/2026-plan` | the same; partial paths and a leading `/` resolve against the repository |
| `/explain src/Services/Foo.cs retry logic` | one file, narrowed to a focus |
| `/explain src/Services/CoreAIService` | a module or directory |
| `/explain changes` | uncommitted changes (`staged` for the index only) |
| `/explain branch` | the current branch against the default branch |
| `/explain #42` or a PR URL | a GitHub pull request (needs an authenticated `gh`) |
| `/explain a2fca26` or `/explain a..b` | a commit or a range |
| `/explain project` | the whole repository |
| `/explain where is the retry policy?` | a question, answered directly first |

Text after the target narrows the focus. Follow-up questions in the same session get answers in the same style.

**Claude Code:** `/code-explainer:explain [args]`. For the bare `/explain`, install it as a personal skill (below).

**Codex:** `$explain [args]`

### Summary plus explanation

| You type | It reports |
|---|---|
| `/explain-summarize` | the conversation's subject, or asks what to report on |
| `/explain-summarize docs/plans/2026-plan.md` | linked summary bullets of the plan, then a detailed walkthrough |
| `/explain-summarize branch` | the current branch: short summary first, then diagrams, tables and links |
| `/explain-summarize #42` | a pull request, summarized and then explained |
| `/explain-summarize chat` | this chat's decisions as bullets, then how they fit together |

It accepts every target that `explain` or `summarize` accepts. The report looks like this:

```
# Title
core idea in one or two sentences
## Summary       ← summarize rules: flat linked bullets
## Explanation   ← explain rules: diagrams with Notes, tables, file:line links
```

**Note:** the core idea appears once at the top, then the same facts are given short and then in depth, with the same links in both parts.

**Claude Code:** `/code-explainer:explain-summarize [target] [focus]`, or bare `/explain-summarize` as a personal skill.

**Codex:** `$explain-summarize [target] [focus]`

**Other agents:** load `skills/explain-summarize/SKILL.md` through the host's skill mechanism. It reads `../explain/SKILL.md` and `../summarize/SKILL.md` with ordinary file tools, so keep all three skill directories side by side.

## Installation

### Claude Code

```bash
claude plugin marketplace add svtlv/cc-stuff
claude plugin install code-explainer@svtlv-cc-stuff
```

For bare `/explain`, `/summarize` and `/explain-summarize` commands, copy the skills into your personal skills:

```bash
git clone https://github.com/svtlv/cc-stuff.git
mkdir -p ~/.claude/skills
cp -R ./cc-stuff/code-explainer/skills/explain ~/.claude/skills/
cp -R ./cc-stuff/code-explainer/skills/summarize ~/.claude/skills/
cp -R ./cc-stuff/code-explainer/skills/explain-summarize ~/.claude/skills/
```

For local development: `claude --plugin-dir ./cc-stuff/code-explainer`.

### Codex

```bash
codex plugin marketplace add svtlv/cc-stuff
codex plugin add code-explainer@svtlv-cc-stuff
```

For a standalone install, copy any of `skills/explain`, `skills/summarize` and `skills/explain-summarize` into `~/.agents/skills/` (personal) or the repository's `.agents/skills/` (project). `explain-summarize` needs the other two copied beside it. Restart the session after installing.

## Layout

```
skills/
├── explain-summarize/
│   └── SKILL.md          # load both skills → one target → Summary, then Explanation
├── summarize/
│   └── SKILL.md          # select input → read → short bullets with verified links
└── explain/
    ├── SKILL.md          # resolve the target → gather → write → follow-ups
    └── references/
        ├── style.md      # the style guide and diagram templates
        ├── targets.md    # gather commands and section outlines for each mode
        └── example.md    # a finished answer at the expected quality
```

## Requirements

No extra tools for summarizing supplied text or chat. File/folder summaries need
the host's file-reading tools; URLs need a supported reader. Repository comparison
explanations need `git`. For pull requests, use an authenticated
[`gh`](https://cli.github.com/); without it, `explain` offers the local branch instead.
