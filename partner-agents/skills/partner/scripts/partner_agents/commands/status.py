"""Commands that report on the session: list, state, idle."""
from __future__ import annotations

import json

from ..idle import agent_state
from ..presence import liveness
from ..sessions import read_sessions
from ..state import baton_of, load_roster, me_id, state_dir
from ..transcript import tail_msgs
from ..util import NL


def cmd_idle(args) -> int:
    """What every agent is doing, and how the next message reaches it.

    Nothing pauses any more: each agent ends its turn as soon as it owes
    nothing, so this is a report, not a threshold to tune.
    """
    sd = state_dir()
    roster = load_roster(sd)
    agents = {p: agent_state(sd, roster, p)
              for p, e in roster["partners"].items() if e.get("status") == "running"}
    if args.json:
        print(json.dumps({"agents": agents}, indent=2))
        return 0
    print("every agent ends its turn once it owes nothing; input wakes it")
    for p, why in agents.items():
        print(f"  {p:8} {why}")
    return 0


def cmd_state(args) -> int:
    """What is actually going on, so the caller can pick the right next move.

    Deliberately one call: deciding between adding a partner, resuming and
    starting fresh needs roster, liveness and history together, and asking for
    them separately invites deciding on half the picture.
    """
    sd = state_dir()
    roster = load_roster(sd)
    live_map = liveness(sd, roster, args.window)
    me = me_id(roster)
    others = {p: v for p, v in live_map.items() if p != me}
    live = [p for p, v in others.items() if v["state"] == "live"]
    idle = [p for p, v in others.items() if v["state"] == "idle"]
    stale = [p for p, v in others.items() if v["state"] == "stale"]
    past = read_sessions(sd)

    if live or idle:
        rec, why = "add", (f"{', '.join(live + idle)} active -- `/partner` archives this "
                           f"session and starts one partner fresh; `/partner add` "
                           f"keeps it and joins")
    elif stale:
        rec, why = "ask", (f"{', '.join(stale)} in the roster but not responding -- "
                           f"try `nudge` first (a tab that stopped looping wakes "
                           f"up); `resume` rebuilds them; `/partner` archives and "
                           f"starts fresh")
    elif past:
        rec, why = "ask", ("no partners running -- `/partner` starts fresh, "
                           "`resume --session <id>` brings an archived one back")
    else:
        rec, why = "new", "nothing running and no history -- start a new session"

    data = {"self": me, "baton": baton_of(roster), "live": live, "idle": idle,
            "stale": stale, "agents": live_map, "sessions": [
                {"id": m["id"], "label": m.get("label"),
                 "agents": list(m.get("agents", {})),
                 "messages": m.get("messages", 0)} for m in past],
            "messages": len(tail_msgs(sd, 100000)), "recommend": rec, "why": why}
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    print(f"you are {me}; baton held by {data['baton']}")
    for pid, v in live_map.items():
        mark = {"live": "live   ", "stale": "STALE  ", "stopped": "stopped",
                "idle": "idle   "}[v["state"]]
        print(f"  {pid:8} {mark} {v['why']}")
    if past:
        print(f"{NL}{len(past)} archived session(s):")
        for m in past[:5]:
            print(f"  {m['id']}   {m.get('label', '')}")
    print(f"{NL}suggested: {rec} -- {why}")
    return 0


def cmd_list(args) -> int:
    sd = state_dir()
    roster = load_roster(sd)
    if args.json:
        print(json.dumps(roster, indent=2))
        return 0
    holder = baton_of(roster)
    print(f"baton: {holder}   (only the baton holder edits files)")
    if not roster["partners"]:
        print("nobody here -- `partner.py providers` lists the agent CLIs on "
              "this machine, then: partner.py spawn --provider <one of them>")
        return 0
    me = me_id(roster)
    live_map = liveness(sd, roster)
    for pid, p in roster["partners"].items():
        mark = " <-- baton" if holder == pid else ""
        mark += "  (you)" if pid == me else ""
        st = live_map.get(pid, {}).get("state", "?")
        print(f"  {pid:8} {p['provider']:8} {p.get('model') or '-':22} "
              f"effort={p.get('effort') or '-':6} auto={p.get('auto') or '-':6}"
              f"[{st}] {p.get('tab') or ''}{mark}")
    return 0
