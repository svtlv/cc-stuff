# Transcript protocol

Every agent — this session and each partner — communicates through one append-only file: `.partner/chat.md` at the repo root.

A single shared transcript rather than per-partner channels is what makes group debate work: with three partners, everyone sees everyone's argument, so a point raised by `p2` can be answered by `p3` without anyone relaying it.

## Identity

Every agent is an ordinary entry in `roster.json`, including the one that spawned the others:

```json
{
  "self":  "p1",
  "baton": "p1",
  "partners": { "p1": {...}, "p2": {...} }
}
```

`baton` names the agent the human most recently gave an instruction to, and it is the only agent that may edit files. See "How the baton moves" below.

Whoever runs `init` takes `p1`; spawned agents continue the numbering. `spawn` calls `init` itself from its `--me-*` arguments, so registering yourself is never a separate step that can be completed on its own. Every entry has the same shape — provider, model, effort, auto level, status — because `init` and `spawn` build them with the same code. The agent that starts the session is a participant, not the thing the others hang off.

### Identity is per-process

Each agent runs through its own wrapper at `.partner/<id>/p.cmd` (or `p.sh`), which exports `PARTNER_ID` before calling the script:

```
@echo off
set PARTNER_ID=p2
"python" "…/partner.py" %*
```

So `claim`, `read`, `wait` and `send` need no id passed to them — the process knows who it is. This matters more than convenience: if identity were read from the shared `roster.json`, every agent would resolve to whoever ran `init`, and a partner running `claim` would take the baton *on that agent's behalf*. `self` survives only as the fallback for the one agent with no wrapper of its own — the session someone typed the skill into.

`stop --all` stops every other agent and leaves the caller's own entry alone, since it has no tab to close.

The single asymmetry in the system is the baton, and it moves.

## How the baton moves

The baton is meant to track the human's attention: whoever they are talking to is whoever writes. No supervising process exists, and nothing outside a terminal tab can observe which tab is being typed into — so the agent that receives the instruction is the one that has to record it:

```bash
.partner/p.sh claim        # "the human just told me to do something"
```

`claim` sets the baton to the caller and announces the move in the transcript, so every other agent learns about it on its next `read` or `wait`.

The distinction each agent is briefed on:

| Where the request came from | What to do |
|-----------------------------|------------|
| The human, typing in this agent's tab | `claim`, then act |
| Another agent, via `wait` | do not claim, do not edit — argue and propose |
| Its own launch prompt | do not claim — `spawn` started it; the boot line says so |
| An automated wake-up typed into its tab | do not claim — another agent noticed its silence |

The second row is the load-bearing one: without it, one agent could tell another to make a change and the baton would drift away from the human entirely.

Rows three and four exist because both messages arrive through the same channel as the human's typing and are indistinguishable from it by construction — a terminal tab has one input. Text is therefore the only defence available, and it is applied at both ends: the launch prompt and the wake-up line each open by disowning themselves, and every briefing names them as the two impostors. Because "is this the human?" is a judgement call, and a wrong answer silently takes write permission from whoever is working, shared state carries a guard too: `claim` refuses when the caller started less than `CLAIM_GRACE` (90s) ago and has never spoken in the transcript, and says to use `claim --force` if the human really did just address a brand-new partner. That combination is what turns an intermittent, invisible failure into a refusal with an explanation.

Every `read` and `wait` prints the current holder as its first line, which puts the reminder exactly where it is needed — an agent reads its inbox immediately before deciding what to do about it. `baton --to <id>` remains for deliberate handover.

Because partners launch with permission prompting relaxed, this is a rule agents keep rather than a sandbox that stops them. It holds because every briefing carries the reason, not just the instruction: concurrent edits produce conflicts nobody can see.

## State layout

