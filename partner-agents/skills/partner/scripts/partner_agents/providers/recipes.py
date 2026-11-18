"""Launch recipes for the agent CLIs whose flags were checked by hand.

Each provider needs one thing: how to start its normal interactive session
with a starting prompt and without stopping to ask permission for routine
work. Nothing is run headlessly, so there are no session ids to track and no
output formats to parse -- which is why an unlisted CLI needs only a template.

AUTO LEVELS
  ask    leave the CLI's own prompting alone
  edits  auto-accept file edits, still sandboxed
  auto   the CLI's own reviewer approves each action  (default)
  full   no prompts and no sandbox

`auto` is the default because `edits` still asks before every shell command
-- including the partner's own `read` and `send`, so a partner launched that
way sits at a prompt in a tab nobody is watching. A CLI with no reviewer of its
own (gemini) gets its no-prompt mode at `auto`.

Flags verified against claude 2.x and codex 0.5x. They drift between
releases; this table is the single place to correct them.

RECIPES holds hand-verified launch flags for the CLIs whose flags were
checked by hand. It is an accelerator, not a permission list: every code path
falls back to probing when a name is not in it.
"""
from __future__ import annotations

from ..prompts import EFFORT_HINT


AUTO_LEVELS = ("ask", "edits", "auto", "full")


def _claude_tui(c: dict) -> list[str]:
    argv = ["claude"]
    if c["model"]:
        argv += ["--model", c["model"]]
    mode = {"edits": "acceptEdits", "auto": "auto",
            "full": "bypassPermissions"}.get(c["auto"])
    if mode:
        argv += ["--permission-mode", mode]
    return argv + [c["prompt"]]


def _codex_tui(c: dict) -> list[str]:
    argv = ["codex"]
    if c["model"]:
        argv += ["-m", c["model"]]
    if c["effort"]:
        argv += ["-c", f'model_reasoning_effort="{c["effort"]}"']
    if c["auto"] == "edits":
        argv += ["-a", "never", "-s", "workspace-write"]
    elif c["auto"] == "auto":
        argv += ["--approve-for-me"]
    elif c["auto"] == "full":
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    return argv + [c["prompt"]]


def _gemini_tui(c: dict) -> list[str]:
    argv = ["gemini"]
    if c["model"]:
        argv += ["-m", c["model"]]
    mode = {"edits": "auto_edit", "auto": "yolo", "full": "yolo"}.get(c["auto"])
    if mode:
        argv += ["--approval-mode", mode]
    return argv + ["-i", c["prompt"]]


RECIPES: dict[str, dict] = {
    "claude": {"bin": "claude", "tui": _claude_tui, "effort": "prompt",
               "efforts": set(EFFORT_HINT),
               "install": "npm install -g @anthropic-ai/claude-code",
               "login": "claude  (then /login)",
               "wait_note": (
                   "A background Bash command may run for up to 7200000 ms; "
                   "the foreground cap (120s by default, 600s max) does not "
                   "apply to it. Pass the timeout explicitly, or the default "
                   "of 30 minutes kills the wait early and costs a turn.")},
    "codex": {"bin": "codex", "tui": _codex_tui, "effort": "flag",
              # A real API field, so only values the API accepts work here.
              # "max" is this skill's own level -- it maps onto "high".
              "efforts": {"low", "medium", "high"},
              "install": "npm install -g @openai/codex",
              "login": "codex login"},
    "gemini": {"bin": "gemini", "tui": _gemini_tui, "effort": "prompt",
               "efforts": set(EFFORT_HINT),
               "install": "npm install -g @google/gemini-cli",
               "login": "gemini  (then follow the browser prompt)"},
}

# Kept as an alias: older briefings and any external caller still say PROVIDERS.
PROVIDERS = RECIPES
