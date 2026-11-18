"""Waking an agent that stopped listening, by typing into its tab.

A tab agent participates by blocking on `wait`. When it ends its turn instead
-- because its shell tool cut a wait short, because a discussion concluded,
or because the baton moved away -- nothing re-invokes it and the next thing
said to it lands in a transcript nobody is reading.

Claude Code and Codex agents can have a plugin Stop hook to catch that. Any other CLI may or may
not have an equivalent, and this plugin cannot require one -- so the recovery
comes from outside the agent entirely: type into its tab, exactly as the human
would. That works whatever is running in it. Every terminal with a control CLI
can do it, and `spawn` records which one owns each tab.

Nothing polls. An agent with nothing owed ends its turn and costs nothing until
input arrives: the human typing in its tab, or a message in chat.md that it
owes a reply to. How that message reaches it depends on its listen mode:

  background  Claude: one background `wait` that returns only for a message;
              the harness's task notification re-invokes the model.
  typed       any other CLI in a tab that can be typed into: the waker types a
              wake-up line once the agent's turn has ended.
  manual      neither: only the human brings it back, by typing in its tab.

An agent that is mid-turn is never typed into. The hooks reach it instead: the
PostToolUse hook shows new messages at its next tool call, and the Stop hook
does not let the turn end while a reply is owed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from .presence import is_listening, last_seen, turn_open
from .state import baton_of, is_bound_session, save_roster
from .terminals.detect import (
    load_terminals, own_tab, terminal_available, terminal_bin, terminal_order,
)
from .terminals.tabs import term_argv
from .timing import (
    BACKGROUND_TOOL_TIMEOUT_MS, BACKGROUND_WAIT, NUDGE_WINDOW, UNANSWERED_NUDGES,
    WAKE_REQUEST_TTL,
)
from .transcript import append_msg, pending_for
from .util import NL, age_seconds, dig_json, nag_throttled
from .waker import ensure_waker, request_wake, waker_alive


def silence_notice(sd: Path, roster: dict, who: str, wake: bool = False) -> str:
    """One line about agents nothing is listening for, optionally waking them.

    Delivered through `wait` and `read` because that is where every agent looks
    constantly -- a partner that has gone deaf is otherwise invisible until
    somebody notices their question was never answered.
    """
    silent = silent_agents(sd, roster, who)
    if not silent:
        return ""
    if wake:
        res = [nudge_agent(sd, roster, p,
                           "you stopped listening -- nobody can reach you.")
               for p in silent]
        woke = [r["id"] for r in res if r["nudged"]]
        gone = [r["why"] for r in res if r.get("gone")]
        out = "".join(f"{NL * 2}[{g}]" for g in gone)
        if woke:
            out += (f"{NL * 2}[{', '.join(woke)} could not be reached; a wake-up "
                    f"was typed into their tabs.]")
        if out:
            return out
    verb = "is" if len(silent) == 1 else "are"
    return (f"{NL * 2}[{', '.join(silent)} {verb} not reachable -- no `wait` is "
            f"in flight and the tab cannot be typed into. The human must type "
            f"`continue` there, or `restart --id <id>` brings it back.]")


def find_handle(spec: dict, title: str) -> str:
    """Recover a tab's id from the terminal itself, by title.

    Needed for an agent spawned before its handle was recorded, and for one
    whose tab was recreated. `list` in the spec says how to ask -- without it
    there is nothing to ask, and the nudge falls back to the manual path.
    """
    tmpl, dig = spec.get("list") or "", spec.get("list_handle") or ""
    binary = terminal_bin(spec)
    if not (tmpl and dig and binary):
        return ""
    argv = term_argv(tmpl, bin=binary, title=title)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        if r.returncode != 0:
            return ""
        doc = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return ""
    # "result.terminals[].handle@title" -- a list to walk, the field to return,
    # and the field to match the title against.
    path, _, fields = dig.partition("[].")
    field, _, key = fields.partition("@")
    items = dig_json(doc, path, want=list) or []
    for it in items:
        if isinstance(it, dict) and str(it.get(key or "title", "")) == title:
            return str(it.get(field, ""))
    return ""


_TYPABLE: dict[str, bool] = {}


def adopt_own_tab(sd: Path, roster: dict, who: str) -> None:
    """Record the tab the calling agent runs in, if its terminal says.

    `spawn` records this for every tab it opens. The agent that ran `init` was
    never spawned, so without this it had no tab on record and could not be
    woken by typing. Called from that agent's own commands, so the handle is
    always the terminal it is in right now -- a resumed session in a new tab
    corrects the record the first time it waits.
    """
    entry = (roster.get("partners") or {}).get(who)
    tab = own_tab()
    if not entry or not tab:
        return
    # Only the agent itself may say where it is. `wait --who`, or a Claude
    # session that merely shares the repo, would otherwise record its own tab
    # here, and every wake-up for this agent would be typed into it.
    if (entry.get("kind") or "tab") == "session":
        if not is_bound_session(entry):
            return
    elif os.environ.get("PARTNER_ID") != who:
        return
    if (entry.get("tab_kind"), entry.get("tab_handle")) == (tab["kind"], tab["handle"]):
        return
    entry["tab_kind"], entry["tab_handle"] = tab["kind"], tab["handle"]
    _TYPABLE.pop(who, None)
    save_roster(sd, roster)


def typable(roster: dict, pid: str) -> bool:
    """Can a wake-up be typed into this agent's tab from outside?

    Cached per process: resolving a terminal may probe it, and the waker asks
    this for every agent on every check.
    """
    if pid not in _TYPABLE:
        entry = (roster.get("partners") or {}).get(pid) or {}
        _TYPABLE[pid] = nudge_command(entry, pid, "wake") is not None
    return _TYPABLE[pid]


def listen_mode(roster: dict, pid: str) -> str:
    """How this agent hears a message while its turn is over -- see the module doc.

    Claude always listens through a background `wait`: its harness re-invokes
    the model when a background command returns, which no other CLI does, and
    that needs no typing into a tab -- so it is never typed into mid-thought.
    """
    entry = (roster.get("partners") or {}).get(pid) or {}
    if entry.get("provider") == "claude":
        return "background"
    return "typed" if typable(roster, pid) else "manual"


def reachable(sd: Path, roster: dict, pid: str) -> bool:
    """Will a message addressed to this agent reach it without the human?"""
    return (is_listening(sd, roster, pid) or turn_open(sd, pid)
            or typable(roster, pid))


def nudge_command(entry: dict, pid: str, text: str) -> list[str] | None:
    """The argv that types `text` plus Enter into this agent's tab.

    Which terminal owns the tab was recorded at spawn, and the terminal entry
    itself says how to type into one -- so a terminal the user described in
    their own file can be woken exactly like a built-in, and one that cannot be
    typed into says so by having no `send` rather than by being special-cased
    here.
    """
    title = entry.get("tab_title") or f"partner:{pid}"
    specs = load_terminals(discover=True)
    kind = entry.get("tab_kind") or ""
    spec = specs.get(kind)
    if not spec and (entry.get("kind") or "tab") == "session":
        # The session agent's tab was never opened by us, so it has no title to
        # look up by; guessing would type into somebody else's tab.
        return None
    if not spec:
        # No record of the terminal (an older roster, or a hand-started tab):
        # the one we are inside now is the best guess available.
        pick = next(((n, s) for n, s in terminal_order()
                     if s.get("send") and terminal_available(s)[0]), None)
        if not pick:
            return None
        kind, spec = pick
    if not spec.get("send"):
        return None
    binary = terminal_bin(spec)
    if not binary:
        return None
    handle = entry.get("tab_handle") or ""
    if "{handle}" in spec["send"] and not handle:
        handle = find_handle(spec, title)
        if not handle:
            return None
    return term_argv(spec["send"], bin=binary, handle=handle, title=title,
                     text=text, text_nl=text + NL)


def nudge_text(pid: str, why: str, mode: str = "typed") -> str:
    """What gets typed into the tab.

    It arrives in the agent's prompt exactly as if the human had typed it, so
    it has to say that it is not the human -- otherwise a woken agent follows
    the baton rule ("the human addressed me -> claim") and takes write
    permission away from whoever actually has it. It also has to be harmless
    when the CLI has already exited and a shell reads the line instead.
    """
    rel = f".partner/{pid}"
    run = rel.replace("/", "\\") + "\\p.cmd" if os.name == "nt" else rel + "/p.sh"
    after = (f"then arm one background `{run} wait --timeout {BACKGROUND_WAIT}` "
             f"(tool timeout {BACKGROUND_TOOL_TIMEOUT_MS}) and end your turn."
             if mode == "background" else
             "then end your turn -- the next message wakes you again.")
    return (f"continue -- automated wake-up from the partner transcript, NOT "
            f"the human: {why} Do NOT claim the baton; nobody has given you an "
            f"instruction. Run `{run} read`, answer what it shows with "
            f"`{run} send`, {after}")


def unanswered(sd: Path, pid: str) -> int:
    """Wake-ups typed into this agent's tab that it has not acted on since.

    Reset the moment it does anything -- any command stamps `lastseen` -- so
    this only grows while nothing in that tab is reading what gets typed.
    """
    try:
        st = json.loads((sd / pid / ".unanswered").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(st.get("count", 0)) if st.get("seen") == last_seen(sd, pid) else 0


def cli_gone(sd: Path, pid: str) -> bool:
    """Has this agent ignored so many wake-ups that its CLI must have exited?"""
    return unanswered(sd, pid) >= UNANSWERED_NUDGES


def _count_nudge(sd: Path, pid: str) -> None:
    try:
        (sd / pid / ".unanswered").write_text(json.dumps(
            {"count": unanswered(sd, pid) + 1, "seen": last_seen(sd, pid)}),
            encoding="utf-8")
    except OSError:
        pass


def restart_hint(sd: Path, pid: str) -> str:
    run = ".partner\\p.cmd" if os.name == "nt" else ".partner/p.sh"
    return (f"{pid} ignored {UNANSWERED_NUDGES} wake-ups typed into its tab, so "
            f"its CLI has exited (killed, crashed or quit) and a shell is reading "
            f"them. Wake-ups to it have stopped. Bring it back with `{run} "
            f"restart --id {pid}`.")


def nudge_agent(sd: Path, roster: dict, pid: str, why: str,
                throttle: int = NUDGE_WINDOW, relay: bool = True) -> dict:
    """Type a wake-up line into one agent's tab.

    Throttled through a marker in the target's own directory rather than the
    caller's, so three agents noticing the same silent partner in the same
    minute produce one nudge between them, not three.

    When this process cannot run the terminal's CLI -- a sandboxed agent -- the
    wake-up is handed to the session's waker instead (`relay`); the waker
    itself passes relay=False so a failure there is reported, not re-queued.
    """
    entry = (roster.get("partners") or {}).get(pid) or {}
    if not entry:
        return {"id": pid, "nudged": False, "why": "unknown agent"}
    if entry.get("status") != "running":
        return {"id": pid, "nudged": False, "why": "not running"}
    if relay and cli_gone(sd, pid):
        # Typing more would only feed a shell. Say so once, to everyone, so
        # whoever reads it can `restart` the agent instead of nudging again.
        if not nag_throttled(sd / pid / ".gone-notice", 10 ** 9):
            append_msg(sd, "system", "@all", restart_hint(sd, pid), baton_of(roster))
        return {"id": pid, "nudged": False, "gone": True,
                "why": restart_hint(sd, pid)}
    if throttle and nag_throttled(sd / pid / ".nudge", throttle):
        # Somebody already woke it inside the window: handled, not a failure.
        return {"id": pid, "nudged": False, "throttled": True,
                "why": "nudged moments ago"}
    argv = nudge_command(entry, pid, nudge_text(pid, why, listen_mode(roster, pid)))
    if not argv:
        manual = entry.get("runner") or f"{sd / pid}/run.sh"
        where = entry.get("tab") or "its window"
        return {"id": pid, "nudged": False,
                "why": f"{where} cannot be typed into from outside (`terminals` "
                       f"says which can) -- type \"continue\" in {pid}'s tab "
                       f"yourself, or restart it with: {manual}"}
    failure = _send(argv)
    if not failure:
        if relay:
            _count_nudge(sd, pid)
        return {"id": pid, "nudged": True, "why": why}
    if relay:
        request_wake(sd, pid, why)
        if waker_alive(sd):
            _count_nudge(sd, pid)
            return {"id": pid, "nudged": True, "why": why, "via": "waker"}
        ensure_waker(sd)
        return {"id": pid, "nudged": False,
                "why": f"{failure} -- this agent cannot drive the terminal (a "
                       f"sandbox?), and no waker is running to do it instead; "
                       f"the request waits {WAKE_REQUEST_TTL}s for one"}
    return {"id": pid, "nudged": False, "why": failure}


def _send(argv: list[str]) -> str:
    """Run a send command; "" on success, otherwise what went wrong."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return type(exc).__name__
    try:
        # Orca reports a refused call as ok:false in its JSON, not only an exit code.
        doc = json.loads(r.stdout)
        if isinstance(doc, dict) and doc.get("ok") is False:
            err = doc.get("error") or {}
            return str(err.get("message") or err.get("code") or "send refused")[:200]
    except ValueError:
        pass
    if r.returncode != 0:
        return (r.stderr or r.stdout or "").strip()[:200] or "send failed"
    return ""