```
.partner/
├── roster.json        every agent, which one is you, who holds the write baton
├── chat.md            the shared transcript
├── p.cmd / p.sh       short wrapper -- how agents invoke partner.py
└── <id>/
    ├── p.cmd / p.sh   this agent's wrapper -- carries its PARTNER_ID
    ├── lastseen       heartbeat: when this agent last acted
    ├── waiting        who is polling right now: token, pid, deadline, heartbeat
    ├── .nudge         when this agent was last woken (throttle)
    ├── .redeliver     when it was last handed its unanswered backlog
    ├── seed.md        the briefing this agent was launched with
    ├── handoff.md     what was decided before it joined
    ├── cursor         byte offset of the last message handed to it
    ├── floor          byte offset where its responsibility starts
    └── run.cmd|.sh    the exact command its terminal tab runs
```

Every agent has an `<id>/` directory, including the one that started the session. If one of them were missing a briefing or a wrapper, it would not be a peer.

Past sessions live alongside, each a complete copy of the above:

```
.partner/sessions/20260905-185718/
├── meta.json          id, label, agent configs, message count
├── roster.json        who was in it
├── chat.md            what they said
└── <id>/              each agent's briefing as it stood
```

`.partner/` is added to `.git/info/exclude` by `partner.py init`, so it is ignored locally without modifying a tracked `.gitignore`.

## Liveness

`roster.json` records `status: "running"` because nothing has told it otherwise. Close a tab and the claim survives, so it cannot decide whether to add a partner or start over. Liveness is evidence instead:

- **Heartbeat.** Every agent writes `<id>/lastseen` whenever it runs `wait`, `read`, `send` or `claim`. Acting is what proves it is alive; a background `wait` stamps it every few seconds. An agent quiet longer than the window (360s) is reported `idle` while its tab exists, because every agent ends its turn once it owes nothing.
- **Listener heartbeat.** Separately, a polling `wait` refreshes `<id>/waiting` every cycle. That answers a different question — *is anyone listening right now* — and it is refreshed rather than merely created, so a `wait` that was killed stops proving it exists instead of leaving a marker that lies.
- **Tab list.** Where the terminal can enumerate its own tabs (`list` + `list_titles` on its entry), that reports whether a tab titled `partner:<id>` still exists — better evidence than a heartbeat, since it describes the tab rather than what the agent last did in it. Terminals that cannot enumerate leave the heartbeat as the only evidence, and some rename tabs from the running process, so a missing title is treated as weak evidence and only ever downgrades an agent that has *also* stopped checking in.

A **fresh heartbeat outranks the tab list**. An agent that ran a command seconds ago is alive regardless of what the tab list says — checking tabs first would call it dead whenever the tab was renamed, launched with `--no-tab`, or started outside Orca. The tab check only downgrades an agent that has *not* checked in recently.

`state` combines all of this with the session history into one answer, and ends with a `recommend` (`add` / `ask` / `new`).

The `recommend` is advisory. What actually decides add-vs-fresh is the **invocation**: a bare `/partner` starts a new session (`spawn --fresh`), `/partner add` joins the live one (`spawn`). `state` is read to *narrate* that — "you are about to archive 2 agents, 6 messages" — and to catch the cases where the user's intent and the roster disagree (a bare `resume` with nothing archived, an `add` with no session running).

It is one call on purpose: roster, liveness and history belong together, and fetching them separately invites deciding on half the picture.

## Sessions

A session is one arrangement of agents plus the transcript they produced. Starting a new one never destroys the old: `archive` moves the live session under `sessions/` whole — files moved, not copied and deleted — and leaves a clean slate. `spawn --fresh` does this as its first act, so a new invocation of the skill cannot silently overwrite the previous argument.

A session with **only this session in it and nothing said beyond system notices** is not archived — there is nothing to come back to, and filing it away would force a needless re-init. So `spawn --fresh` on a cold roster just adds the first partner; the archive step is a no-op.

`meta.json` records each agent's provider, model, effort and auto level, which is what makes rebuilding possible. The `label` is the first non-`system` message in the transcript, so a list of sessions reads as a list of questions rather than timestamps.

`resume` restores a session and relaunches **every** agent from its roster entry — the same `relaunch()` path `spawn` uses, so a rebuilt agent is indistinguishable from a freshly created one. Two details make it continue rather than restart:

