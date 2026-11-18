"""Idle partners cost nothing: no polling, and input wakes only who owes a reply."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'partner-agents/skills/partner/scripts'
sys.path.insert(0, str(SCRIPTS))
from partner_agents import state, transcript, waking
from partner_agents.briefing import build_seed
from partner_agents.commands import hooks
from partner_agents.timing import HOP_LIMIT
from partner_agents.transcript import FINAL_TAG, append_msg, pending_for


class PushWake(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.git').mkdir()
        self.sd = self.root / '.partner'
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        waking._TYPABLE.clear()
        self.roster = {'self': 'p1', 'baton': 'p1', 'partners': {
            'p1': {'id': 'p1', 'provider': 'claude', 'kind': 'session',
                   'status': 'running'},
            'p2': {'id': 'p2', 'provider': 'codex', 'kind': 'tab',
                   'status': 'running', 'tab_kind': 'orca'},
            'p3': {'id': 'p3', 'provider': 'claude', 'kind': 'tab',
                   'status': 'running'}}}
        state.save_roster(self.sd, self.roster)

    def as_agent(self, pid):
        os.environ['PARTNER_ID'] = pid

    def test_listen_mode_per_provider_and_tab(self):
        with patch.object(waking, 'typable', side_effect=lambda r, p: p == 'p2'):
            self.assertEqual(waking.listen_mode(self.roster, 'p1'), 'background')
            self.assertEqual(waking.listen_mode(self.roster, 'p2'), 'typed')
            self.assertEqual(waking.listen_mode(self.roster, 'p3'), 'background')
        with patch.object(waking, 'typable', return_value=False):
            self.assertEqual(waking.listen_mode(self.roster, 'p2'), 'manual')

    def test_final_message_creates_no_debt(self):
        append_msg(self.sd, 'p1', 'p2', 'Please check this.', 'p1')
        self.assertEqual(len(pending_for(self.sd, self.roster, 'p2')), 1)
        append_msg(self.sd, 'p1', 'p2', f'{FINAL_TAG} Settled, thanks.', 'p1')
        self.assertEqual(len(pending_for(self.sd, self.roster, 'p2')), 1)
        append_msg(self.sd, 'p2', 'p1', 'Done.', 'p1')
        self.assertEqual(pending_for(self.sd, self.roster, 'p2'), [])

    def test_advisor_ping_pong_stops_creating_debts(self):
        # p2 and p3 advise; p1 holds the baton. They answer each other forever.
        for i in range(HOP_LIMIT + 1):
            sender, to = ('p2', 'p3') if i % 2 == 0 else ('p3', 'p2')
            append_msg(self.sd, sender, to, f'AGREED {i}', 'p1')
        # The last message is the (HOP_LIMIT + 1)th in the chain: no debt.
        last_to = 'p3' if HOP_LIMIT % 2 == 0 else 'p2'
        self.assertEqual(pending_for(self.sd, self.roster, last_to), [])
        self.assertIn('advisor messages in a row', transcript.hop_limit_note(self.sd))
        # The baton holder speaking resets the chain.
        append_msg(self.sd, 'p1', last_to, 'New question.', 'p1')
        self.assertEqual(len(pending_for(self.sd, self.roster, last_to)), 1)
        self.assertEqual(transcript.hop_limit_note(self.sd), '')

    def wake(self, **kw):
        calls = []

        def fake_nudge(sd, roster, pid, why, throttle=0, relay=True):
            calls.append(pid)
            return {'id': pid, 'nudged': True, 'why': why}
        with patch.object(waking, 'typable', side_effect=lambda r, p: p == 'p2'), \
                patch.object(waking, 'nudge_agent', side_effect=fake_nudge):
            res = waking.wake_owed(self.sd, self.roster, **kw)
        return calls, res

    def test_idle_typed_agent_is_woken_once_per_message(self):
        append_msg(self.sd, 'p1', 'p2', 'Review this.', 'p1')
        calls, res = self.wake()
        self.assertEqual(calls, ['p2'])
        self.assertEqual(res['woke'], ['p2'])
        # Same message, no reaction yet, inside the retry window: not again.
        self.assertEqual(self.wake()[0], [])
        # A newer message for it wakes it again.
        append_msg(self.sd, 'p1', 'p2', 'And this.', 'p1')
        self.assertEqual(self.wake()[0], ['p2'])

    def test_idle_baton_holder_is_woken_for_late_advice(self):
        self.roster['baton'] = 'p2'
        state.save_roster(self.sd, self.roster)
        append_msg(self.sd, 'p3', 'p2', 'Late objection.', 'p2')
        self.assertEqual(pending_for(self.sd, self.roster, 'p2'), [])
        self.assertEqual(self.wake()[0], ['p2'])

    def test_busy_or_untypable_agents_are_not_typed_into(self):
        append_msg(self.sd, 'p1', '@all', 'Thoughts?', 'p1')
        (self.sd / 'p2').mkdir(parents=True, exist_ok=True)
        (self.sd / 'p2/turn').write_text(str(__import__('time').time()))
        calls, _ = self.wake()
        # p2 is mid-turn (the hooks reach it); p3 is Claude in no typable tab.
        self.assertEqual(calls, [])

    def run_stop(self, pid):
        self.as_agent(pid)
        out = io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO(json.dumps({'cwd': str(self.root)}))), \
                patch.object(hooks, 'ensure_waker'), \
                patch.object(hooks, 'listening_soon', return_value=False), \
                patch.object(waking, 'typable', return_value=True), \
                contextlib.redirect_stdout(out):
            hooks.cmd_hook_stop(None)
        return json.loads(out.getvalue())

    def test_stop_lets_typed_agent_idle_without_wait(self):
        (self.sd / 'p2').mkdir(parents=True, exist_ok=True)
        (self.sd / 'p2/turn').write_text('1')
        self.assertEqual(self.run_stop('p2'), {})
        self.assertFalse((self.sd / 'p2/turn').exists())

    def test_stop_holds_claude_tab_without_background_wait(self):
        decision = self.run_stop('p3')
        self.assertEqual(decision['decision'], 'block')
        self.assertIn('BACKGROUND', decision['reason'])
        self.assertIn('7200000', decision['reason'])

    def test_post_tool_hook_hands_over_new_messages_once(self):
        self.as_agent('p2')
        append_msg(self.sd, 'p1', 'p2', 'Mid-turn note.', 'p1')

        def post():
            out = io.StringIO()
            with patch.object(sys, 'stdin', io.StringIO(json.dumps({'cwd': str(self.root)}))), \
                    contextlib.redirect_stdout(out):
                hooks.cmd_hook_post_tool(None)
            return json.loads(out.getvalue())
        first = post()
        ctx = first['hookSpecificOutput']
        self.assertEqual(ctx['hookEventName'], 'PostToolUse')
        self.assertIn('Mid-turn note.', ctx['additionalContext'])
        self.assertIn('NOT a human instruction', ctx['additionalContext'])
        self.assertEqual(post(), {})
        # Handed over is not answered: the debt is still there for the Stop hook.
        self.assertEqual(len(pending_for(self.sd, self.roster, 'p2')), 1)

    def test_tab_briefings_listen_without_polling(self):
        claude = build_seed('p3', self.roster, self.root, self.sd,
                            SCRIPTS / 'partner.py', False, 'tab')
        self.assertIn('run_in_background: true', claude)
        self.assertIn('wait --timeout 7000', claude)
        codex = build_seed('p2', self.roster, self.root, self.sd,
                           SCRIPTS / 'partner.py', False, 'tab')
        self.assertIn('you do not run `wait`', codex)
        self.assertNotIn('run_in_background', codex)
        for seed in (claude, codex):
            self.assertIn('--final', seed)
            self.assertNotIn('paused', seed)


if __name__ == '__main__':
    unittest.main()