def silent_agents(sd: Path, roster: dict, who: str,
                  targets: list[str] | None = None) -> list[str]:
    """Running agents that a message cannot reach without the human.

    An agent that ended its turn is not silent by itself any more -- that is
    how every agent idles. It is silent when nothing can bring it back: no
    `wait` in flight, no turn open for the hooks to reach, and a tab that
    cannot be typed into.
    """
    out = []
    for pid, entry in (roster.get("partners") or {}).items():
        if pid == who or entry.get("status") != "running":
            continue
        if targets is not None and pid not in targets:
            continue
        if reachable(sd, roster, pid):
            continue
        age = age_seconds(last_seen(sd, pid))
        # Give a newly spawned agent time to reach its first `wait` before
        # declaring it deaf -- it is still reading its briefing.
        if age is not None and age < 30:
            continue
        out.append(pid)
    return out


def _wake_state(sd: Path, pid: str) -> dict:
    try:
        return json.loads((sd / pid / ".woke").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wake_owed(sd: Path, roster: dict, targets: list[str] | None = None,
              why: str = "a message in the transcript is waiting for your reply.",
              relay: bool = True) -> dict:
    """Type a wake-up into every idle agent that owes a reply. The one path.

    Run by the waker on every change to chat.md, and by `send` itself when no
    waker is alive. Idempotent through <id>/.woke, which records the newest
    owed message the agent was woken for: it is woken again for a newer
    message, or after NUDGE_WINDOW if it never acted on the last wake-up --
    never twice for the same message it already reacted to.
    """
    woke, failed = [], []
    now = time.time()
    for pid, entry in (roster.get("partners") or {}).items():
        if entry.get("status") != "running":
            continue
        if targets is not None and pid not in targets:
            continue
        owed = pending_for(sd, roster, pid, holder_too=True)
        if not owed:
            continue
        if is_listening(sd, roster, pid) or turn_open(sd, pid):
            continue             # a wait or the hooks deliver it
        age = age_seconds(entry.get("started"))
        if age is not None and age < 30:
            continue             # still booting; typing into it would garble that
        if not typable(roster, pid):
            continue             # manual: reported by silent_agents instead
        # Timestamps have one-second resolution, so the newest owed message is
        # told apart by its content and the size of the backlog as well.
        last = owed[-1]
        newest = hashlib.sha1(f"{len(owed)}|{last['ts']}|{last['from']}|"
                              f"{last['body']}".encode("utf-8")).hexdigest()[:16]
        st = _wake_state(sd, pid)
        seen = last_seen(sd, pid)
        if st.get("for") == newest and (
                st.get("seen") != seen or now - float(st.get("at") or 0) < NUDGE_WINDOW):
            continue             # woken for this already: it reacted, or give it time
        r = nudge_agent(sd, roster, pid, why, throttle=0, relay=relay)
        try:
            (sd / pid).mkdir(parents=True, exist_ok=True)
            (sd / pid / ".woke").write_text(json.dumps(
                {"for": newest, "at": now, "seen": last_seen(sd, pid)}),
                encoding="utf-8")
        except OSError:
            pass
        if r["nudged"]:
            woke.append(pid)
        elif not r.get("throttled"):
            failed.append(f"{pid} ({r['why']})")
    return {"woke": woke, "failed": failed}
