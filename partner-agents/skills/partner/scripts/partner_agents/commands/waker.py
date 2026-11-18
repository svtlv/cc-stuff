"""The waker: one detached process per session that types wake-ups for others.

It watches chat.md, and whenever a message leaves an idle agent owing a reply,
it types a wake-up into that agent's tab (waking.wake_owed). That is what lets
every agent end its turn instead of polling: the waker polls a file, which
costs nothing, so no model has to. It also performs wake-ups that a sandboxed
agent could not type itself -- see partner_agents/waker.py.

`partner.py waker` is started by `ensure_waker`, never by hand; it exits on its
own once the session is over.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ..idle import last_activity
from ..presence import is_listening
from ..state import load_roster, state_dir
from ..terminals.detect import load_terminals, terminal_bin
from ..terminals.tabs import term_argv
from ..timing import WAKE_RECHECK, WAKE_REQUEST_TTL, WAKER_IDLE_EXIT, WAKER_POLL
from ..waker import waker_alive
from .. import waking
from ..waking import nudge_agent, wake_owed


def can_drive(roster: dict) -> bool:
    """Can this process run the CLI of every terminal the agents' tabs are in?

    Asked by listing tabs, the one harmless command a terminal entry offers. A
    process in a sandbox fails exactly here -- which is the point: it must not
    hold the job and then fail every wake-up it was handed.
    """
    specs = load_terminals(discover=True)
    kinds = {e.get("tab_kind") for e in roster.get("partners", {}).values()
             if e.get("tab_kind")}
    for kind in kinds:
        spec = specs.get(kind) or {}
        binary = terminal_bin(spec)
        if not spec.get("send") or not binary:
            return False
        argv = term_argv(spec.get("list") or "", bin=binary)
        if not argv:
            continue             # no harmless command to ask with; try the job
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0:
            return False
        try:
            # Orca answers a sandboxed caller with ok:false rather than an exit code.
            if json.loads(r.stdout).get("ok") is False:
                return False
        except (ValueError, AttributeError):
            pass
    return True


def _beat(sd: Path, token: str) -> None:
    (sd / "waker.json").write_text(
        json.dumps({"token": token, "pid": os.getpid(), "beat": time.time()}),
        encoding="utf-8")


def _owner(sd: Path) -> str:
    try:
        return json.loads((sd / "waker.json").read_text(encoding="utf-8"))["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def cmd_waker(args) -> int:
    sd = state_dir()
    if waker_alive(sd) or not (sd / "roster.json").exists():
        return 0
    if not can_drive(load_roster(sd)):
        return 0
    token = f"{os.getpid()}-{time.time():.3f}"
    _beat(sd, token)
    time.sleep(WAKER_POLL)
    seen_chat, seen_roster, next_check = None, None, 0.0
    while _owner(sd) == token:   # two started at once: the last writer keeps it
        _beat(sd, token)
        try:
            roster = load_roster(sd)
            mtime = (sd / "roster.json").stat().st_mtime
        except (OSError, ValueError):
            return 0
        if mtime != seen_roster:
            # A tab recorded since the last look (adopt_own_tab, spawn) changes
            # whether that agent can be typed into; the answer is cached.
            waking._TYPABLE.clear()
            seen_roster = mtime
        running = [p for p, e in roster.get("partners", {}).items()
                   if e.get("status") == "running"]
        if not running or time.time() - last_activity(sd, roster) > WAKER_IDLE_EXIT:
            break
        for pid in running:
            req = sd / pid / "wake.json"
            try:
                data = json.loads(req.read_text(encoding="utf-8"))
                req.unlink()
            except (OSError, ValueError):
                continue
            if time.time() - float(data.get("at") or 0) > WAKE_REQUEST_TTL:
                continue
            if is_listening(sd, roster, pid):
                continue         # it came back on its own meanwhile
            nudge_agent(sd, roster, pid, str(data.get("why") or ""),
                        throttle=0, relay=False)
        # A new message, a turn that just ended, or a wake-up nobody acted on:
        # re-checked on every change to chat.md, and every WAKE_RECHECK anyway.
        try:
            st = (sd / "chat.md").stat()
            chat = (st.st_size, st.st_mtime)
        except OSError:
            chat = None
        if chat != seen_chat or time.time() >= next_check:
            seen_chat, next_check = chat, time.time() + WAKE_RECHECK
            wake_owed(sd, roster, relay=False)
        time.sleep(WAKER_POLL)
    if _owner(sd) == token:
        try:
            (sd / "waker.json").unlink()
        except OSError:
            pass
    return 0