- Each agent's `handoff.md` is rewritten from the restored transcript, so it knows what was already settled.
- Each cursor is set to the end of that transcript. Without this an agent would find the entire history sitting in its inbox and try to answer all of it.

`spawn` sets a new partner's cursor the same way, once, right after announcing its arrival: the backlog is already in its `handoff.md`, so its first `wait` should block for the question being put to it, not return the whole transcript.

A `system` message then announces the resume, which is the one new message every agent sees.

Resuming archives whatever was live first, so switching between sessions never loses work. The agent running `resume` adopts the `kind: "session"` entry — a restored roster would otherwise still name the agent that created it, on a machine where that process no longer exists.

## Message format

```markdown
### 2026-09-05T09:52:54Z | from:p1 | to:@all | baton:p1

Body text. Markdown, any length.

<!--/msg-->
```

- `from` — sender id: any agent id, or `system` for tooling notices such as a baton move.
- `to` — an agent id, or `@all`.
- `baton` — who held write permission when the message was written. Useful when reading history: it explains why an agent argued instead of editing.
- `<!--/msg-->` — the record separator. It is an HTML comment, so `chat.md` still renders cleanly in any markdown viewer.

Append only. Rewriting history breaks every agent's cursor and silently drops messages.

## Cursors, floors, and re-delivery

Each agent tracks two byte offsets into `chat.md`:

- **`cursor`** — how far it has been *handed* messages. `read` and `wait` return messages after it that are addressed to the agent or `@all` and were not sent by it.
- **`floor`** — where its responsibility starts, written when it joins or is resumed. Everything above the floor is history it was briefed on, not mail it owes an answer to.

`read --peek` reads without advancing the cursor — useful for inspecting what an agent is about to see without consuming it.

Because these are byte offsets rather than message indexes, an agent that was closed and restarted resumes exactly where it left off.

### The cursor moves only after delivery succeeds

`read_new` does not advance the cursor; `commit_cursor` does, after the output has been printed and flushed. The two were one step until a message went missing: a `wait` killed between "cursor advanced" and "output reached the model" consumed the message and lost it, and no later `wait` could return it, because the cursor was already past. That is not a rare race: every CLI caps command runtime, and a cap shorter than the wait makes the kill routine rather than exceptional — a process killed before output is read can still lose its delivery. Delivering a message twice costs a duplicate; delivering it zero times costs the debate a participant.

### `wait` asks the transcript, not just the cursor

The cursor answers "has this been handed over", which stops being the right question the moment a handover fails. Two ways it fails, and both used to be unrecoverable from inside:

- a `wait` killed mid-print, as above;
- an agent that was given a message and ended its turn without replying — the cursor is past it, so nothing will ever show it again.

Either way the message is gone until another agent notices the silence and pings. So `wait` also asks a cursor-free question every poll: *is anything addressed to me still unanswered?* — `pending_for`, the same check the Stop hook uses, bounded below by the floor.

That question is asked **per sender**, not against one "since I last spoke" line. With three agents talking, a global line means a reply to `p1` also discharges the debt to `p3`, so a message that arrived alongside another and was never answered becomes invisible to every later check — permanently. What answers a sender is a message **to that sender**, or one to `@all`; `pending` lists what is owed and to whom. Anything older than `REDELIVER_AFTER` (45s) comes back marked **re-delivered**, and a reply is what clears it.

The cursor only ever moves **forward**. A `read` in one process and a `wait` in another each hold an offset captured at a different moment, and letting the older one win would rewind the cursor and re-deliver a stretch that was already answered.

Three things keep that from becoming noise:

- **New messages win.** Re-delivery only runs when nothing new arrived, so the normal cycle never sees it.
- **The age threshold.** A message being replied to right now is being worked on, not dropped.
- **A throttle.** `<id>/.redeliver` holds off repeats for 90s, so an agent that cannot answer is reminded at a bounded rate instead of spinning through instant returns.

`read` shows the same backlog under "still unanswered, from earlier", with no age threshold — an agent running `read` is asking what it owes.

The floor is what makes this safe for a new partner: without it, an agent that has never spoken would owe a reply to every `@all` message in the transcript and answer the entire backlog on its first `wait` — the failure the cursor reset was introduced to prevent.

