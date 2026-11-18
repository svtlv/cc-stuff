"""Who is working, who is idle, and when anybody last did real work.

There is no session-wide pause any more. Polling is what cost money: an idle
`wait` that times out hands control back to its model, which spends a turn
deciding to run `wait` again. Now no agent polls. Each one ends its turn the
moment it has nothing owed, and the next input wakes it -- see waking.py for
how, per listen mode. So "idle" is a per-agent fact to report, not a state the
group has to agree on before anybody may stop.
"""
from __future__ import annotations

import time
from pathlib import Path

from .presence import is_listening, turn_open
from .transcript import pending_for, tail_msgs
from .util import age_seconds, read_float, write_float
from .waking import cli_gone, listen_mode


def mark_active(sd: Path) -> None:
    """Record that real work happened. The waker exits after a long quiet spell."""
    write_float(sd / "activity", time.time())


def agent_state(sd: Path, roster: dict, pid: str) -> str:
    """One line on what this agent is doing and how the next message reaches it."""
    if cli_gone(sd, pid):
        return "CLI exited -- `restart --id " + pid + "` brings it back"
    owed = pending_for(sd, roster, pid)
    if turn_open(sd, pid):
        return "working (turn open)" + (
            f", owes a reply to {', '.join(sorted({m['from'] for m in owed}))}"
            if owed else "")
    if owed:
        return f"owes a reply to {', '.join(sorted({m['from'] for m in owed}))}"
    mode = listen_mode(roster, pid)
    if mode == "background":
        return ("idle, background `wait` armed -- costs nothing"
                if is_listening(sd, roster, pid) else
                "idle, NO background `wait` armed -- a message will not reach it")
    if mode == "typed":
        return "idle -- the next message types a wake-up into its tab"
    return "idle -- its tab cannot be typed into; only the human can wake it"


def last_activity(sd: Path, roster: dict) -> float:
    """When anybody last did something other than wait, as epoch seconds.

    `lastseen` cannot answer this -- a background `wait` refreshes it. Commands
    that are work stamp `activity`; the last real message and the newest agent's
    start cover transcripts written before that stamp existed.
    """
    stamps = [read_float(sd / "activity") or 0.0]
    now = time.time()
    for m in reversed(tail_msgs(sd, 100000)):
        if m["from"] != "system":
            age = age_seconds(m.get("ts"))
            if age is not None:
                stamps.append(now - age)
            break
    for entry in roster.get("partners", {}).values():
        age = age_seconds(entry.get("started"))
        if age is not None:
            stamps.append(now - age)
    return max(stamps)
