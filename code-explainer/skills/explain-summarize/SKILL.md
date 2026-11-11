---
name: explain-summarize
description: This skill should be used when the user runs "/explain-summarize" or "$explain-summarize", or asks to "explain and summarize this", "summarize and explain", "give me a summary and an explanation", "give me a report on this", "summary first, then the details", or wants one report that starts with a short summary and continues with a detailed walkthrough of a file, folder, plan, design, changes, branch, pull request, commit, project, question, text, link or the current chat. It reuses the summarize and explain skills of the code-explainer plugin.
argument-hint: "[file | dir | PR# or URL | branch | commit | changes | project | text | chat | question] [focus]"
allowed-tools: Bash, Read, Grep, Glob, WebFetch, AskUserQuestion
metadata:
  version: "0.1.0"
---

# Explain and Summarize

Produce one report about one target: a short linked **Summary** first, then a
detailed **Explanation**. Do not invent a third style. The Summary follows the
`summarize` skill and the Explanation follows the `explain` skill, both of which
ship next to this skill.

Only read. Never edit, commit, push, publish or send anything as a side effect.

## Step 1 — Load the Two Skills

Resolve paths from the directory that holds this `SKILL.md`. Read both files in
full before doing anything else:

| Part of the report | Instructions to load |
|---|---|
| Summary | `../summarize/SKILL.md` |
| Explanation | `../explain/SKILL.md`, then the `references/` files it names |

Read the files directly with the host's file tools. This works in Claude Code,
Codex and any other agent that can read files, and does not need a Skill tool.
Re-read `../explain/references/style.md` before writing, as `explain` requires.

If either file is missing, as in a standalone copy of this one directory, stop.
Tell the user that `explain-summarize` needs the sibling `explain` and
`summarize` directories installed beside it, and name the folder they belong in.
Do not improvise a replacement style.

Where a rule in this file and a rule in a loaded skill disagree, this file wins
for the report's layout, and the loaded skill wins for everything inside its part.

## Step 2 — Resolve One Target

Use one target for both parts. Split the argument into `<target> [focus]`, and
apply the focus to both parts.

- **Code targets** (file, directory, PR, branch, `changes`, commit, `project`,
  question): resolve them with the table and path rules in `explain` Step 1.
- **Other material** (URL, pasted text, attachment, "this chat", "the plan
  above"): resolve them with the input rules in `summarize`.
- **No argument:** follow `explain`: use the conversation's subject only when it
  is obvious, and say in one line what was picked ("Reporting on: the retry
  branch."). Otherwise ask one question with two or three concrete choices from
  the context, using the host's question tool or plain text, and wait.

When the target is prose or a chat and has no code behind it, the Explanation
still uses the `explain` style (core idea, diagrams with a Note, tables), with
links to document sections instead of code lines.

## Step 3 — Read the Material Once

Gather with the deeper of the two rules, which is `explain` Step 2 and
`explain/references/targets.md` for code. That reading also covers what
`summarize` needs. Keep exact line numbers for every link, from `grep -n` or a
numbered read, and reuse them in both parts. Note open items (TODO, UNVERIFIED,
unchecked plan steps) once, so both parts report them the same way.

If part of the material is out of reach, say what was covered at the top of the
report, as both skills require.

## Step 4 — Write the Report

Use this layout. Write the core idea once, under the title, and do not repeat
it inside either part.

```markdown
# <Plain title of the target>

<One or two plain sentences: what this is and why it matters.>

## Summary

- **[Main point](path/file.md:12)**, short explanation.
- **[Next point](src/file.py:40)**, short explanation.

## Explanation

### Big picture
<flow diagram>

**Note:** <what the diagram shows>

### <Important part>
...
```

| Part | Rules | Changes from the original skill |
|---|---|---|
| Summary | `summarize`: flat bullets, bold linked opening phrase, about 3–10 points, meaning and uncertainty kept | It now sits under a `## Summary` heading. No diagrams or tables inside it. |
| Explanation | `explain`: big picture diagram, a **Note:** under every diagram, tables, `file:line` links, one concrete example, honest open items | Its `##` sections become `###`, and the `#` title and core idea are not repeated. |

Keep the two parts consistent:

1. **Same facts.** A point in the Summary must appear, with more depth, in the
   Explanation. Do not contradict, and do not add a claim to only one part.
2. **No copy.** The Explanation builds on the Summary. It does not restate the
   bullets as prose.
3. **Same links.** Link the same lines for the same claim in both parts.
4. **Scale with the target.** A small file gets a few bullets and one or two
   explanation sections. A PR or a project gets more.
5. **No preamble, no closing recap.** End after the last explanation section.
6. **Write in the language of the user's request.**

## Step 5 — Follow-up Questions

Answer follow-ups in the `explain` style, as `explain` Step 4 describes. Give a
fresh Summary only when the user asks for one again.

## Host Notes

| Topic | Claude Code | Codex | Other agents |
|---|---|---|---|
| Invoke | `/code-explainer:explain-summarize [args]`, or `/explain-summarize` as a personal skill | `$explain-summarize [args]` | Load this `SKILL.md` through the host's skill mechanism, or give it as instructions |
| Load sibling skills | Read tool | shell or file tool | any file-reading tool |
| Ask the user | AskUserQuestion | plain-text question, then stop | the host's question tool, or plain text |

Install `explain`, `summarize` and `explain-summarize` side by side in the same
skills folder. Use the link format the host makes clickable, as described in the
two loaded skills.