This and the nudge cover different halves of the same problem. Re-delivery recovers a message an agent dropped while it was listening; the wake-up recovers an agent that has ended its turn. An agent woken by a nudge is told to run `read`, which is exactly where its unanswered backlog is waiting.

## How an agent takes part

There is no supervising process. Each agent is an ordinary interactive session that drives itself, using the wrapper written at `.partner/p.cmd` (Windows) or `.partner/p.sh` (macOS/Linux):

```bash
.partner/p2/p.sh wait                     # blocks until addressed
.partner/p2/p.sh send --to @all --text ".."
.partner/p2/p.sh claim                    # the human just addressed me
.partner/p2/p.sh spawn --provider gemini  # bring in another partner
.partner/p2/p.sh list                     # who is here, who holds the baton
```

Any agent can run any of these, `spawn` included. `.partner/p.sh` (no id) is the shared wrapper, for a human at a shell.

`wait` blocks until a message arrives for that agent, so the agent has something to answer rather than needing to be driven. Only a Claude agent runs it, in the background, because only Claude's harness re-invokes the model when a background command returns. It returns within `--timeout` (default 7000s) even when nothing arrives.

### Command timeouts

A background Bash command in Claude Code may run for up to 7200000 ms, and the foreground cap does not apply to it. Without an explicit `timeout`, the default of 30 minutes kills the wait early and costs a turn, so the briefing and the Stop hook both name it. `wait` announces itself before polling, so even a killed wait carries its instructions. No other CLI runs `wait`, so its command caps do not matter.

### One wait consumes at a time

Two `wait` processes for the same agent split its inbox: each message is returned by whichever polls first, and a message handed to a wait whose output nobody reads is a message that was consumed and never answered. It is the same loss as a killed wait, arriving by a different route.

Worse, duplicates used to be *manufactured* here. The marker was removed in the wait's `finally` block, so the **first** wait to return deleted it while a second was still polling — after which `is_listening` said nothing was listening, and the Stop hook and `state` both told the agent to fix that by starting a wait. One extra wait bred the next.

So ownership is explicit:

- `<id>/waiting` holds a **token, pid, deadline and heartbeat**, refreshed every poll by whoever owns it.
- A `wait` that finds a live marker belonging to somebody else **idles** — it polls without reading, consumes nothing, and reports "another `wait` was already listening" when its own timeout expires. It idles rather than exiting because an immediate exit hands control straight back to the agent, which arms another wait, which exits immediately, and so on.
- If the owner stops refreshing (killed, or its process died), the idling wait **takes over** within a cycle or two. Nothing waits out a deadline that will never arrive.
- Only the owner clears the marker on the way out.

`read` prints which state the caller is in — *a wait is already in flight for you*, or *no wait is in flight for you* — because "have I already armed one?" was previously unanswerable except by guessing, and guessing wrong is what produced a second one.

The session agent backgrounds its `wait`, so a Stop firing immediately afterwards can beat the new process to writing its marker. The Stop hook covers that race by looking for the marker for up to `ARM_GRACE` (5s) before it calls the agent deaf (`listening_soon`). It used to accept any `lastseen` under 15s instead. That was wrong because every `send` refreshes `lastseen`: a session agent that replied and ended its turn with no `wait` armed passed as listening, and a partner looped for twenty minutes waiting on it.

### Waking an agent that stopped listening

The shared Stop hook is available in Claude Code and Codex when enabled and trusted. Other CLIs may or may not have an equivalent, and this plugin cannot require one — so for a tab that ends its turn the recovery comes from outside the agent entirely: type into the tab, exactly as the human would. That works whatever is running in it.

```bash
.partner/p.sh nudge --id p2      # one agent
.partner/p.sh nudge              # every idle agent that owes a reply
```

`spawn` records how each tab was opened (`tab_kind`, `tab_handle`, `tab_title` on the roster entry) because the terminal that owns a tab is not recoverable afterwards. *How* to type into one is a property of the terminal entry — its `send` template — so this works for a terminal the user described in their own file exactly as for a built-in, and a terminal that cannot be typed into is expressed by having no `send` rather than by being special-cased in code. Where the handle was never recorded, `list_handle` recovers it by title. See `references/terminals.md`.

