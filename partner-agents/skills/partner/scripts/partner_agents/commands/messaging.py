"""Commands agents use to talk: wait, send, read, and moving the baton."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from ..idle import mark_active
from ..presence import marker_live, read_marker, touch_seen, write_marker
from ..prompts import EXPLORE_BRIEF, OPENING
from ..state import baton_of, load_roster, me_id, save_roster, state_dir
from ..timing import BACKGROUND_TOOL_TIMEOUT_MS, BACKGROUND_WAIT, CLAIM_GRACE, REDELIVER_WINDOW
from ..transcript import (
    FINAL_TAG, append_msg, baton_banner, commit_cursor, dropped_for, hop_limit_note,
    parse_msgs, pending_for, read_new, render, tail_msgs,
)
from ..util import NL, age_seconds, emit, nag_throttled, process_cmdline, unlink_quietly
from ..waker import ensure_waker, waker_alive
from ..waking import (
    adopt_own_tab, listen_mode, nudge_agent, silence_notice, silent_agents, wake_owed,
)


def listen_hint(roster: dict, who: str, listening: bool) -> str:
    """What this agent does to stay reachable, for the end of `read` and `wait`."""
    mode = listen_mode(roster, who)
    if mode == "background":
        if listening:
            return ("[a background `wait` is in flight for you -- do NOT start "
                    "another; one is what keeps the inbox undivided]")
        return (f"[no `wait` is in flight for you -- before this turn ends, arm "
                f"ONE background `wait --timeout {BACKGROUND_WAIT}` (tool timeout "
                f"{BACKGROUND_TOOL_TIMEOUT_MS}), or a message will not reach you]")
    if mode == "typed":
        return ("[you do not need `wait`: when you are done, end your turn -- the "
                "next message types a wake-up into your tab]")
    return ("[your tab cannot be typed into: once you end your turn, only the "
            "human can wake you, by typing in your tab]")


def cmd_wait(args) -> int:
    """Block until somebody addresses this agent.

    Run in the background by a Claude agent, whose harness re-invokes the model
    when it returns -- so it returns only for a message, and an idle agent costs
    nothing. Every other agent ends its turn instead and is woken by typing
    into its tab (see waking.py); a `wait` run there still works, it is just
    not needed.
    """
    sd = state_dir()
    roster = load_roster(sd)
    who = args.who or me_id(roster)
    deadline = time.time() + args.timeout
    # Whoever runs this is in its own tab right now; recording it is what lets a
    # message type it awake later.
    adopt_own_tab(sd, roster, who)
    ensure_waker(sd)             # typing wake-ups for agents that do not wait
    # Say what this command is doing *before* blocking, and flush it. A capped
    # wait that has printed nothing looks to the agent like a command that does
    # not work, which is how partners talk themselves out of listening.
    if not args.json:
        if listen_mode(roster, who) == "background":
            print(f"[listening as {who} for up to {args.timeout}s. Run this in "
                  f"the BACKGROUND and end your turn: it returns only for a "
                  f"message, and the task notification re-invokes you.]",
                  flush=True)
        else:
            print(f"[listening as {who} for up to {args.timeout}s. You do not "
                  f"need `wait`: end your turn instead, and the next message "
                  f"wakes you. Never run it in a loop.]", flush=True)
    # <id>/waiting says who is polling for this agent. Only that one consumes
    # messages; a second `wait` for the same agent idles instead, because two
    # of them split the inbox -- each message goes to whichever polls first, and
    # the one whose output nobody reads takes its message with it.
    #
    # So ownership is a token with a heartbeat, and a duplicate blocks quietly
    # rather than exiting: exiting immediately would return control to the agent,
    # which arms another wait, which exits immediately...
    token = f"{os.getpid()}-{time.time():.3f}"
    try:
        while True:
            mark = read_marker(sd, who)
            if mark.get("token") == token:
                owner = True
            elif marker_live(mark):
                owner = False            # somebody else is consuming; idle
            else:
                # Free, or the previous owner stopped proving it was alive.
                write_marker(sd, who, token, deadline)
                owner = True
            if not owner:
                if time.time() >= deadline:
                    emit(args, {"messages": [], "duplicate": True},
                         f"(another `wait` for {who} was already listening, so "
                         f"this one stayed out of the way -- keep exactly ONE "
                         f"in flight; a second splits the inbox and messages "
                         f"get read by whichever polls first)")
                    return 0
                time.sleep(args.poll)
                continue
            write_marker(sd, who, token, deadline)   # heartbeat
            touch_seen(sd, who)          # still here, still listening
            msgs, end = read_new(sd, who)
            if msgs:
                roster = load_roster(sd)
                if args.json:
                    print(json.dumps({"baton": baton_of(roster), "you": who,
                                      "messages": msgs}, indent=2))
                else:
                    print(baton_banner(roster, who) + NL * 2 + render(msgs)
                          + hop_limit_note(sd) + silence_notice(sd, roster, who))
                # Only now, once the messages are actually out: a wait killed
                # before this point re-delivers rather than losing them.
                sys.stdout.flush()
                commit_cursor(sd, who, end)
                return 0

            # Nothing new -- but the cursor only records what was handed over,
            # not what was answered. Anything addressed to this agent that it
            # never replied to comes back here, which is what stops a dropped
            # message from needing another agent to notice and ping.
            roster = load_roster(sd)
            dropped = dropped_for(sd, roster, who)
            if dropped and not nag_throttled(sd / who / ".redeliver",
                                              REDELIVER_WINDOW):
                if args.json:
                    print(json.dumps({"baton": baton_of(roster), "you": who,
                                      "messages": dropped, "redelivered": True},
                                     indent=2))
                else:
                    print(baton_banner(roster, who) + NL * 2 + render(dropped)
                          + NL * 2
                          + "[re-delivered: addressed to you, still unanswered. "
                            "You were given this before and did not reply -- "
                            "answer it with `send` (even just to disagree, or "
                            "`--final` to say it is settled). Replying is what "
                            "clears it.]"
                          + silence_notice(sd, roster, who))
                sys.stdout.flush()
                commit_cursor(sd, who, end)
                return 0

            if time.time() >= deadline:
                emit(args, {"messages": []},
                     f"(nothing addressed to {who} in {args.timeout}s) "
                     + listen_hint(roster, who, listening=False))
                return 0
            time.sleep(args.poll)
    finally:
        # Only the owner clears the marker. Clearing somebody else's is what
        # made a live listener look dead and started the whole cycle.
        try:
            if read_marker(sd, who).get("token") == token:
                (sd / who / "waiting").unlink()
        except OSError:
            pass


def cmd_claim(args) -> int:
    """Take the write baton, because the human just gave you an instruction.

    The baton is meant to follow the human's attention: whoever they are
    talking to is the one who edits. Nothing outside the tabs can observe that
    -- only the agent being typed at knows it is being typed at -- so claiming
    is how that fact gets into shared state where the others can see it.
    """
    sd = state_dir()
    roster = load_roster(sd)
    who = args.who or me_id(roster)
    if who not in roster["partners"]:
        known = ", ".join(roster["partners"]) or "none"
        emit(args, {"error": "unknown"}, f"no agent named {who}; known: {known}")
        return 1
    prev = baton_of(roster)
    if prev == who:
        touch_seen(sd, who)
        mark_active(sd)
        emit(args, {"baton": who, "changed": False},
             f"baton: already yours ({who}) -- go ahead")
        return 0

    # A claim from an agent that has just been launched and has never spoken is
    # almost always its own launch message being read as a human instruction:
    # the prompt lands where the human's typing lands, and the briefing says a
    # human addressing you means claim. It is intermittent -- a judgement call
    # each model makes differently -- and the cost is silent, so the guard is
    # here in shared state rather than only in the briefing. The human really
    # can address a partner seconds after it starts, so this refuses once and
    # says how to proceed rather than deciding it knows better.
    if not args.force:
        entry = roster["partners"][who]
        age = age_seconds(entry.get("started"))
        spoke = any(m["from"] == who for m in tail_msgs(sd, 200))
        if age is not None and age < CLAIM_GRACE and not spoke:
            touch_seen(sd, who)
            emit(args, {"baton": prev, "changed": False, "refused": "launch"},
                 f"claim refused: you started {int(age)}s ago and have not "
                 f"spoken yet, so this is almost certainly your own launch "
                 f"message rather than the human. The baton stays with {prev}. "
                 f"Take part as an advisor. If the human "
                 f"really did just type an instruction to you, run "
                 f"`claim --force`.")
            return 0
    touch_seen(sd, who)
    mark_active(sd)
    roster["baton"] = who
    save_roster(sd, roster)
    append_msg(sd, "system", "@all",
               f"**{who}** was given an instruction directly by the human and has "
               f"taken the write baton from **{prev}**. {who} edits files from now "
               f"on; everyone else advises until the human turns to them. "
               f"**{prev}**: you are an advisor again -- answer what you are "
               f"asked.", who)
    emit(args, {"baton": who, "previous": prev, "changed": True},
         f"baton: {prev} -> {who} (you may edit now)")
    return 0


def cmd_unwait(args) -> int:
    """Stop this agent's own `wait`: the one process its marker names.

    Agents used to do this by killing every process whose command line matched
    *partner.py*wait* -- which also matched partner CLIs, whose launch prompt
    said the same words, and took them down. The marker records the pid, so
    nothing needs matching; and the pid is checked to still be a partner.py
    wait before anything is killed, since pids are reused.
    """
    sd = state_dir()
    roster = load_roster(sd)
    who = me_id(roster)
    mark = read_marker(sd, who)
    pid = mark.get("pid")
    if not pid:
        emit(args, {"stopped": None}, f"no `wait` is in flight for {who}")
        return 0
    cmdline = process_cmdline(pid)
    # A Python interpreter running partner.py with `wait` as its subcommand --
    # not merely a command line that mentions both, which is what went wrong.
    ours = bool(re.match(r'\s*"?[^"]*python[^"\s]*"?\s', cmdline, re.I)
                and re.search(r'partner\.py"?\s+wait(\s|$)', cmdline, re.I))
    if not ours:
        unlink_quietly(sd / who / "waiting")     # the pid is gone or reused
        emit(args, {"stopped": None},
             f"the `wait` recorded for {who} (pid {pid}) is not running any more"
             f" -- nothing killed")
        return 0
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError as exc:
        emit(args, {"stopped": None, "error": str(exc)},
             f"could not stop pid {pid}: {exc}")
        return 1
    if read_marker(sd, who).get("pid") == pid:
        unlink_quietly(sd / who / "waiting")
    emit(args, {"stopped": pid}, f"stopped {who}'s `wait` (pid {pid})")
    return 0


def cmd_nudge(args) -> int:
    """Wake agents that stopped looping, by typing into their tabs."""
    sd = state_dir()
    roster = load_roster(sd)
    me = me_id(roster)
    touch_seen(sd, me)
    if not (args.id or args.all):
        # Everyone idle who owes a reply -- the same pass the waker makes.
        res = wake_owed(sd, roster, why=args.text or
                        "a message in the transcript is waiting for your reply.")
        lines = ([f"nudged {', '.join(res['woke'])}"] if res["woke"] else []) \
            + res["failed"]
        emit(args, {"nudged": res["woke"], "failed": res["failed"]},
             NL.join(lines) or "nobody idle owes a reply -- nothing to wake")
        return 0
    targets = [args.id] if args.id else [p for p in roster["partners"] if p != me]
    why = args.text or "another agent asked you to check the transcript."
    results = [nudge_agent(sd, roster, p, why, throttle=0) for p in targets]
    ok = [r["id"] for r in results if r["nudged"]]
    bad = [f"{r['id']}: {r['why']}" for r in results
           if not r["nudged"] and not r.get("throttled")]
    lines = ([f"nudged {', '.join(ok)}"] if ok else []) + bad
    emit(args, {"nudged": ok, "failed": bad}, NL.join(lines) or "nothing to do")
    return 0


def cmd_send(args) -> int:
    sd = state_dir()
    roster = load_roster(sd)
    args.sender = args.sender or me_id(roster)
    touch_seen(sd, args.sender)
    body = args.text
    if args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    if not body:
        body = sys.stdin.read()
    if getattr(args, "final", False) and not body.lstrip().startswith(FINAL_TAG):
        body = f"{FINAL_TAG} {body.strip()}"

    mark_active(sd)
    adopt_own_tab(sd, roster, args.sender)
    chat = sd / "chat.md"
    before = chat.stat().st_size if chat.exists() else 0
    append_msg(sd, args.sender, args.to, body, baton_of(roster))

    # Every agent that owes a reply now and has ended its turn is woken: a
    # Claude agent by its own background `wait` returning, any other by a line
    # typed into its tab. The waker does the typing within a second; without a
    # waker, this process does it, through the same idempotent pass.
    addressed = None if args.to in ("@all", "all") else [args.to]
    woke, deaf = [], []
    if not args.no_nudge:
        ensure_waker(sd)
        if not waker_alive(sd):
            # Never the sender: it is mid-command, and an agent without the
            # plugin hooks has no turn marker to say so.
            targets = addressed or [p for p in roster["partners"] if p != args.sender]
            res = wake_owed(sd, roster, targets,
                            f"{args.sender} sent you a message you have not read.")
            woke, deaf = res["woke"], res["failed"]
        deaf += silent_agents(sd, roster, args.sender, addressed)
    tail = ""
    if woke:
        tail += f"{NL}woke {', '.join(woke)} by typing into their tabs"
    if deaf:
        tail += (f"{NL}cannot be reached until the human types in their tab: "
                 f"{', '.join(deaf)}")

    if not args.wait:
        emit(args, {"sent": True, "to": args.to, "woke": woke, "deaf": deaf},
             f"sent to {args.to}{tail}")
        return 0

    running = [k for k, v in roster["partners"].items()
               if v.get("status") == "running" and k != args.sender
               and k != me_id(roster)]
    expect = args.expect or (len(running) if args.to in ("@all", "all") else 1)
    deadline, seen = time.time() + args.wait, {}
    while time.time() < deadline:
        raw = chat.read_bytes()[before:].decode("utf-8", errors="replace")
        for m in parse_msgs(raw):
            if m["from"] != args.sender and m["to"] in (args.sender, "@all", "all"):
                seen[m["from"]] = m
        if len(seen) >= max(expect, 1):
            break
        time.sleep(1.5)

    replies = list(seen.values())
    text = render(replies) or (
        f"(no reply within {args.wait}s -- check the agent tabs; "
        f"`idle` shows what each agent is doing)")
    silent = silent_agents(sd, load_roster(sd), args.sender, addressed)
    if silent:
        text += (f"{NL * 2}[{', '.join(silent)} cannot be reached: no `wait` in "
                 f"flight and no tab to type into. The human must type there.]")
    emit(args, {"replies": replies, "woke": woke, "deaf": deaf,
                "silent": silent}, text + tail)
    return 0


def cmd_kickoff(args) -> int:
    """Open the session: put the human's instruction, or an orientation brief,
    in front of everyone in one message."""
    sd = state_dir()
    roster = load_roster(sd)
    sender = args.sender or me_id(roster)
    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    body = (OPENING.format(sender=sender, text=text.strip())
            if text and text.strip() else EXPLORE_BRIEF)
    ns = argparse.Namespace(
        json=getattr(args, "json", False), quiet=False, sender=sender,
        to="@all", text=body, file=None, wait=args.wait, expect=args.expect,
        no_nudge=False, final=False)
    return cmd_send(ns)


def cmd_read(args) -> int:
    sd = state_dir()
    roster = load_roster(sd)
    who = args.who or me_id(roster)
    touch_seen(sd, who)
    msgs, end = read_new(sd, who)
    # Anything addressed to this agent that it never answered, whether or not
    # the cursor has already passed it. `read` is where an agent checks what it
    # owes, so a dropped message has to show up here too.
    seen_ts = {m["ts"] for m in msgs}
    dropped = [m for m in dropped_for(sd, roster, who, min_age=0)
               if m["ts"] not in seen_ts]
    # Whether a wait is already in flight for *this* agent, which is the one
    # thing a Claude agent needs to know before arming another: a second wait
    # splits the inbox.
    adopt_own_tab(sd, roster, who)
    mine = read_marker(sd, who)
    listening = NL * 2 + listen_hint(roster, who, marker_live(mine))
    if args.json:
        print(json.dumps({"baton": baton_of(roster), "you": who,
                          "messages": msgs, "unanswered": dropped,
                          "listening": marker_live(mine),
                          "listen_mode": listen_mode(roster, who)}, indent=2))
    else:
        body = render(msgs) or "(nothing new)"
        if dropped:
            body += (f"{NL * 2}--- still unanswered, from earlier ---{NL * 2}"
                     f"{render(dropped)}{NL * 2}"
                     f"[you were given these before and have not replied. "
                     f"Answering with `send` is what clears them.]")
        print(baton_banner(roster, who) + NL * 2 + body + hop_limit_note(sd)
              + listening + silence_notice(sd, roster, who))
    sys.stdout.flush()
    if not args.peek:
        commit_cursor(sd, who, end)
    return 0


def cmd_baton(args) -> int:
    sd = state_dir()
    roster = load_roster(sd)
    if not args.to:
        holder = baton_of(roster)
        emit(args, {"baton": holder}, f"baton: {holder}")
        return 0
    if args.to not in roster["partners"]:
        known = ", ".join(roster["partners"]) or "none"
        emit(args, {"error": "unknown"}, f"no agent named {args.to}; known: {known}")
        return 1
    prev = baton_of(roster)
    mark_active(sd)
    roster["baton"] = args.to
    save_roster(sd, roster)
    append_msg(sd, "system", "@all",
               f"Write baton moved from **{prev}** to **{args.to}**. "
               f"{args.to} edits files from now on; everyone else advises. "
               f"**{prev}**: you are an advisor again -- answer what you are "
               f"asked.", args.to)
    emit(args, {"baton": args.to, "previous": prev}, f"baton: {prev} -> {args.to}")
    return 0


def cmd_pending(args) -> int:
    """What this agent still owes a reply to. Read-only; the Stop hook runs the
    same check to keep an agent from going silent on the group."""
    sd = state_dir()
    roster = load_roster(sd)
    who = args.who or me_id(roster)
    pend = pending_for(sd, roster, who)
    if args.json:
        print(json.dumps({"pending": bool(pend), "you": who,
                          "baton": baton_of(roster), "messages": pend}, indent=2))
    else:
        print(f"{len(pend)} awaiting your reply:{NL * 2}{render(pend)}" if pend
              else "nothing pending -- you have answered everything addressed to you")
    return 0
