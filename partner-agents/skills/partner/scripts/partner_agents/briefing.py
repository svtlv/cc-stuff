"""The briefing every agent is launched with: seed.md and handoff.md.

`init` and `spawn` both brief through `write_briefing`, which is what keeps the
agent that starts a session a peer of the ones it starts rather than a
differently-described lead.
"""
from __future__ import annotations

from pathlib import Path

from .prompts import (
    EFFORT_HINT, LISTEN_BACKGROUND, LISTEN_TYPED, LOOP_SESSION, LOOP_TAB, SEED,
)
from .providers.resolve import provider_spec, wait_note
from .state import baton_of, repo_snapshot, write_wrappers
from .timing import BACKGROUND_TOOL_TIMEOUT_MS, BACKGROUND_WAIT, HOP_LIMIT
from .transcript import render, tail_msgs
from .util import NL


def boot_prompt(seed_f: Path, holder: str) -> str:
    """The one line a partner's CLI is started with.

    It arrives in the tab exactly where the human's own typing arrives, and a
    briefing that says "the human addressing you means claim the baton" makes
    that ambiguity expensive: the agent claims on its own launch message and
    quietly takes write permission from whoever actually has it. It happens
    intermittently, because reading it as an instruction is a judgement call.
    So the launch message disowns itself explicitly.
    """
    # This text is on the partner CLI's command line for its whole life, so it
    # must not contain "partner.py": an agent that once killed stray waits by
    # matching *partner.py*wait* took two Codex partners down with them.
    return (f"[automated launch message from the partner launcher -- NOT a human "
            f"instruction. Do not run `claim`; the write baton belongs to "
            f"{holder} until the human types something new in this tab.] "
            f"Read {seed_f} and follow it exactly. It explains who you are, "
            f"who you are working with, and how to talk to them. Begin now with "
            f"step 1 of the loop it describes.")


def build_seed(pid: str, roster: dict, root: Path, sd: Path,
               script: Path, has_handoff: bool, kind: str = "tab") -> str:
    others = [k for k in roster["partners"] if k != pid]
    peers = (f"Your partners are: {', '.join(others)}."
             if others else "You are the first one here; others may join later.")
    p = roster["partners"][pid]
    hint = EFFORT_HINT.get((p.get("effort") or "").lower())
    effort = ""
    if hint and provider_spec(p["provider"]).get("effort") == "prompt":
        effort = f"- {hint}{NL}"
    handoff = (f"{NL}Read {sd / pid / 'handoff.md'} first -- it says what was "
               f"decided before you joined, and what is still open.{NL}"
               if has_handoff else
               f"{NL}Nothing has been decided yet.{NL}")
    run = write_wrappers(sd, script, pid)
    # Only Claude's harness re-invokes the model when a background command
    # returns, so only Claude listens through a `wait` -- see listen_mode. The
    # briefing is written before the tab opens, so it goes by provider alone;
    # whether a non-Claude tab can really be typed into, `read` reports later.
    provider = p.get("provider") or ""
    claude = provider == "claude"
    host_name = {"codex": "Codex", "claude": "Claude Code"}.get(provider,
                                                               provider or "the host")
    if claude:
        note = wait_note(provider)
        listen_rule = LISTEN_BACKGROUND.format(
            run=run, wait_timeout=BACKGROUND_WAIT,
            tool_timeout=BACKGROUND_TOOL_TIMEOUT_MS,
            wait_note=f"{NL * 2}{note}" if note else "")
        listen_step = ("make sure one background `wait` is armed (`read` says "
                       "whether one is in flight; arm it if not), then end your "
                       "turn.")
        stop_wait = ", and while no background `wait` is armed for you"
    else:
        listen_rule = LISTEN_TYPED
        listen_step = "end your turn. The next message or the human wakes you."
        stop_wait = ""
    loop = (LOOP_SESSION if kind == "session" else LOOP_TAB).format(
        run=run, host_name=host_name, listen_rule=listen_rule,
        listen_step=listen_step, stop_wait=stop_wait, hop_limit=HOP_LIMIT)
    human_input = ("They type into your terminal tab."
                   if kind == "tab" else
                   f"They reach you through your own {host_name} harness, not a "
                   "terminal tab -- and they may instead be typing in another "
                   "agent's tab.")
    if kind == "tab" or not claude:
        start = "Now run step 1."
    else:
        start = ("Run step 1 (`read`) now, then step 4 -- arm the background "
                 "`wait` and end your turn. You will be re-invoked when the "
                 "human or a partner needs you.")
    return SEED.format(
        id=pid, cwd=root, peers=peers, chat=sd / "chat.md", run=run,
        baton=baton_of(roster),
        other=", ".join(others) or "the others",
        example_peer=others[0] if others else "p2",
        effort=effort, handoff=handoff, loop=loop,
        human_input=human_input, start=start)


def write_briefing(sd: Path, pid: str, roster: dict, root: Path, script: Path,
                   context: str | None, kind: str = "tab") -> Path:
    """Write one agent's handoff.md and seed.md.

    Shared by `init` and `spawn` on purpose: the moment the agent that starts
    the session is briefed by different code than the ones it spawns, the two
    drift and stop being peers.
    """
    pdir = sd / pid
    pdir.mkdir(parents=True, exist_ok=True)
    brief = []
    if context:
        ctx = Path(context)
        brief.append(ctx.read_text(encoding="utf-8", errors="replace")
                     if ctx.exists() else context)
    snap = repo_snapshot(root)
    if snap:
        brief.append("## Working tree at the moment you joined" + NL * 2 + snap)
    hist = tail_msgs(sd, 20)
    if hist:
        brief.append("## Debate you are joining, most recent last" + NL * 2
                     + render(hist))
    if brief:
        (pdir / "handoff.md").write_text((NL * 2).join(brief), encoding="utf-8")
    seed = pdir / "seed.md"
    seed.write_text(build_seed(pid, roster, root, sd, script, bool(brief), kind),
                    encoding="utf-8")
    return seed