Where it fires:

- **The waker** runs `wake_owed` on every change to `chat.md` — see "Listening without polling".
- **`send`** runs the same pass itself when no waker is alive.
- **`read`, `wait` and `send`** name agents that cannot be reached at all (no `wait` in flight and no tab to type into), so an agent about to conclude that a partner has nothing to say learns that nobody can hear it instead.

`wake_owed` keeps one wake-up per owed message through `<id>/.woke`, so the waker and `send` never type the same line twice. The text says it is automated and not the human, and tells the agent not to claim the baton — see "How the baton moves".

**The waker types for agents that cannot.** Typing means running the terminal's own CLI, and a sandboxed agent may not be allowed to. Codex under `-s workspace-write` cannot even read Orca's runtime file, so every `orca terminal send` it runs fails. When a nudge fails, `nudge_agent` writes `<id>/wake.json` for the target, and the session's waker performs it within a second. The waker is one detached `partner.py waker` process per session:

- `ensure_waker` starts it from `init`, `spawn`, `resume`, `send`, `wait` and every prompt and Stop hook, whenever `waker.json` has no fresh heartbeat. Launches are throttled to one per `WAKER_RELAUNCH` (60s).
- Before it takes the job it lists tabs with the terminal's own CLI (`can_drive`). A waker started from inside a sandbox fails there and exits, leaving the job to one started by an agent that can drive the terminal.
- It skips a request older than `WAKE_REQUEST_TTL` (120s) or for an agent that is listening again, and sends the rest unthrottled, since the requester already applied the throttle.
- It exits once no agent is running, or after `WAKER_IDLE_EXIT` (6h) with no activity.

**A CLI that has exited is left alone.** A wake-up only works if something in the tab reads it. When the agent's CLI is gone (killed, crashed, or quit), a shell reads it and rejects it, over and over. `<id>/.unanswered` counts wake-ups typed since the agent last acted, and any command it runs resets the count by moving `lastseen`. At `UNANSWERED_NUDGES` (3), `nudge_agent` stops typing and posts one system message to `@all` naming `restart --id <id>`. `idle` then reports it as exited. Terminal metadata cannot decide this: Orca still reports `agentIdentity: codex` for a tab whose Codex has exited. So the evidence is behavioral and works in any terminal.

**Stopping your own `wait`.** `unwait` kills the one process named in the caller's `<id>/waiting` marker, and only after its command line proves it is a Python `partner.py wait`, since PIDs are reused. It never matches by name. Partner CLIs carry their launch prompt on their command line, so the launch prompt avoids the literal `partner.py`.

Without a live waker, a failed nudge says so: `nudged: false`, with the reason and a note that the request is waiting for a waker. 

The briefing tells each agent to start every turn with `read`, answer, and end the turn — and what it does on each turn depends on whether it holds the baton. The holder runs the debate and makes the change (state a position, `send --to @all --wait`, weigh the replies, act). Everyone else advises — and not only in reply to the holder: any non-holder can open a thread with any other, `send --to p3` as readily as `--to @all`, so three or four agents can work a question out among themselves and hand the holder a joint recommendation. The baton moves with the human's attention; each `read` and `wait` reprints the holder, and an agent switches roles the moment it changes.

The starting session listens like any other agent of its CLI: a Claude Code session keeps one background `wait`, and a Codex session runs no `wait` and is woken by typing into its tab when that tab can be typed into. Read [host integration](hosts.md) for the exact mechanism. This changes how the participant listens, not its role in the debate.

### Listening without polling

A `wait` that times out with nothing hands control back to its model, and the model spends a turn deciding to run `wait` again. Three agents cycling every 90s through an hour nobody is at the keyboard used to be most of what a session cost. So no agent polls. An agent that owes nothing ends its turn and costs nothing until input arrives, and how input reaches it depends on its **listen mode** (`waking.listen_mode`):

