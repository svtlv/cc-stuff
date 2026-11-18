#!/usr/bin/env bash
# Hook entry point for partner-agents: `hook.sh hook-stop`, `hook.sh hook-prompt`
# or `hook.sh hook-post-tool`.
#
# Delegates to partner.py. `hook-stop` decides whether the agent running this
# session owes a reply in .partner/chat.md or has no `wait` in flight, and if
# so prints a block decision; otherwise it closes the agent's turn. `hook-prompt`
# opens the turn, and it and `hook-post-tool` hand the agent new messages. All
# fail open: no working Python, no plugin script, or no partner session ->
# nothing happens.
set -euo pipefail

# PostToolUse fires after every tool call in every session with this plugin
# enabled. Outside a partner session, leave before starting any Python.
if [ "${1:-}" = "hook-post-tool" ]; then
  d=$PWD
  while [ ! -f "$d/.partner/roster.json" ]; do
    [ "$d" = "/" ] || [ -z "$d" ] && exit 0
    d=$(dirname "$d")
  done
fi

# Pick a Python that actually runs. `command -v python3` on Windows often
# resolves to the Microsoft Store alias, which is a stub that errors out -- so
# test each candidate instead of trusting the lookup.
py=""
for c in python python3 py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
    py=$c
    break
  fi
done
[ -n "$py" ] || exit 0

root=${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"}}
script="$root/skills/partner/scripts/partner.py"
[ -f "$script" ] || exit 0

exec "$py" "$script" "${1:-hook-stop}"
