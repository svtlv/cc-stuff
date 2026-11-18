"""Every clock the partners run on, in one place.

Most of these are balanced against each other -- a wait must return before a
CLI's command cap, a heartbeat must land well inside the liveness window -- so
they are easier to reason about side by side than next to the code using them.
"""
from __future__ import annotations


WAIT_POLL = 3.0


NUDGE_WINDOW = 120         # seconds between nudges to the same silent agent

# How long after launch a claim is treated as the agent misreading its own boot
# prompt for a human instruction. Long enough to cover reading the briefing and
# reaching the first `wait`; short enough that a human typing at a new partner
# is rarely caught by it, and `claim --force` covers them when they are.
CLAIM_GRACE = 90

# A message addressed to an agent that it has not answered is re-delivered by
# `wait` once it is this old -- long enough that a reply already in progress is
# never mistaken for a dropped message, short enough that a real drop costs one
# cycle rather than a partner noticing the silence.
REDELIVER_AFTER = 45

REDELIVER_WINDOW = 90      # seconds between re-deliveries of the same backlog

# A polling `wait` refreshes its marker every cycle, so "is anyone listening"
# is answered by a heartbeat rather than by the marker merely existing: a wait
# that was killed stops refreshing, and another can take over within a cycle
# or two instead of waiting out a deadline that will never arrive.
MARKER_STALE = 15

LIVE_WINDOW = 360          # seconds since an agent last acted for it to count as live

# A Claude agent listens through one background `wait` and ends its turn. The
# harness re-invokes it only when that command returns, so the wait returns
# only for a message -- or just before the harness's own cap on a background
# command (7200000 ms) would kill it. That is one model turn per two idle hours
# instead of one per 90-second cycle of the old foreground loop.
BACKGROUND_WAIT = 7000
BACKGROUND_TOOL_TIMEOUT_MS = 7200000

# Advisors may owe each other a reply forever: every reply is a new debt. Once
# this many messages in a row came from agents that do not hold the baton, the
# chain stops creating debts -- nobody is woken or held by the Stop hook for it
# -- until the baton holder or the human speaks again.
HOP_LIMIT = 8

# How often the waker re-checks for owed replies when chat.md has not changed,
# so a wake-up the agent never acted on is retried after NUDGE_WINDOW.
WAKE_RECHECK = 15

# A turn marker older than this is from a turn that never reached Stop -- an
# interrupt -- and stops counting as work in progress.
TURN_STALE = 1800

# A background `wait` needs an interpreter start before it writes its marker. A
# Stop firing right after one was armed looks for that marker this long before
# calling the agent deaf -- rather than trusting a recent `lastseen`, which any
# `send` refreshes and which is how a deaf session agent used to slip through.
ARM_GRACE = 5

WAKER_POLL = 1.0           # how often the waker looks at chat.md and wake requests
WAKER_STALE = 10           # a waker heartbeat older than this is a dead waker
WAKER_RELAUNCH = 60        # at most one attempt per this many seconds to start one
WAKE_REQUEST_TTL = 120     # an older request describes a moment that has passed
WAKER_IDLE_EXIT = 6 * 3600 # nobody has done anything for this long: the waker exits

# Wake-ups typed into a tab whose agent never acts on them are going to a CLI
# that has exited -- killed, crashed, or quit -- and a shell is reading them.
# After this many in a row the tab is left alone and everyone is told to
# `restart` the agent instead.
UNANSWERED_NUDGES = 3