| Mode | Who | How a message reaches it after its turn ended |
|------|-----|-----------------------------------------------|
| `background` | every Claude agent, tab or session | one background `wait --timeout 7000` (Bash `timeout: 7200000`) returns; the task notification re-invokes the model |
| `typed` | any other CLI whose tab can be typed into | the waker types a wake-up line into the tab |
| `manual` | any other CLI whose tab cannot be typed into | only the human, by typing in the tab |

**Who gets woken.** `wake_owed` is the one pass, run by the waker on every change to `chat.md` and every `WAKE_RECHECK` (15s), and by `send` itself when no waker is alive. It types into an agent only when all of these hold:

- it owes a reply (`pending_for`, with the baton holder included, so late advice still reaches an idle holder);
- it has no `wait` in flight and no turn open (`<id>/turn`, written by the prompt hook and removed by the Stop hook);
- it started more than 30s ago, and its tab can be typed into.

`<id>/.woke` records the newest owed message it was woken for, keyed by content because timestamps have one-second resolution. The agent is woken again for a newer message, or after `NUDGE_WINDOW` (120s) if it never acted on the last wake-up — never twice for a message it already reacted to.

**A busy agent is never typed into.** The hooks reach it instead: `UserPromptSubmit` opens the turn and adds new messages to the prompt, `PostToolUse` adds what arrives mid-turn after each tool call, and `Stop` closes the turn unless a reply is owed. Handing messages over through a hook moves the cursor, as `read` does, so a background `wait` still in flight does not return them a second time. Whether they are answered is read from the transcript, so the Stop hook still holds the turn until they are.

**Closing threads.** `send --final` starts the body with `[final -- no reply needed]`; a final message creates no debt. Two advisors answering each other would otherwise wake each other forever, so `past_hop_limit` stops creating debts after `HOP_LIMIT` (8) messages in a row from agents that did not hold the baton when they sent them. The holder speaking resets the chain. `read`, `wait` and the hooks say when the limit is reached.

`idle` prints, for every agent, what it is doing and how the next message reaches it (`working (turn open)`, `owes a reply to p1`, `idle, background wait armed`, `idle -- the next message types a wake-up into its tab`, `idle -- ... only the human can wake it`).

Versions before 1.5.0 paused the whole session once every agent had been finished for a minute, and looped until then. `resume` and `archive` still remove a leftover `paused.json`.

## Opening the session

A roster of briefed agents is not a debate. Everyone is blocked on `wait`, the transcript holds nothing but join notices, and nobody has been asked anything — so the session has to be opened deliberately:

```bash
.partner/p.sh kickoff --text "what the human wants worked on"
.partner/p.sh kickoff                    # they have not said yet
```

Both send one message to `@all`, and both are canned text rather than improvised, so every agent receives the same protocol in the same words. An improvised "have a look around" produces a summary nobody asked for and an agent that then stops answering.

**With `--text`**, the human's instruction is framed as the session's opening question, naming the sender as the baton holder: the others verify it against the code and argue, the holder acts.

**Without**, it sends the orientation brief — the agents split the repository between them, read it, ground every claim at `path:line`, and hand each other one joint picture of what the project is and what to be careful with. Its constraints are the point: **nobody edits anything**, not even the baton holder; claims are split rather than duplicated; it converges in at most two rounds and everyone returns to `wait`. The goal is that the human's first real question is not answered from a cold read — not to fill the silence with work nobody asked for.

Skip the orientation when the context is already there: a resumed session, or a `--context` briefing that already says what the work is. Re-reading the repo to restate it costs everyone a round.

## What an agent is launched with

`.partner/<id>/seed.md` contains:

1. **Who it is** and who its partners are, stated as equals with nobody in charge.
2. **That a human may address it at any time** (in its tab, or — for the session agent — through its own harness); when they do it holds the baton and runs the debate before acting, and the rest of the time it advises whoever holds it and argues with the other advisors. Then it goes idle again.
3. **The commands** for `wait`, `send` and `list`, using the short wrapper.
4. **The baton rule** — `claim` when the human addresses it, never when another agent does — with the reason: concurrent edits produce conflicts nobody can see.
5. **How to argue well** — lead with a position, disagreement needs a concrete alternative, verify against the code, cite `path:line`, stop after two exchanges without movement.
6. **How it listens** (background `wait` for Claude, none for any other CLI) and **the turn itself**, as numbered steps.
7. **A pointer to its `handoff.md`**, which carries the working tree, the debate so far, and whatever briefing was written at spawn.

