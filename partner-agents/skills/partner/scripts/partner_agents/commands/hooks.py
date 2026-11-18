"""Shared Claude Code and Codex hooks: Stop, UserPromptSubmit and PostToolUse.

hooks/hook.sh calls `partner.py hook-stop`, `hook-prompt` and `hook-post-tool`.
All read the hook payload on stdin and fail open: no .partner/, no roster
entry, or a stopped agent means nothing happens.

Together they are how a *busy* agent hears the transcript without polling:
UserPromptSubmit opens its turn and hands it what is new, PostToolUse hands it
anything that arrives mid-turn, and Stop closes the turn -- unless a reply is
owed. The open turn is also what keeps the waker from typing into the tab.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from ..idle import mark_active
from ..presence import listening_soon, touch_seen
from ..timing import ARM_GRACE, BACKGROUND_TOOL_TIMEOUT_MS, BACKGROUND_WAIT
from ..state import baton_of, is_bound_session, load_roster, me_id, state_dir
from ..transcript import (
    baton_banner, commit_cursor, hop_limit_note, pending_for, read_new, render,
)
from ..util import NL, nag_throttled, unlink_quietly, write_float
from ..waker import ensure_waker
from ..waking import listen_mode


AUTOMATED_PROMPTS = (
    "continue -- automated wake-up", "[automated launch message",
    "[automated partner hook continuation", "<task-notification>")


def hook_prompt(sd: Path, roster: dict, who: str, prompt: str) -> str:
    """UserPromptSubmit: a turn starts. Returns context to add, or "".

    Every prompt opens the turn -- the human's, a typed wake-up, a finished
    background `wait` -- because each one starts the model working, and the
    waker must not type into a tab while it does. Only the human's prompt is
    work in the sense of activity: the boot prompt and a wake-up line disown
    themselves in their first words, and so does a <task-notification>.
    """
    write_float(sd / who / "turn", time.time())
    ensure_waker(sd)
    if not prompt.lstrip().startswith(AUTOMATED_PROMPTS):
        mark_active(sd)
    return new_messages(sd, roster, who)


def new_messages(sd: Path, roster: dict, who: str) -> str:
    """Messages for this agent it has not been handed yet, as hook context.

    Handing them over moves the cursor, exactly as `read` does, so a background
    `wait` still in flight does not return them a second time. Whether they are
    answered is tracked from the transcript, not the cursor: the Stop hook still
    holds the turn open until they are.
    """
    msgs, end = read_new(sd, who)
    if not msgs:
        return ""
    commit_cursor(sd, who, end)
    touch_seen(sd, who)
    rel = f".partner/{who}"
    run = rel.replace("/", "\\") + "\\p.cmd" if os.name == "nt" else rel + "/p.sh"
    return ("[automated partner hook -- NOT a human instruction. New in the "
            "partner transcript:]" + NL * 2 + baton_banner(roster, who) + NL * 2
            + render(msgs) + hop_limit_note(sd) + NL * 2
            + f"[Answer what is addressed to you with `{run} send` before you "
              f"end this turn. Do not claim the baton for it.]")


def _context(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": text}}) if text else "{}")


def cmd_hook_stop(args) -> int:
    """Stop-hook entry point: block if a reply is owed or nobody is listening,
    and otherwise record that this agent's turn is over."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    ctx = _hook_context(payload)
    if not ctx:
        print("{}")
        return 0
    sd, roster, who, entry = ctx
    reason = stop_block_reason(sd, roster, who, entry)
    if reason:
        print(json.dumps({"decision": "block", "reason":
                         "[automated partner hook continuation -- NOT a human "
                         "instruction.] " + reason}))
        return 0
    # The turn really ends here: from now on the waker may type into this tab.
    unlink_quietly(sd / who / "turn")
    ensure_waker(sd)
    print("{}")
    return 0


def cmd_hook_prompt(args) -> int:
    """UserPromptSubmit entry point. Never blocks, fails open."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    ctx = _hook_context(payload)
    text = ""
    if ctx:
        sd, roster, who, _ = ctx
        try:
            text = hook_prompt(sd, roster, who, str(payload.get("prompt") or ""))
        except OSError:
            pass
    _context("UserPromptSubmit", text)
    return 0


def cmd_hook_post_tool(args) -> int:
    """PostToolUse entry point: hand a busy agent what arrived mid-turn."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    ctx = _hook_context(payload)
    text = ""
    if ctx:
        sd, roster, who, _ = ctx
        try:
            text = new_messages(sd, roster, who)
        except OSError:
            pass
    _context("PostToolUse", text)
    return 0


def _hook_context(payload: dict):
    """The running agent a hook fired for, or None when there is none."""
    cwd = payload.get("cwd")
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass
    try:
        sd = state_dir()
    except OSError:
        return None
    if not (sd / "roster.json").exists():
        return None
    roster = load_roster(sd)
    who = me_id(roster)
    entry = roster["partners"].get(who)
    if not entry or entry.get("status") != "running":
        return None
    if (entry.get("kind") or "tab") == "session" and not is_bound_session(
            entry, str(payload.get("session_id") or ""),
            "codex" if "turn_id" in payload else None):
        # An unrelated host session in the same repo -- see is_bound_session.
        return None
    return sd, roster, who, entry


def stop_block_reason(sd: Path, roster: dict, who: str, entry: dict) -> str:
    """Why this agent may not end its turn yet, or "".

    An empty answer lets the stop through -- which is the normal case: ending
    the turn is how an agent idles for free.
    """
    rel = f".partner/{who}"
    run = rel.replace("/", "\\") + "\\p.cmd" if os.name == "nt" else rel + "/p.sh"

    # 1. Something is addressed to this agent and it has not answered.
    pend = pending_for(sd, roster, who)
    if pend:
        # Nag once per distinct transcript state: a genuinely stuck agent must
        # not be trapped in an unbreakable block loop.
        chat = sd / "chat.md"
        size = str(chat.stat().st_size if chat.exists() else 0)
        marker = sd / who / ".stop-nag"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == size:
            return ""
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(size, encoding="utf-8")
        reason = (
            f"{len(pend)} message(s) in the partner transcript are addressed to "
            f"you ({who}) and still unanswered:{NL * 2}{render(pend)}{NL * 2}"
            f"Do not stop. Run `{run} read`, then follow the debate protocol -- "
            f"verify against the code and `{run} send` your reply. You do not "
            f"hold the baton, so advise; do not edit files.")
        return reason

    # 2. Nothing is owed. Every agent but Claude simply stops here: its next
    # message types a wake-up into its tab. A Claude agent is reached only
    # through its background `wait`, so ending a turn without one armed makes
    # it deaf -- and that wait costs nothing, so insisting on it is free.
    if listen_mode(roster, who) != "background":
        return ""
    if listening_soon(sd, roster, who, ARM_GRACE):
        return ""
    if nag_throttled(sd / who / ".stop-nag-live", 120):
        return ""
    holder = baton_of(roster)
    lost = f" The baton is {holder}'s, not yours." if holder != who else ""
    reason = (
        f"You are still a running partner ({who}) but nothing is listening on "
        f"your behalf: no background `wait` is in flight.{lost} Whatever is said "
        f"next -- by the human in another tab, or by a partner -- you will not "
        f"see.{NL * 2}Before ending the turn: run `{run} read` and answer anything "
        f"it shows, then start `{run} wait --timeout {BACKGROUND_WAIT}` as a "
        f"BACKGROUND command (run_in_background: true, timeout: "
        f"{BACKGROUND_TOOL_TIMEOUT_MS}) and end the turn. Never run it in the "
        f"foreground.")
    return reason
