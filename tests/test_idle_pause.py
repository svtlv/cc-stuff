"""A tab nobody can type into must not keep a finished session cycling."""
from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'partner-agents/skills/partner/scripts'))
from partner_agents import idle


ROSTER = {'partners': {
    'p1': {'kind': 'session', 'status': 'running'},
    'p2': {'kind': 'tab', 'status': 'running', 'tab_kind': 'apple-terminal'},
}}


class IdlePause(unittest.TestCase):
    def verdict(self, wakeable: dict, busy: dict | None = None):
        busy = busy or {}
        with patch.object(idle, 'last_activity', return_value=time.time() - 3600), \
                patch.object(idle, 'agent_busy',
                             side_effect=lambda sd, r, p: busy.get(p, '')), \
                patch.object(idle, 'wakeable',
                             side_effect=lambda r, p: wakeable[p]):
            return idle.idle_verdict(Path('.'), ROSTER)

    def test_unwakeable_tab_no_longer_blocks_the_pause(self):
        ok, why = self.verdict({'p1': True, 'p2': False})
        self.assertTrue(ok)
        self.assertIn('p2 cannot be woken by a message', why)
        self.assertIn('`continue`', why)

    def test_all_wakeable_pauses_without_naming_anyone(self):
        ok, why = self.verdict({'p1': True, 'p2': True})
        self.assertTrue(ok)
        self.assertNotIn('cannot be woken', why)

    def test_a_busy_agent_still_holds_the_pause_off(self):
        ok, why = self.verdict({'p1': True, 'p2': False}, busy={'p1': 'mid-turn'})
        self.assertFalse(ok)
        self.assertIn('p1 mid-turn', why)

    def test_manual_banner_says_the_human_wakes_it(self):
        text = idle.pause_banner({'since': 'now'}, sleeper=False, manual=True)
        self.assertIn('end your turn now', text)
        self.assertIn('human types `continue`', text)
        self.assertNotIn('types a wake-up into your tab', text)


if __name__ == '__main__':
    unittest.main()