The starting prompt passed on the command line only says "read `seed.md` and follow it", which keeps a multi-kilobyte briefing out of a terminal command line.

## The hooks

`hooks/hooks.json` registers three hooks, all through `hooks/hook.sh <subcommand>`. `hook-prompt` (UserPromptSubmit) opens the agent's turn and adds new messages to the prompt. `hook-post-tool` (PostToolUse) adds messages that arrive mid-turn; `hook.sh` leaves before starting Python when no `.partner/roster.json` exists above the working directory. `hook-stop` (Stop) runs each time any agent's turn ends. It makes two checks, in order, and when neither blocks, it closes the turn.

**1. Unanswered messages.** Blocks when a message in `chat.md` is addressed to that agent (by id or `@all`), arrived after the agent last spoke, and has no reply yet: the agent is told to `read` and `send` before it can stop.

**2. A Claude agent with no background `wait`.** Blocks only for an agent in `background` mode. Every other agent may end its turn freely, since the waker reaches it by typing. A Claude agent is reached only through its own `wait`, and that wait costs nothing while it sleeps, so the hook insists on it. `wait` holds `<id>/waiting` for as long as it polls, so the marker answers *"listening right now"*. A background `wait` armed just before a Stop has not written its marker yet, so the hook looks for it for up to `ARM_GRACE` (5s).

Details that keep it from getting in the way:

- **The baton holder is exempt from check 1.** It drives the change; it is not waiting on anyone. `pending_for` returns nothing for it.
- **Own messages, `--final` messages and messages past the hop limit never count.**
- **It nags at a bounded rate.** `.partner/<id>/.stop-nag` holds the transcript size at the last message block — an identical size means "already told them". `.stop-nag-live` throttles the listener warning to once every 120s. A genuinely stuck agent is never wedged in an unbreakable loop.
- **It fails open.** No Python, no `.partner/`, or an unregistered/stopped agent — the stop proceeds.
- **Identity** comes from `PARTNER_ID`, which `run.sh`/`run.cmd` export into the tab, so the hook resolves the same agent the wrappers do rather than assuming it is the session.

## Convergence

There is no mechanical round limit, because no supervising process exists to enforce one. The briefing asks each agent to stop after two exchanges with no movement, state the disagreement fairly, and let the human decide.

That is the right place for the rule. A deadlock between two models is a genuine signal that the question is a judgement call, and the human is the one who should break it; grinding on produces confident-sounding convergence that reflects stamina rather than correctness.

## Where the code lives

`scripts/partner.py` is only the entry point. Every wrapper, hook and briefing calls that path, including `.partner/<id>/p.cmd` files written by older versions, so it never moves. The code is in the `partner_agents` package beside it. Standard library only, and each module imports only from the layers above it in this list:

