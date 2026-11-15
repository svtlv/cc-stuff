#!/usr/bin/env bash
# Hook entry point for autopilot: `hook.sh session-start|prompt|stop`.
#
# Delegates to autopilot.py hook, which reads the hook input from stdin. It
# prints nothing unless a run in this repository needs a chat: a live run that
# no chat is watching, or a finished run whose end was not reported. Fails open:
# no working Python or no driver script -> nothing happens.
set -uo pipefail

py=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
    py=$c
    break
  fi
done
[ -n "$py" ] || exit 0

root=${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"}}
script="$root/skills/autopilot/scripts/autopilot.py"
[ -f "$script" ] || exit 0

"$py" "$script" hook "${1:-prompt}" 2>/dev/null || true
exit 0
