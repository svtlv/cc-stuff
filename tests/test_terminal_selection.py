"""A partner opens in the app the session was started from, else anywhere."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'partner-agents/skills/partner/scripts'))
from partner_agents.terminals import detect, tabs


def first(env: dict, prefer: str | None = None) -> str:
    with patch.dict(os.environ, env, clear=True):
        return detect.terminal_order(prefer)[0][0]


class TerminalSelection(unittest.TestCase):
    def setUp(self):
        # Only the built-in table: no user or repo terminal files.
        for p in (patch.object(detect, 'TERMINAL_FILES', []),
                  patch.object(detect, 'state_dir', side_effect=OSError)):
            p.start()
            self.addCleanup(p.stop)

    def test_orca_chat_session_ranks_orca_first(self):
        env = {'__CFBundleIdentifier': 'com.stablyai.orca'}
        self.assertEqual(first(env), 'orca')
        env['ORCA_AGENT_SESSION_ID'] = 'claude_x'
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(detect.host_match(detect.TERMINALS['orca']), 'inside')
            self.assertTrue(detect.terminal_available(
                {**detect.TERMINALS['orca'], 'bin': sys.executable})[0])

    def test_the_app_on_screen_beats_other_installed_terminals(self):
        cases = [
            ('apple-terminal', {'TERM_PROGRAM': 'Apple_Terminal'}),
            ('apple-terminal', {'__CFBundleIdentifier': 'com.apple.Terminal'}),
            ('iterm', {'TERM_PROGRAM': 'iTerm.app', 'ITERM_SESSION_ID': 'w0'}),
            ('wezterm', {'WEZTERM_PANE': '3'}),
            ('ghostty', {'TERM': 'xterm-ghostty'}),
            ('gnome-terminal', {'GNOME_TERMINAL_SCREEN': '/org/x'}),
        ]
        for want, env in cases:
            with self.subTest(env=env):
                self.assertEqual(first(env), want)

    def test_multiplexer_inside_an_app_wins(self):
        env = {'TMUX': '/tmp/t,1,0', 'TERM_PROGRAM': 'tmux',
               '__CFBundleIdentifier': 'com.apple.Terminal'}
        self.assertEqual(first(env), 'tmux')

    def test_explicit_choice_still_comes_first(self):
        env = {'TERM_PROGRAM': 'Apple_Terminal'}
        self.assertEqual(first(env, prefer='iterm'), 'iterm')

    def test_falls_back_when_the_same_app_cannot_open_a_tab(self):
        tried = []

        def open_in(name, spec, title, runner, cwd):
            tried.append(name)
            return {} if name == 'apple-terminal' else {'kind': name}

        env = {'TERM_PROGRAM': 'Apple_Terminal'}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(tabs, 'terminal_available', return_value=(True, '')), \
                patch.object(tabs, 'open_in', side_effect=open_in), \
                patch.object(detect, 'discover_terminals', return_value={}):
            tab = tabs.open_tab('partner:p2', Path('run.sh'), Path('.'))
        self.assertEqual(tried[0], 'apple-terminal')
        self.assertEqual(tab['kind'], tried[1])


if __name__ == '__main__':
    unittest.main()