| Module | Holds |
|--------|-------|
| `timing.py` | every clock: `BACKGROUND_WAIT`, `MARKER_STALE`, `REDELIVER_AFTER`, `LIVE_WINDOW`, `HOP_LIMIT`, `WAKE_RECHECK`, ... |
| `util.py` | helpers with no partner knowledge: `utcnow`, `age_seconds`, `nag_throttled`, `run_command`, `emit` |
| `state.py` | `.partner/` paths, `roster.json`, `me_id`, `baton_of`, the per-agent wrappers |
| `transcript.py` | `chat.md`: `append_msg`, `read_new`, `commit_cursor`, `set_floor`, `pending_for`, `dropped_for`, `FINAL_TAG`, `past_hop_limit` |
| `prompts.py` | every agent-facing text: `SEED`, `LISTEN_BACKGROUND`, `LISTEN_TYPED`, `LOOP_STEPS`, `LOOP_TAB`, `LOOP_SESSION`, `EXPLORE_BRIEF`, `OPENING` |
| `providers/` | `recipes`, `binaries`, `probe`, `discovery`, `overrides`, `models`, `resolve`, `validate`, `launch` |
| `terminals/` | `catalog` (the terminal table), `detect` (finding and probing terminals), `tabs` (opening a tab) |
| `presence.py` | the `waiting` marker, `is_listening`, `lastseen`, `liveness` |
| `waking.py` | `listen_mode`, `wake_owed`, `nudge_command`, `nudge_agent`, `silent_agents`, `silence_notice` |
| `briefing.py` | `boot_prompt`, `build_seed`, `write_briefing` |
| `sessions.py` | `archive_current`, `read_sessions`, `relaunch` |
| `idle.py` | `agent_state`, `last_activity`, `mark_active` |
| `commands/` | one `cmd_*` per subcommand: `session`, `messaging`, `status`, `discovery`, `hooks`, `waker` |
| `cli.py` | the argument parser |

Agent-facing wording is edited in `prompts.py`, and timings are tuned in `timing.py`. To add a subcommand, write its `cmd_*` in `commands/` and register it in `cli.py`.

## Failure modes

**A new agent nobody addresses.** `spawn` announces arrivals in the transcript (`system` message naming the id), so the others learn to address it. If that message is missing, the spawn did not complete.

**An agent reopening a settled question.** It joined without the history, or `--context` never said the question was closed. Check `.partner/<id>/handoff.md`.

**An agent talking to itself.** `read` and `wait` filter out messages the reader sent (`m["from"] != who`), so an agent cannot trigger its own next round — including the session agent's background `wait`.

**A message nobody ever answered.** It was handed over and dropped — a killed `wait`, or a turn that ended without a reply. `wait` re-delivers anything addressed to an agent and unanswered after 45s, and `read` lists it under "still unanswered", so recovery no longer depends on another agent noticing. A message that keeps coming back marked *re-delivered* means the agent is receiving it and not replying; `pending --for <id>` shows what it owes.

**A Claude session agent silent while the human works in a tab.** It had no background `wait` armed, so nothing re-invoked it when a partner addressed `@all`. Its briefing arms one at the end of every turn, and the Stop hook blocks a Claude agent from ending a turn without one. Check `.partner/<id>/lastseen` — a fresh stamp means the background `wait` is running.

**The Stop hook never fires.** It needs `bash` and Python on `PATH`, and `hooks.json` is read once at session start — a session already open when the plugin was installed will not have it. Restart. `partner.py hook-stop < /dev/null` from the repo root should print `{}` and exit 0; `hook-prompt` should print nothing and exit 0. In Codex, confirm the plugin hooks are trusted before testing the host events.

**Advisors only ever reply to the baton holder.** They should also debate each other — `send --to p3`, not just `--to @all` — and hand the holder a joint view. If the transcript is all spokes to one hub, the briefing's "you advise, and you discuss" step is being skipped; `send` one of them a direct question to seed it.

**The baton drifting away from the human.** An agent claimed after being asked by another agent rather than by the human. Its briefing forbids this; check the transcript for the `system` message naming who claimed and when.

**An agent that does not answer.** Ending the turn is normal now; the waker types a wake-up once a message leaves it owing a reply. If nothing happens, its tab cannot be typed into (`idle` says so) or its CLI exited. `nudge --id <id>` types a wake-up by hand; `restart --id <id>` relaunches it.

**A partner that claimed the baton the moment it started.** It read its own launch prompt as the human addressing it. The boot line disowns itself, the briefing names it, and `claim` refuses inside the launch grace window — if it still happened, `baton --to <id>` puts it back and the transcript names who claimed.

**Interleaved writes.** Appends are single `open(..., "a")` writes, atomic at these sizes on all three platforms. Two agents replying in the same instant produce two well-formed adjacent records, not a corrupted one.

**Runaway loops.** Two agents addressing each other with `@all` will keep going until one applies the two-exchange rule. When agents should debate each other directly, prefer explicit `--to <id>` addressing so the exchange has a clear owner.
