"""Exercise the autopilot driver against a fake plan runner CLI."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'autopilot/skills/autopilot/scripts/autopilot.py'
sys.path.insert(0, str(DRIVER.parent))
import autopilot  # noqa: E402


def python_tool(path, source):
    """An executable fixture on POSIX, a Python + CMD pair on Windows."""
    if os.name == 'nt':
        script = path.with_suffix('.py')
        script.write_text(source, encoding='utf-8')
        command = path.with_suffix('.cmd')
        with command.open('w', encoding='utf-8', newline='') as handle:
            handle.write(f'@echo off\r\n"{sys.executable}" -X utf8 "{script}" %*\r\n'
                         'exit /b %ERRORLEVEL%\r\n')
        return command
    path.write_text(f'#!{sys.executable}\n' + source, encoding='utf-8')
    path.chmod(0o755)
    return path


def system_tool_path():
    """Keep git/Python available without inheriting model CLIs from the machine."""
    folders = [str(Path(shutil.which('git')).parent), str(Path(sys.executable).parent)]
    if os.name == 'nt':
        folders.append(str(Path(os.environ['SystemRoot']) / 'System32'))
    return os.pathsep.join(folders)


def runner_split(text):
    """Port of the runner's splitArgs (pkg/executor/executor.go): quotes and backslashes."""
    args, cur, quote, escaped = [], '', None, False
    for ch in text:
        if escaped:
            cur, escaped = cur + ch, False
        elif ch == '\\':
            escaped = True
        elif ch in '"\'':
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
            else:
                cur += ch
        elif ch == ' ' and quote is None:
            if cur:
                args.append(cur)
            cur = ''
        else:
            cur += ch
    return args + ([cur] if cur else [])


FAKE = textwrap.dedent('''\
    import os, subprocess, sys
    from pathlib import Path
    sys.path.insert(0, os.environ['AUTOPILOT_DRIVER_DIR'])
    import autopilot
    args = [a for a in sys.argv[1:] if a != '--no-color']
    if args == ['--version']:
        print('ralphex v1.7.0-windows.4'); sys.exit(0)
    def out(s, end='\\n'):
        sys.stdout.write(s + end); sys.stdout.flush()
    def line():
        return sys.stdin.readline().strip()
    def ask(question, options):
        # like the runner's AskQuestion: fzf (the driver's shim) says "no match", then stdin
        import shutil
        p = subprocess.run([shutil.which('fzf'), '--prompt', question + ': ', '--height', '10'],
                           input='\\n'.join(options + ['Other (type your own answer)']) + '\\n',
                           text=True, capture_output=True)
        assert p.returncode == 1, p.returncode
        out('Enter your answer: ', end='')
        return line()
    def review(plan):
        # the runner's plan review loop: reviewer, findings, revision, then the question again
        for rnd in (1, 2):
            pick = ask('Review the plan before implementation?',
                       ['External reviewer', 'Same agent', 'Skip review'])
            out('REVIEWER: ' + pick)
            if pick == 'Skip review':
                out('[26-01-01 10:00:03] plan review skipped'); return
            out('\\n--- plan review round %d ---\\n' % rnd)
            if rnd == 2:
                out('[26-01-01 10:00:04] plan review found no issues'); return
            out('[26-01-01 10:00:03] plan review findings:')
            out('[26-01-01 10:00:03]   1. CRITICAL: Task 1 - no test step')
            out('[26-01-01 10:00:03]   2. MINOR: Overview - wording')
            out('[26-01-01 10:00:03]   3. MAJOR: Task 1 - wrong path src/a.py, the file')
            out('[26-01-01 10:00:03]   is src/b.py')
            picked = ask('Which findings should be applied?', ['All findings', 'None'])
            out('SELECTED: ' + picked)
            if picked == 'None':
                out('[26-01-01 10:00:03] no findings selected, plan left unchanged'); return
            out('[26-01-01 10:00:03] applying selected findings to the plan...')
            plan.write_text(plan.read_text() + '- [ ] test it\\n')
            out('[26-01-01 10:00:04] plan revised')
    if os.environ.get('FAKE_FATAL'):
        out('error: unknown flag --bogus'); sys.exit(2)
    if os.environ.get('FAKE_MODE') == 'claude-down' and '--codex' not in args:
        out('--- task iteration 1 ---')
        out('ERROR: claude execution: Not logged in. Run claude auth login')
        sys.exit(1)
    if os.environ.get('FAKE_MODE') == 'claude-down' and args[0] != '--plan':
        out('CODEX-FALLBACK: ' + ' '.join(args))
        plan = Path(args[0]); plan.write_text(plan.read_text().replace('- [ ]', '- [x]'))
        sys.exit(0)
    if args[0] == '--plan':
        out('--- plan iteration 1 ---')
        out('agrouter: selection: {"cli":"claude","model":"claude-opus-5-5","effort":"high"}')
        # like the runner: exec.LookPath("fzf") (PATHEXT finds fzf.cmd on Windows), then
        # start the resolved file directly, with no shell (Go's os/exec, not cmd /C)
        import shutil
        p = subprocess.run([shutil.which('fzf'), '--prompt', 'Which cache backend?: ', '--height', '10'],
                           input='Redis\\nIn-memory, local\\nOther (type your own answer)\\n',
                           text=True, capture_output=True)
        assert p.returncode == 1, p.returncode
        out('Enter your answer: ', end='')
        out('[26-01-01 10:00:00] ANSWER: ' + line())
        while True:
            out('\\n━━━ Plan Draft ━━━\\n# Plan: cache\\n### Task 1: add\\n- [ ] do it\\n━━━━━━━━━━━━━━━━━━\\n')
            out('Review the plan draft')
            for i, o in enumerate(['Accept', 'Revise', 'Interactive review', 'Reject'], 1):
                out(f'  {i}) {o}')
            out('Enter number (1-4): ', end='')
            pick = line()
            if pick == '2':
                out('\\nEnter revision feedback: ', end='')
                out('[26-01-01 10:00:01] FEEDBACK: ' + line())
                continue
            if pick == '3':
                tmp = Path('edit-tmp.md'); tmp.write_text('x')
                # like the runner: resolve the editor, then execx adds "cmd /C" for a .cmd path
                import shutil
                editor = shutil.which(os.environ['VISUAL'])
                argv = [editor, str(tmp)]
                if editor.lower().endswith(('.cmd', '.bat')):
                    argv = ['cmd', '/C'] + argv
                subprocess.run(argv, check=True)
                out('[26-01-01 10:00:02] EDITED: ' + tmp.read_text().splitlines()[0]); tmp.unlink()
                continue
            break
        Path('docs/plans').mkdir(parents=True, exist_ok=True)
        Path('docs/plans/cache.md').write_text('### Task 1: add\\n- [ ] do it\\n')
        if os.environ.get('FAKE_MODE') == 'plan-review':
            review(Path('docs/plans/cache.md'))
        out('\\nplan creation completed in 1s, created docs/plans/cache.md')
        out('Continue with plan implementation? [y/N]: ', end='')
        out('ANSWER-CONTINUE: ' + line())
        sys.exit(0)
    if args[0] == '--plan-review':
        out('ARGS: ' + ' '.join(args))
        review(Path(args[1]))
        out('\\nplan review completed in 1s')
        out('Continue with plan implementation? [y/N]: ', end='')
        out('ANSWER-CONTINUE: ' + line())
        sys.exit(0)
    mode = os.environ.get('FAKE_MODE', '')
    if mode in ('review', 'review-later'):
        import signal, time
        if '--external-review-tool=none' in args:
            out('ARGS-NONE'); sys.exit(0)
        broke = []
        if hasattr(signal, 'SIGQUIT'):
            signal.signal(signal.SIGQUIT, lambda *a: broke.append(1))
        out('--- task iteration 1 ---')
        time.sleep(3 if mode == 'review-later' else 0.2)
        out('--- codex iteration 1 ---')
        for _ in range(100):
            if broke:
                out('[26-01-01 10:00:00] manual break requested, external review terminated early')
                break
            time.sleep(0.1)
        out('--- review 2 ---'); sys.exit(0)
    if mode == 'codex-down':
        if '--external-review-tool=none' in sys.argv:
            out('ARGS-NONE'); sys.exit(0)
        out('--- codex iteration 1 ---')
        out('[26-01-01 10:00:00] ERROR: codex: exec: "codex": executable file not found in $PATH')
        sys.exit(1)
    if mode == 'codex-banner':
        out('--- task iteration 1 ---')
        out('[26-01-01 10:00:00] model: gpt-6.1-sol')
        out('[26-01-01 10:00:00] reasoning effort: high')
        out('[26-01-01 10:00:01] Task 1: working')
        import time; time.sleep(1.5)
        out('agrouter: selection: {"cli":"claude","model":"claude-opus-5-5","effort":"medium"}')
        out('--- claude evaluating codex findings ---')
        time.sleep(1.5)
        sys.exit(0)
    if mode == 'milestones':
        import time
        plan = Path(args[0])
        def done(*boxes):
            text = plan.read_text()
            for b in boxes:
                text = text.replace('- [ ] ' + b, '- [x] ' + b)
            plan.write_text(text)
        def commit(msg):
            subprocess.run(['git', 'add', '-A'], check=True, capture_output=True)
            subprocess.run(['git', 'commit', '-q', '-m', msg], check=True, capture_output=True)
        out('--- task iteration 1 ---')
        # the real runner prints the selection header and its JSON on separate lines
        out('[26-01-01 10:00:00] agrouter: selection:')
        out('[26-01-01 10:00:00] {"cli":"claude","model":"claude-opus-5-5","effort":"xhigh"}')
        Path('src').mkdir(exist_ok=True)
        Path('src/cache.py').write_text('x = 1\\ny = 2\\n')
        time.sleep(1.2); done('a', 'b'); commit('Task 1: one'); time.sleep(1.5)
        out('--- task iteration 2 ---')
        # routing fails: agrouter warns, then its selection says what fell back, wrapped at spaces
        out("[26-01-01 10:00:01] agrouter: warning: Classifier routing failed at the classify stage")
        out("[26-01-01 10:00:01] (classifier_timeout); model and effort fall back to claude defaults")
        out('[26-01-01 10:00:01] agrouter: selection:')
        import json, textwrap as tw
        sel = json.dumps({'cli': 'claude', 'model': None, 'effort': None,
                          'reason': {'code': 'classifier_timeout', 'stage': 'classify',
                                     'message': 'Model routing timed out'},
                          'fallback': {'action': 'child_defaults', 'fields': ['model', 'effort'],
                                       'message': "Model and effort fall back to the child's defaults"},
                          'effective': {'model': {'value': 'claude-opus-5-5',
                                                  'source': 'verified_child_default'},
                                        'effort': {'value': None, 'source': 'child_default_unknown',
                                                   'reason': {'code': 'default_not_reported'}}}},
                         separators=(',', ':'))
        for part in tw.wrap(sel, 60, break_long_words=False, break_on_hyphens=False):
            out('[26-01-01 10:00:01] ' + part)
        time.sleep(1.2); done('c'); commit('Task 2: two'); time.sleep(1.5)
        out('--- claude review 1 ---')
        out('agrouter: selection: {"cli":"claude","model":"claude-opus-5-5","effort":"high"}')
        time.sleep(1.2); Path('fix.txt').write_text('x'); commit('fix: review finding')
        time.sleep(1.5)
        sys.exit(0)
    if mode == 'env':
        leaked = [k for k in ('CLAUDECODE', 'CODEX_SANDBOX', 'CODEX_THREAD_ID') if k in os.environ]
        out('LEAKED=' + ','.join(leaked)); sys.exit(0)
    if mode == 'limit-wait':
        import time
        out('--- task iteration 1 ---')
        out('[26-01-01 10:00:00] I am adding rate limit middleware to the API')
        out('[26-01-01 10:00:01] Task 1: add rate limiting')
        out("agrouter: You've hit your limit; weekly subscription quota exhausted; resets at 2099-01-01T00:00:00Z")
        out('[26-01-01 10:00:02] rate limit detected: "You' + "'" + 've hit your limit" in claude output, waiting 2s before retry...')
        time.sleep(2.5)
        out("agrouter: You've hit your limit; weekly subscription quota exhausted; resets at 2099-01-01T00:00:00Z")
        out('[26-01-01 10:00:05] rate limit detected: "You' + "'" + 've hit your limit" in claude output, waiting 2s before retry...')
        time.sleep(2.5)
        out('[26-01-01 10:00:08] continuing the task after the wait')
        sys.exit(0)
    if mode in ('limit-exit', 'limit-long'):
        import datetime, time
        counter = Path(os.environ['FAKE_COUNTER'])
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        if n == 0:
            secs = 2 if mode == 'limit-exit' else 36000
            when = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=secs))
            out("agrouter: You've hit your limit; weekly subscription quota exhausted; resets at "
                + when.strftime('%Y-%m-%dT%H:%M:%SZ'))
            out('error: detected "You' + "'" + 've hit your limit" in claude output')
            out("run 'claude /usage' for more information")
            sys.exit(1)
        out('--- task iteration 1 ---'); sys.exit(0)
    counter = Path(os.environ['FAKE_COUNTER'])
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    out('--- task iteration 1 ---')
    if n == 0:
        out('[26-01-01 10:00:00] ERROR: claude execution: signal: killed')
        out('Failed: 2026-01-01 10:00:01 (1s) - runner: task phase: killed'); sys.exit(1)
    plan = Path(args[0]); plan.write_text(plan.read_text().replace('- [ ]', '- [x]'))
    out('--- claude review 1 ---')
    out('[26-01-01 10:00:05] reviewing changes')
    sys.exit(0)
''')


class AutopilotDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, check=True)
        fake = base / ('fake-cli.py' if os.name == 'nt' else 'fake-cli')
        fake.write_text(f'#!{sys.executable}\n' + FAKE, encoding='utf-8')
        fake.chmod(0o755)
        self.fakebin = base / 'bin'
        self.fakebin.mkdir()
        for name in ('claude', 'codex'):
            python_tool(self.fakebin / name, textwrap.dedent('''\
                import json, os, sys
                if '--version' in sys.argv:
                    print('cli 0.169.0')
                elif 'auth' in sys.argv:
                    print(json.dumps({'loggedIn': os.environ.get('FAKE_CLAUDE_LOGIN', '1') == '1'}))
                elif 'login' in sys.argv:
                    sys.exit(0 if os.environ.get('FAKE_CODEX_LOGIN', '1') == '1' else 1)
                else:
                    print('No findings.')
                '''))
        self.env = dict(os.environ, AUTOPILOT_CLI=str(fake),
                        AUTOPILOT_STATE_DIR=str(base / 'state'), AUTOPILOT_BACKOFF='0',
                        AUTOPILOT_NO_ROUTER='1', AUTOPILOT_LIMIT_MIN='1', AUTOPILOT_CODEX_LOGIN_OK='1',
                        RALPHEX_CONFIG_DIR=str(base / 'no-runner-config'),
                        AGROUTER_CONFIG_DIR=str(base / 'no-router-config'),
                        AUTOPILOT_CONFIG=str(base / 'no-autopilot-config'),
                        AUTOPILOT_DRIVER_DIR=str(DRIVER.parent), PYTHONUTF8='1',
                        PATH=str(self.fakebin) + os.pathsep + os.environ['PATH'],
                        FAKE_COUNTER=str(base / 'counter'), AUTOPILOT_WAKE='off')
        # never type into the terminal the tests run in
        for var in ('ORCA_TERMINAL_HANDLE', 'TMUX', 'TMUX_PANE', 'WEZTERM_PANE', 'KITTY_WINDOW_ID',
                    'KITTY_LISTEN_ON', 'ITERM_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
            self.env.pop(var, None)

    def commit_configs(self):
        """The runner refuses to branch from a dirty default branch; commit what a test wrote."""
        subprocess.run(['git', 'add', '-A', '.'], cwd=self.repo, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm',
                        'configs'], cwd=self.repo, check=True, capture_output=True)

    def drive(self, *args, env=None):
        res = subprocess.run([sys.executable, str(DRIVER), *args], cwd=self.repo,
                             env=env or self.env, capture_output=True, text=True,
                             encoding='utf-8', timeout=60)
        self.assertTrue(res.stdout.strip(), res.stderr)
        return json.loads(res.stdout.strip().splitlines()[-1])

    def wait(self, sid, since=0):
        return self.drive('wait', sid, '--timeout', '20', '--since', str(since))

    def until(self, sid, status, since=0):
        deadline = time.time() + 30
        while time.time() < deadline:
            rep = self.wait(sid, since)
            if rep['status'] == status:
                return rep
            since = rep['seq']
        self.fail(f'never reached {status}: {rep}')

    def next_report(self, sid, timeout=20):
        res = subprocess.run([sys.executable, str(DRIVER), 'next', sid, '--timeout', str(timeout)],
                             cwd=self.repo, env=self.env, capture_output=True, text=True,
                             encoding='utf-8', timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_next_wakes_once_per_prompt_with_short_text(self):
        sid = self.drive('plan', '--desc', 'add caching')['session']
        deadline = time.time() + 30
        text = ''
        while 'PROMPT' not in text and time.time() < deadline:
            text = self.next_report(sid)
        self.assertIn('PROMPT question', text)
        self.assertIn('question: Which cache backend?', text)
        self.assertIn('  2. In-memory, local', text)
        self.assertIn('recommended: none registered', text)
        self.assertIn(f'answer: AP answer {sid} --choice N', text)
        self.assertLess(len(text), 1500)
        # the same prompt does not wake the chat again: only the timeout ends this call
        self.assertIn('NO NEWS', self.next_report(sid, timeout=2))
        # a new chat reattaching sees the open prompt again
        res = subprocess.run([sys.executable, str(DRIVER), 'next', sid, '--catch-up', '--timeout', '5'],
                             cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn('PROMPT question (id', res.stdout)
        self.drive('recommend', sid, '--choice', '2', '--reason', 'no new service')
        self.drive('answer', sid, '--choice', '2')
        text = ''
        while 'PROMPT draft' not in text and time.time() < deadline:
            text = self.next_report(sid)
        self.assertIn('draft: ', text)
        self.assertIn('--action revise --text', text)
        feedback = self.repo.parent / 'feedback.txt'
        feedback.write_text('keep $(NoWarn) as is\nand split', encoding='utf-8')
        self.drive('answer', sid, '--action', 'revise', '--text-file', str(feedback))
        while 'PROMPT draft' not in (text := self.next_report(sid)) and time.time() < deadline:
            pass
        self.drive('answer', sid, '--action', 'accept')
        while 'DONE' not in text and time.time() < deadline:
            text = self.next_report(sid)
        self.assertIn('DONE · plan file: docs/plans/cache.md', text)
        log = Path(self.drive('wait', sid, '--timeout', '1')['log']).read_text()
        self.assertIn('FEEDBACK: keep $(NoWarn) as is and split', log)

    def test_plan_question_revise_edit_accept(self):
        sid = self.drive('plan', '--desc', 'add caching')['session']
        rep = self.until(sid, 'waiting')
        w = rep['waiting']
        self.assertEqual(w['kind'], 'question')
        self.assertEqual(w['question'], 'Which cache backend?')
        self.assertEqual(w['options'], ['Redis', 'In-memory, local'])
        self.drive('answer', sid, '--choice', '2')
        rep = self.until(sid, 'waiting', rep['seq'])
        self.assertEqual(rep['waiting']['kind'], 'draft')
        self.assertIn('# Plan: cache', Path(rep['waiting']['draft_file']).read_text())
        self.drive('answer', sid, '--action', 'revise', '--text', 'split\ninto two')
        rep = self.until(sid, 'waiting', rep['seq'])
        edited = Path(rep['waiting']['edited_file'])
        edited.write_text('EDITED BY USER\n')
        self.drive('answer', sid, '--action', 'edited')
        rep = self.until(sid, 'waiting', rep['seq'])
        self.drive('answer', sid, '--action', 'accept')
        rep = self.until(sid, 'done', rep['seq'])
        self.assertEqual(rep['plan_file'], 'docs/plans/cache.md')
        # planning goes through the router too: its selection is tracked like a run's
        self.assertEqual(rep['model'], {'cli': 'claude', 'model': 'claude-opus-5-5', 'effort': 'high'})
        plan_session = self.drive('report', sid)['sessions'][0]
        self.assertEqual(plan_session['models_used'], {'claude/claude-opus-5-5/high': 1})
        log = Path(rep['log']).read_text()
        for expected in ('ANSWER: In-memory, local', 'FEEDBACK: split into two',
                         'EDITED: EDITED BY USER', 'ANSWER-CONTINUE: n'):
            self.assertIn(expected, log)

    def test_runner_plan_review_is_relayed_with_defaults(self):
        env = dict(self.env, FAKE_MODE='plan-review',
                   AUTOPILOT_TIMEOUTS='question=1,draft=1,review_pick=1,review_apply=1')
        sid = self.drive('plan', '--desc', 'add caching', env=env)['session']
        rep = self.until(sid, 'waiting')
        seen = {}
        deadline = time.time() + 40
        while rep['status'] != 'done' and time.time() < deadline:
            w = rep.get('waiting') or {}
            review = w.get('review')
            if review and w['id'] not in seen:
                seen[w['id']] = review
                if review['step'] == 'apply':
                    sdir = Path(self.env['AUTOPILOT_STATE_DIR']) / 'sessions' / sid
                    text = autopilot.next_text(rep, sdir)
                    self.assertIn('plan review: which findings', text)
                    self.assertIn('#3 MAJOR: Task 1 - wrong path src/a.py, the file is src/b.py', text)
                    self.assertIn('default on no reply: 1,3', text)
            rep = self.wait(sid, rep['seq'])
        self.assertEqual(rep['status'], 'done', rep)
        steps = [(r['step'], r['round'], r['default']['answer']) for r in seen.values()]
        self.assertEqual(steps, [('pick', 1, 'External reviewer'), ('apply', 1, '1,3'),
                                 ('pick', 2, 'Skip review')])
        first = list(seen.values())[0]
        self.assertEqual(first['plan_file'], 'docs/plans/cache.md')
        self.assertEqual([f['severity'] for f in list(seen.values())[1]['findings']],
                         ['CRITICAL', 'MINOR', 'MAJOR'])
        log = Path(rep['log']).read_text()
        for expected in ('REVIEWER: External reviewer', 'SELECTED: 1,3', 'REVIEWER: Skip review',
                         'ANSWER-CONTINUE: n'):
            self.assertIn(expected, log)
        self.assertIn('- [ ] test it', (self.repo / 'docs/plans/cache.md').read_text())
        report = self.drive('report', sid)
        timeouts = [(t['question'], t['chosen']) for t in report['timeouts']]
        self.assertIn(('Which findings should be applied?', '1,3'), timeouts)
        notes = [d['text'] for d in report['decisions'] if d['kind'] == 'note']
        self.assertIn('Plan review round 1 (External reviewer): 3 findings '
                      '(1 critical, 1 major, 1 minor)', notes)
        self.assertIn('Plan review: plan revised', notes)
        st = json.loads((Path(self.env['AUTOPILOT_STATE_DIR']) / 'sessions' / sid
                         / 'state.json').read_text())
        lines = [autopilot.event_line(e) for e in st['events'] if e['kind'] == 'plan-review']
        self.assertIn('• Plan review: Plan revised.', lines)
        self.assertIn('• Plan review: Plan review skipped.', lines)

    def test_plan_review_of_an_existing_plan(self):
        plan = self.repo / 'docs/plans/p.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('### Task 1: one\n- [ ] a\n')
        start = self.drive('plan-review', 'docs/plans/p.md')
        self.assertIn('--plan-review docs/plans/p.md', start['command'])
        sid = start['session']
        rep = self.until(sid, 'waiting')
        self.assertEqual(rep['waiting']['review']['plan_file'], 'docs/plans/p.md')
        self.drive('answer', sid, '--choice', '2')
        rep = self.until(sid, 'waiting', rep['seq'])
        self.assertEqual(rep['waiting']['review']['reviewer'], 'Same agent')
        self.drive('answer', sid, '--text', 'None', '--reason', 'all settled in chat')
        rep = self.until(sid, 'done', rep['seq'])
        self.assertEqual(rep['plan_file'], 'docs/plans/p.md')
        log = Path(rep['log']).read_text()
        for expected in ('REVIEWER: Same agent', 'SELECTED: None', 'ANSWER-CONTINUE: n'):
            self.assertIn(expected, log)
        self.assertIn('error', self.drive('plan-review', 'docs/plans/missing.md'))
        old = dict(self.env, AUTOPILOT_PLAN_REVIEW='0')
        self.assertIn('v1.7.0-windows.4', self.drive('plan-review', 'docs/plans/p.md', env=old)['error'])
        self.assertTrue(any('has no plan review' in x for x in
                            self.drive('doctor', '--json', env=old)['setup_incomplete']))

    def test_run_restarts_after_transient_failure(self):
        plan = self.repo / 'docs/plans/p.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('### Task 1: one\n- [ ] a\n- [ ] b\n')
        sid = self.drive('run', '--plan', 'docs/plans/p.md')['session']
        rep = self.until(sid, 'done')
        self.assertEqual(rep['restarts'], 1)
        self.assertEqual(rep['tasks'], {'done': 2, 'total': 2, 'current_task': None})
        self.assertEqual(rep['phase'], 'code review')
        st = json.loads((Path(self.env['AUTOPILOT_STATE_DIR']) / 'sessions' / sid
                         / 'state.json').read_text())
        kinds = [e['kind'] for e in st['events']]
        self.assertIn('error', kinds)
        self.assertIn('restart', kinds)

    def test_missing_tools_point_to_setup(self):
        env = {k: v for k, v in self.env.items()
               if k not in ('AUTOPILOT_CLI', 'AUTOPILOT_NO_ROUTER')}
        env['PATH'] = system_tool_path()
        report = self.drive('doctor', '--json', env=env)
        self.assertEqual(len(report['missing_tools']), 2)
        err = self.drive('plan', '--desc', 'x', env=env)['error']
        self.assertIn('not installed: ralphex.windows, agrouter', err)
        self.assertIn('autopilot-setup', err)
        env['AUTOPILOT_CLI'] = self.env['AUTOPILOT_CLI']
        self.assertIn('not installed: agrouter',
                      self.drive('run', '--plan', 'missing.md', env=env)['error'])

    def test_unanswered_prompts_take_recommendation_then_fallback(self):
        env = dict(self.env, AUTOPILOT_TIMEOUTS='question=2,draft=2')
        start = self.drive('plan', '--desc', 'add caching', env=env)
        sid, flow = start['session'], start['flow']
        rep = self.until(sid, 'waiting')
        self.assertLessEqual(rep['auto_answer_in_s'], 2)
        rec = self.drive('recommend', sid, '--choice', '2', '--reason', 'no new infra needed')
        self.assertEqual(rec['recommended'], 'In-memory, local')
        deadline = time.time() + 30
        while (rep['waiting'] or {}).get('kind') != 'draft' and time.time() < deadline:
            rep = self.wait(sid, rep['seq'])
        self.assertEqual(rep['waiting']['kind'], 'draft')
        # no recommendation for the draft: the fallback accepts it
        rep = self.until(sid, 'done', rep['seq'])
        plan = self.repo / 'docs/plans/cache.md'
        run = self.drive('run', '--plan', str(plan.relative_to(self.repo)), '--flow', flow)
        self.until(run['session'], 'done')
        self.drive('log', run['session'], '--kind', 'fix', '--text', 'fixed flaky test',
                   '--reason', 'same failure twice')
        report = self.drive('report', '--flow', flow)
        self.assertEqual([x['kind'] for x in report['sessions']], ['plan', 'run'])
        answers = [(d['by'], d['answer']) for d in report['decisions'] if d['kind'] == 'answer']
        self.assertEqual(answers, [('auto', 'In-memory, local'), ('auto', 'accept'),
                                   ('autopilot', 'no')])
        kinds = [d['kind'] for d in report['decisions']]
        self.assertIn('recovery', kinds)
        self.assertIn('fix', kinds)
        auto = [d for d in report['decisions'] if d['by'] == 'auto']
        self.assertIn('recommended', auto[0]['reason'])
        self.assertIn('no new infra needed', auto[0]['reason'])
        self.assertIn('accepted the draft', auto[1]['reason'])
        # agent-level timeout, logged the way the skills log "implement now" on no reply
        self.drive('log', run['session'], '--kind', 'decision', '--by', 'auto',
                   '--text', 'Implement now', '--question', 'Implement the plan now?',
                   '--waited', '60', '--reason', 'plan accepted, tests included')
        timeouts = self.drive('report', '--flow', flow)['timeouts']
        self.assertEqual([(t['chosen'], t['recommended']) for t in timeouts],
                         [('In-memory, local', True), ('accept', False), ('Implement now', True)])
        self.assertEqual(timeouts[0]['question'], 'Which cache backend?')
        self.assertTrue(all(t['waited_min'] is not None for t in timeouts))
        self.assertEqual(timeouts[2]['waited_min'], 60)
        self.assertIn('tests included', timeouts[2]['reason'])
        # the report's first section: every choice the user did not make, user answers and notes left out
        self.drive('log', run['session'], '--kind', 'note', '--text', 'watching the run')
        self.drive('log', run['session'], '--kind', 'decision', '--by', 'user', '--text', 'Use Redis')
        without = self.drive('report', '--flow', flow)['without_user']
        chosen = [w['chosen'] for w in without]
        self.assertTrue({'In-memory, local', 'accept', 'no', 'fixed flaky test',
                         'Implement now'} <= set(chosen))
        self.assertNotIn('Use Redis', chosen)
        self.assertNotIn('watching the run', chosen)
        self.assertNotIn('user', {w['by'] for w in without})
        self.assertIn('agent', {w['by'] for w in without})

    def test_milestones_report_each_task_with_its_model_and_commit(self):
        env = dict(self.env, FAKE_MODE='milestones', AUTOPILOT_PROGRESS_S='0.3',
                   GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com')
        plan = self.repo / 'p.md'
        plan.write_text('# Plan\n### Task 1: one\n- [ ] a\n- [ ] b\n### Task 2: two\n- [ ] c\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True, env=env)
        subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=self.repo, check=True, env=env)
        sid = self.drive('run', '--plan', 'p.md', env=env)['session']
        updates, since = [], 0
        deadline = time.time() + 40
        while time.time() < deadline:
            rep = self.wait(sid, since)
            updates += rep['updates']
            since = rep['seq']
            if rep['status'] == 'done':
                break
        self.assertEqual([u for u in updates if not u.startswith('✓ Run')], [
            '▶ Task 1/2 started: one · claude-opus-5-5 (xhigh) · steps 0/3\n   Plan: a; b',
            '✓ Task 1/2 done in 0 min: one · claude-opus-5-5 (xhigh) · steps 2/3 · '
            + updates[1].split('\n')[0].rsplit(' · ', 1)[1]
            + '\n   Done: a; b · 1 file (+2 -0): cache.py',
            '⚠ Router fallback on Task 2/2: two: Model routing timed out (classifier_timeout, '
            'classify stage). Not chosen by agrouter; runs on claude defaults: model '
            'claude-opus-5-5 (verified default), effort unknown (claude did not report it).',
            '▶ Task 2/2 started: two · claude-opus-5-5 (default effort) [router fallback] · '
            'steps 2/3\n   Plan: c',
            '✓ Task 2/2 done in 0 min: two · claude-opus-5-5 (default effort) [router fallback] · '
            'steps 3/3 · ' + updates[4].split('\n')[0].rsplit(' · ', 1)[1] + '\n   Done: c',
            '▶ Code review: claude review 1 · claude-opus-5-5 (high)',
            updates[6]])
        self.assertTrue(updates[1].split('\n')[0].endswith(' Task 1: one'))
        self.assertTrue(updates[4].split('\n')[0].endswith(' Task 2: two'))
        self.assertRegex(updates[6], r'^• Commit [0-9a-f]+: fix: review finding$')
        session = self.drive('report', sid)['sessions'][0]
        log = session['task_log']
        self.assertEqual(log['Task 1: one']['models'], ['claude-opus-5-5 (xhigh)'])
        self.assertEqual((log['Task 1: one']['about'], log['Task 1: one']['did'],
                          log['Task 1: one']['changes']), ('a; b', 'a; b', '1 file (+2 -0): cache.py'))
        self.assertTrue(log['Task 2: two']['commit'].endswith('Task 2: two'))
        self.assertEqual(session['models_used'], {'claude/claude-opus-5-5/xhigh': 1,
                                                  'claude/claude-opus-5-5/? (router fallback)': 1,
                                                  'claude/claude-opus-5-5/high': 1})
        # a fallback is not an error, and the report and decision log keep it
        self.assertEqual(session['errors'], [])
        [fb] = session['router_fallbacks']
        self.assertEqual((fb['code'], fb['title'], fb['fields']),
                         ('classifier_timeout', 'Task 2: two', ['model', 'effort']))
        decisions = self.drive('report', sid)['decisions']
        [d] = [d for d in decisions if d['by'] == 'agrouter']
        self.assertEqual(d['text'], 'Task 2: two: not routed, ran on claude defaults: model '
                         'claude-opus-5-5 (verified default), effort unknown (claude did not report it)')
        self.assertEqual(d['reason'], 'Model routing timed out (classifier_timeout)')

    def milestone_env(self):
        env = dict(self.env, FAKE_MODE='milestones', AUTOPILOT_PROGRESS_S='0.3',
                   CLAUDE_CODE_SESSION_ID='chat-1',
                   GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com')
        plan = self.repo / 'p.md'
        plan.write_text('# Plan\n### Task 1: one\n- [ ] a\n- [ ] b\n### Task 2: two\n- [ ] c\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.repo, check=True, env=env)
        subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=self.repo, check=True, env=env)
        return env

    def hook(self, event, env, chat='chat-1'):
        res = subprocess.run([sys.executable, str(DRIVER), 'hook', event], cwd=self.repo, env=env,
                             input=json.dumps({'session_id': chat, 'cwd': str(self.repo)}),
                             capture_output=True, text=True, timeout=30)
        return json.loads(res.stdout) if res.stdout.strip() else None

    def test_stop_after_task_then_resume_with_the_same_flow(self):
        env = self.milestone_env()
        start = self.drive('run', '--plan', 'p.md', env=env)
        sid = start['session']
        rep = self.wait(sid)
        while not any(u.startswith('▶ Task 1/2') for u in rep['updates']):
            rep = self.wait(sid, rep['seq'])
        self.assertEqual(self.drive('stop', sid, '--after-task', env=env)['status'], 'stop queued')
        updates = []
        deadline = time.time() + 30
        while time.time() < deadline:
            rep = self.wait(sid, rep['seq'])
            updates += rep['updates']
            if rep['status'] in ('stopped', 'done', 'failed'):
                break
        self.assertEqual(rep['status'], 'stopped')
        self.assertEqual(updates[0], '⏸ Will stop after Task 1/2 finishes (one).')
        self.assertTrue(updates[1].startswith('✓ Task 1/2 done'), updates)
        self.assertEqual(updates[-1], '■ Stopped after Task 1/2. Say "resume" to continue from the next one.')
        self.assertIn('- [ ] c', (self.repo / 'p.md').read_text())  # task 2 untouched
        resumed = self.drive('resume', sid, env=env)
        self.assertEqual(resumed['flow'], start['flow'])
        self.assertNotEqual(resumed['session'], sid)
        self.drive('stop', resumed['session'], env=env)

    def test_one_watcher_catch_up_and_hooks_keep_the_owner_in_charge(self):
        env = self.milestone_env()
        sid = self.drive('run', '--plan', 'p.md', env=env)['session']
        first = subprocess.Popen([sys.executable, str(DRIVER), 'wait', sid, '--timeout', '60',
                                  '--since', '999'], cwd=self.repo, env=env,
                                 stdout=subprocess.PIPE, text=True)
        time.sleep(1.5)
        # a second wait takes over; the first one leaves without reporting anything
        self.drive('wait', sid, '--timeout', '1', '--since', '999', env=env)
        out, _ = first.communicate(timeout=10)
        self.assertTrue(json.loads(out.strip().splitlines()[-1])['superseded'])
        while self.drive('watch-status', sid, env=env)['sessions'][0]['alive']:
            time.sleep(0.5)
        # nobody saw the run end: the hooks speak up, only the owner chat is held back
        context = self.hook('prompt', env)['hookSpecificOutput']['additionalContext']
        self.assertIn(sid, context)
        self.assertIn('This chat started it', context)
        self.assertEqual(self.hook('stop', env)['decision'], 'block')
        self.assertIsNone(self.hook('stop', env, chat='other-chat'))
        self.assertIn('Another chat started it',
                      self.hook('session-start', env, chat='other-chat')
                      ['hookSpecificOutput']['additionalContext'])
        # reattaching posts every missed line, in order
        caught = self.drive('wait', sid, '--catch-up', env=env)['updates']
        self.assertTrue(caught[0].startswith('▶ Task 1/2 started'), caught)
        self.assertEqual(caught[-1], '✓ Run finished. Writing the final report.')
        self.assertEqual(self.drive('wait', sid, '--catch-up', env=env)['updates'], [])
        self.drive('mark-reported', sid, env=env)
        self.assertIsNone(self.hook('stop', env))
        self.assertIsNone(self.hook('prompt', env))

    def test_unwatched_run_wakes_its_chat_by_typing_into_its_tab(self):
        env = self.milestone_env()
        typed = Path(self.tmp.name) / 'typed.txt'
        tool = Path(self.tmp.name) / 'type-into-tab'
        python_tool(tool, f'import sys\nopen({str(typed)!r}, "a").write(sys.argv[1] + "\\n")\n')
        cfg = Path(self.tmp.name) / 'autopilot.cfg'
        cfg.write_text(f'[wake]\ncommand = "{tool}" {{text}}\n')
        env.update(AUTOPILOT_WAKE='type', AUTOPILOT_CONFIG=str(cfg), AUTOPILOT_WAKE_AFTER_S='1',
                   AUTOPILOT_WAKE_EVERY_S='3', AUTOPILOT_LINGER_S='60', AUTOPILOT_WAKE_CHECK_S='0.5')
        sid = self.drive('run', '--plan', 'p.md', env=env)['session']
        # nobody waits: the supervisor types the wake line into the owner's tab
        deadline = time.time() + 30
        while time.time() < deadline and not typed.exists():
            time.sleep(0.5)
        line = typed.read_text().splitlines()[0]
        self.assertTrue(line.startswith(f'autopilot: reattach to run {sid} ('), line)
        st = lambda: self.drive('watch-status', sid, env=env)['sessions'][0]
        while st()['alive']:
            time.sleep(0.5)
        # the run is over but unreported: the supervisor lingers and keeps waking
        state = json.loads((Path(env['AUTOPILOT_STATE_DIR']) / 'sessions' / sid / 'state.json').read_text())
        self.assertTrue(autopilot.pid_alive(state['pid']))
        deadline = time.time() + 20
        while 'ended (done)' not in typed.read_text() and time.time() < deadline:
            time.sleep(0.5)
        self.assertIn('ended (done)', typed.read_text())
        self.drive('wait', sid, '--catch-up', env=env)
        self.drive('mark-reported', sid, env=env)
        deadline = time.time() + 20
        while autopilot.pid_alive(state['pid']) and time.time() < deadline:
            time.sleep(0.5)
        self.assertFalse(autopilot.pid_alive(state['pid']))  # reported: the supervisor leaves

    def test_wake_commands_for_typeable_terminals(self):
        tmux = autopilot.wake_argv({'kind': 'tmux', 'handle': '%3', 'bin': 'tmux', 'socket': '/s'}, 'hi')
        self.assertEqual(tmux, ['tmux', '-S', '/s', 'send-keys', '-t', '%3', '-l', 'hi', ';',
                                'send-keys', '-t', '%3', 'Enter'])
        self.assertEqual(autopilot.wake_argv({'kind': 'wezterm', 'handle': '7', 'bin': 'wezterm'}, 'hi'),
                         ['wezterm', 'cli', 'send-text', '--pane-id', '7', '--no-paste', 'hi\r'])
        self.assertEqual(autopilot.wake_argv({'kind': 'kitty', 'handle': '2', 'bin': 'kitty',
                                              'to': 'unix:/k'}, 'hi'),
                         ['kitty', '--to', 'unix:/k', '@', 'send-text', '--match', 'id:2', 'hi\r'])
        orca = autopilot.wake_argv({'kind': 'orca', 'handle': 'h1', 'bin': 'orca'}, 'hi')
        self.assertEqual(orca, ['orca', 'terminal', 'send', '--terminal', 'h1', '--text', 'hi', '--enter'])
        with unittest.mock.patch.dict(os.environ, {'TMUX_PANE': '%9', 'TMUX': '/tmp/sock,1,0'}), \
                unittest.mock.patch('shutil.which', return_value='/usr/bin/tmux'):
            self.assertEqual(autopilot.own_tab(), {'kind': 'tmux', 'handle': '%9',
                                                   'bin': '/usr/bin/tmux', 'socket': '/tmp/sock'})

    def start_run(self, mode):
        plan = self.repo / 'p.md'
        plan.write_text('### Task 1: a\n- [ ] a\n')
        env = dict(self.env, FAKE_MODE=mode)
        return self.drive('run', '--plan', 'p.md', env=env), env

    def state(self, sid):
        return json.loads((Path(self.env['AUTOPILOT_STATE_DIR']) / 'sessions' / sid
                           / 'state.json').read_text())

    def test_runner_limit_wait_is_tracked_not_reported_as_errors(self):
        start, _ = self.start_run('limit-wait')
        self.assertIn('--wait=900s', start['command'])
        rep = self.wait(start['session'])
        self.assertEqual(rep['status'], 'running')
        self.assertEqual(rep['limit']['tool'], 'claude')
        self.assertEqual(rep['limit']['resets'], '2099-01-01T00:00:00Z')
        self.assertGreater(rep['limit']['resets_in_s'], 10 ** 8)
        rep = self.until(start['session'], 'done', rep['seq'])
        st = self.state(start['session'])
        kinds = [e['kind'] for e in st['events']]
        self.assertEqual(kinds.count('limit'), 1)
        self.assertIn('limit-retry', kinds)
        self.assertIn('limit-cleared', kinds)
        self.assertNotIn('error', kinds)
        self.assertEqual(st['restarts'], [])
        self.assertIsNone(rep['limit'])
        report = self.drive('report', start['session'])
        texts = [d.get('text', '') for d in report['decisions']]
        self.assertTrue(any(t.startswith('added --wait') for t in texts))
        self.assertTrue(any('waiting on a claude rate limit' in t for t in texts))
        self.assertTrue(any('rate limit cleared' in t for t in texts))

    def test_limit_exit_waits_for_reset_then_restarts(self):
        start, _ = self.start_run('limit-exit')
        rep = self.until(start['session'], 'done')
        st = self.state(start['session'])
        self.assertEqual([r['category'] for r in st['restarts']], ['limit'])
        limit_events = [e for e in st['events'] if e['kind'] == 'limit']
        self.assertEqual(limit_events[0]['handled_by'], 'autopilot')
        self.assertLessEqual(limit_events[0]['wait_s'], 5)

    def test_long_limit_is_handed_to_the_agent(self):
        start, _ = self.start_run('limit-long')
        rep = self.until(start['session'], 'failed')
        failed = [e for e in rep['new_events'] if e['kind'] == 'failed'][-1]
        self.assertEqual(failed['category'], 'limit')
        self.assertIn('needs a decision', failed['reason'])
        self.assertGreater(failed['resets_in_s'], 30000)

    def test_host_session_vars_do_not_reach_the_runner(self):
        env = dict(self.env, CLAUDECODE='1', CODEX_SANDBOX='seatbelt', CODEX_THREAD_ID='t')
        plan = self.repo / 'p.md'
        plan.write_text('### Task 1: a\n- [ ] a\n')
        sid = self.drive('run', '--plan', 'p.md', env=dict(env, FAKE_MODE='env'))['session']
        rep = self.until(sid, 'done')
        self.assertIn('LEAKED=', Path(rep['log']).read_text())
        self.assertNotIn('LEAKED=C', Path(rep['log']).read_text())

    def test_running_model_and_executor_are_reported(self):
        plan = self.repo / 'p.md'
        plan.write_text('### Task 1: a\n- [ ] a\n')
        env = dict(self.env, FAKE_MODE='codex-banner')
        start = self.drive('run', '--plan', 'p.md', '--', '--codex', env=env)
        sid = start['session']
        deadline, model = time.time() + 20, None
        while time.time() < deadline and (model or {}).get('effort') != 'high':
            model = self.state(sid).get('model')
            time.sleep(0.1)
        self.assertEqual(model, {'cli': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'})
        rep = self.until(sid, 'done')
        self.assertEqual(rep['executor'], 'codex')
        report = self.drive('report', sid)['sessions'][0]
        self.assertEqual(report['models_used'], {'codex/gpt-6.1-sol/high': 1,
                                                 'claude/claude-opus-5-5/medium': 1})
        self.assertEqual(rep['model'], {'cli': 'claude', 'model': 'claude-opus-5-5', 'effort': 'medium'})
        self.assertEqual(rep['phase'], 'external review')
        self.assertNotIn('error', [e['kind'] for e in self.state(sid)['events']])

    def test_reminder_then_extend_then_never(self):
        env = dict(self.env, AUTOPILOT_TIMEOUTS='question=4,question.remind=3')
        sid = self.drive('plan', '--desc', 'add caching', env=env)['session']
        rep = self.until(sid, 'waiting')
        self.assertEqual(rep['auto_answer'], 'question')
        # the open prompt does not wake wait again; the reminder does
        rep = self.wait(sid, rep['seq'])
        self.assertEqual([e['kind'] for e in rep['new_events']], ['reminder'])
        self.drive('extend', sid, '--never')
        time.sleep(4)
        rep = self.drive('wait', sid, '--timeout', '1', '--since', str(rep['seq']))
        self.assertEqual(rep['status'], 'waiting')
        self.assertIsNone(rep['auto_answer_in_s'])
        decisions = self.drive('report', sid)['decisions']
        self.assertEqual(decisions[-1]['text'], 'more time to answer: no auto-answer')
        self.drive('stop', sid)

    def test_run_refuses_a_plan_the_runner_cannot_use(self):
        (self.repo / 'design.md').write_text('# Design\n## 1. Decision\nCache it.\n')
        err = self.drive('run', '--plan', 'design.md')['error']
        self.assertIn('cannot use this plan', err)
        self.assertIn('--redesign', err)

    def until_phase(self, sid, phase):
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.state(sid).get('phase') == phase:
                return
            time.sleep(0.1)
        self.fail(f'never reached phase {phase}')

    def test_skip_review_breaks_the_running_external_review(self):
        start, _ = self.start_run('review')
        self.until_phase(start['session'], 'external review')
        self.assertEqual(self.drive('skip-review', start['session'])['when'], 'now')
        rep = self.until(start['session'], 'done')
        self.assertIn('ARGS-NONE' if os.name == 'nt' else 'manual break requested',
                      Path(rep['log']).read_text())
        texts = [d.get('text') for d in self.drive('report', start['session'])['decisions']]
        self.assertIn('skip the external code review', texts)

    def test_skip_review_is_queued_until_the_review_starts(self):
        start, _ = self.start_run('review-later')
        self.until_phase(start['session'], 'implementing tasks')
        self.assertEqual(self.drive('skip-review', start['session'])['when'],
                         'when the external review starts')
        rep = self.until(start['session'], 'done')
        self.assertIn('ARGS-NONE' if os.name == 'nt' else 'manual break requested',
                      Path(rep['log']).read_text())

    def test_codex_failing_in_review_restarts_without_it(self):
        start, _ = self.start_run('codex-down')
        rep = self.until(start['session'], 'done')
        self.assertIn('ARGS-NONE', Path(rep['log']).read_text())
        st = self.state(start['session'])
        self.assertEqual([r['category'] for r in st['restarts']], ['review-skipped'])

    def test_unusable_codex_review_is_skipped_at_start(self):
        env = dict(self.env, FAKE_MODE='env')
        env.pop('AUTOPILOT_CODEX_LOGIN_OK')
        env['PATH'] = system_tool_path()
        claude_only = self.fakebin / 'only-claude'
        claude_only.mkdir()
        for tool in self.fakebin.glob('claude*'):
            shutil.copy2(tool, claude_only / tool.name)
        env['PATH'] = str(claude_only) + os.pathsep + env['PATH']
        plan = self.repo / 'p.md'
        plan.write_text('### Task 1: a\n- [ ] a\n')
        start = self.drive('run', '--plan', 'p.md', env=env)
        self.assertIn('--external-review-tool=none', start['command'])
        self.until(start['session'], 'done')
        decisions = self.drive('report', start['session'])['decisions']
        skip = [d for d in decisions if d.get('text') == 'skipped the external code review for this run']
        self.assertEqual(skip[0]['reason'], 'codex is not installed')

    def test_only_codex_can_run_and_claude_plan_review_is_skipped(self):
        (self.repo / '.ralphex').mkdir()
        (self.repo / '.ralphex/config').write_text('executor = codex\n')
        (self.repo / '.autopilot').mkdir()
        (self.repo / '.autopilot/config').write_text('[timeouts]\ndecide = 1h\n')
        env = dict(self.env, FAKE_CLAUDE_LOGIN='0', FAKE_MODE='env')
        cap = self.drive('doctor', '--json', env=env)['reviews']
        self.assertTrue(cap['codex']['usable'])
        self.assertFalse(cap['claude']['usable'])
        self.assertIn('claude auth login', cap['claude']['reason'])
        # the runner offers no External reviewer under the Codex executor: Same agent reviews
        self.assertEqual(cap['plan_review']['recommended'], 'Same agent')
        self.assertEqual({o['option']: o['usable'] for o in cap['plan_review']['options']},
                         {'External reviewer': False, 'Same agent': True, 'Skip review': True})
        plan = self.repo / 'p.md'
        plan.write_text('### Task 1: a\n- [ ] a\n')
        self.commit_configs()
        start = self.drive('run', '--plan', 'p.md', env=env)
        self.assertEqual(start['executor'], 'codex')
        self.until(start['session'], 'done')

    def test_claude_unavailable_falls_back_at_start_without_changing_config(self):
        (self.repo / '.ralphex').mkdir()
        config = self.repo / '.ralphex/config'
        config.write_text('plan_model = opus:high\nreview_model = sonnet:medium\n')
        before = config.read_bytes()
        env = dict(self.env, FAKE_CLAUDE_LOGIN='0', FAKE_MODE='env')
        self.commit_configs()
        (self.repo / 'p.md').write_text('### Task 1: a\n- [ ] a\n')
        start = self.drive('run', '--plan', 'p.md', '--', '--task-model', 'opus:high', env=env)
        self.assertEqual(start['executor'], 'codex')
        self.assertIn('--codex', start['command'])
        self.assertIn('--task-model=:', start['command'])
        self.assertNotIn('opus', start['command'])
        self.until(start['session'], 'done')
        self.assertEqual(config.read_bytes(), before)
        decisions = self.drive('report', start['session'])['decisions']
        self.assertTrue(any('switched this session' in d.get('text', '') for d in decisions))

    def test_claude_disappearing_mid_run_falls_back_once_and_keeps_finished_steps(self):
        (self.repo / 'p.md').write_text('### Task 1: a\n- [x] done\n- [ ] remaining\n')
        start = self.drive('run', '--plan', 'p.md', env=dict(self.env, FAKE_MODE='claude-down'))
        rep = self.until(start['session'], 'done')
        self.assertEqual(rep['executor'], 'codex')
        self.assertIn('CODEX-FALLBACK', Path(rep['log']).read_text())
        self.assertEqual((self.repo / 'p.md').read_text().count('- [x]'), 2)
        self.assertEqual([r['category'] for r in self.state(start['session'])['restarts']],
                         ['executor-fallback'])

    def test_planning_falls_back_to_codex_and_stays_plan_only(self):
        start = self.drive('plan', '--desc', 'add caching',
                           env=dict(self.env, FAKE_CLAUDE_LOGIN='0',
                                    AUTOPILOT_TIMEOUTS='question=1,draft=1'))
        self.assertEqual(start['executor'], 'codex')
        rep = self.until(start['session'], 'done')
        self.assertIn('ANSWER-CONTINUE: n', Path(rep['log']).read_text())

    def test_fallback_respects_read_only_codex_and_missing_credentials(self):
        (self.repo / '.ralphex').mkdir()
        (self.repo / '.ralphex/config').write_text('codex_sandbox = read-only\n')
        env = dict(self.env, FAKE_CLAUDE_LOGIN='0')
        err = self.drive('plan', '--desc', 'cache', env=env)['error']
        self.assertIn('codex_sandbox is read-only', err)
        self.assertFalse((Path(self.env['AUTOPILOT_STATE_DIR']) / 'sessions').exists())
        env = dict(env, FAKE_CODEX_LOGIN='0')
        env.pop('AUTOPILOT_CODEX_LOGIN_OK')
        err = self.drive('plan', '--desc', 'cache', env=env)['error']
        self.assertIn('claude is not logged in', err)
        self.assertIn('autopilot-setup', err)

    def test_run_fatal_error_stops_without_restart(self):
        plan = self.repo / 'p.md'
        plan.write_text('### Task 1: a\n- [ ] a\n')
        env = dict(self.env, FAKE_FATAL='1')
        sid = self.drive('run', '--plan', 'p.md', env=env)['session']
        rep = self.until(sid, 'failed')
        self.assertEqual(rep['restarts'], 0)
        self.assertIn('unknown flag', rep['last_error'])


class HostPortability(unittest.TestCase):
    def test_windows_review_skip_restarts_with_an_explicit_review_flag_replaced(self):
        for flags in (['--external-review-tool=codex'], ['--external-review-tool', 'custom']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                (directory / 'session.json').write_text(json.dumps({
                    'repo': tmp, 'argv': ['ralphex.exe', 'plan.md', *flags]}))
                supervisor = autopilot.Supervisor(directory)
                try:
                    supervisor.state['skip_external'] = True
                    supervisor.proc = unittest.mock.Mock(pid=42)
                    supervisor.proc.poll.return_value = None
                    with unittest.mock.patch.object(autopilot, 'WINDOWS', True), \
                            unittest.mock.patch.object(autopilot, 'kill_tree') as kill:
                        supervisor.break_external_review()
                    self.assertTrue(supervisor.restart_now)
                    kill.assert_called_once_with(42)
                    with unittest.mock.patch.object(autopilot, 'WINDOWS', True), \
                            unittest.mock.patch.object(autopilot.subprocess, 'CREATE_NEW_PROCESS_GROUP',
                                                       512, create=True), \
                            unittest.mock.patch.object(autopilot.subprocess, 'Popen') as spawn, \
                            unittest.mock.patch.object(autopilot.threading, 'Thread'):
                        supervisor.spawn()
                    kwargs = spawn.call_args.kwargs
                    self.assertEqual(kwargs['args'],
                                     ['ralphex.exe', 'plan.md', '--external-review-tool=none'])
                    self.assertEqual(kwargs['env']['AUTOPILOT_PYTHON'], sys.executable)
                finally:
                    supervisor.log.close()

    def test_windows_pid_check_uses_the_pid_column(self):
        with unittest.mock.patch.object(autopilot, 'WINDOWS', True), \
                unittest.mock.patch.object(autopilot, 'run_quiet') as listing:
            listing.return_value = (0, '"python.exe","1234","Console","1","123 K"')
            self.assertFalse(autopilot.pid_alive(123))
            self.assertTrue(autopilot.pid_alive(1234))
            listing.return_value = (0, 'INFO: No tasks are running which match the specified criteria.')
            self.assertFalse(autopilot.pid_alive(1234))

    def test_windows_state_update_retries_a_temporary_reader_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.json'
            state.write_text('{"status": "old"}')
            replace = autopilot.os.replace
            calls = []
            def locked_once(source, dest):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError('reader holds the old file')
                return replace(source, dest)
            with unittest.mock.patch.object(autopilot, 'WINDOWS', True), \
                    unittest.mock.patch.object(autopilot.os, 'replace', side_effect=locked_once):
                autopilot.write_json(state, {'status': 'new'})
            self.assertEqual(json.loads(state.read_text()), {'status': 'new'})

    def test_windows_shims_quote_interpreter_and_use_valid_crlf(self):
        with tempfile.TemporaryDirectory(prefix='autopilot with spaces ') as tmp:
            with unittest.mock.patch.object(autopilot, 'WINDOWS', True):
                bindir = autopilot.write_shims(Path(tmp))
            for name in ('fzf', 'autopilot-editor'):
                text = (bindir / f'{name}.cmd').read_bytes()
                self.assertIn(f'"{sys.executable}" -X utf8'.encode('utf-8'), text)
                self.assertIn(b'\r\n', text)
                self.assertNotIn(b'\r\r\n', text)

    def test_windows_batch_clis_and_python_entrypoints(self):
        with unittest.mock.patch.object(autopilot, 'WINDOWS', True), \
                unittest.mock.patch.object(autopilot.shutil, 'which', return_value='codex.cmd'):
            self.assertEqual(autopilot.command_kwargs(['codex', 'login', 'status']),
                             {'args': ['codex.cmd', 'login', 'status'], 'shell': True})
        self.assertEqual(autopilot.command_kwargs(['helper.py', 'argument']),
                         {'args': [sys.executable, 'helper.py', 'argument']})

    def test_host_detection_hints(self):
        cases = [({'CLAUDECODE': '1'}, 'claude-code'), ({'CODEX_THREAD_ID': 't'}, 'codex'),
                 ({'GEMINI_CLI': '1'}, 'gemini-cli'), ({'OPENCODE': '1'}, 'opencode'), ({}, 'unknown')]
        for env, name in cases:
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(autopilot.detect_host()['name'], name)
        with unittest.mock.patch.dict(os.environ, {'CODEX_SANDBOX': 'seatbelt',
                                                   'CODEX_SANDBOX_NETWORK_DISABLED': '1'}, clear=True):
            host = autopilot.detect_host()
            self.assertTrue(host['sandboxed'] and host['network_disabled'])

    def test_state_falls_back_into_git_dir_when_user_dir_is_not_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with unittest.mock.patch.dict(os.environ, {'HOME': '/nonexistent-home',
                                                           'XDG_STATE_HOME': ''}, clear=False), \
                        unittest.mock.patch.object(autopilot, 'writable', return_value=False):
                    os.environ.pop('AUTOPILOT_STATE_DIR', None)
                    root = autopilot.state_root()
            finally:
                os.chdir(cwd)
            self.assertEqual(root.parts[-2:], ('.git', 'autopilot'))


class ExecutorSpecs(unittest.TestCase):
    def test_specs_must_fit_the_executor(self):
        pinned = {'plan_model': 'opus:high', 'task_model': ':', 'review_model': 'gpt-6.1-sol:max'}
        codex = autopilot.spec_problems('codex', pinned)
        self.assertTrue(any(p.startswith('plan_model = opus:high') for p in codex))
        self.assertTrue(any('review_model = gpt-6.1-sol:max: the runner drops a phase-level "max"' in p
                            for p in codex))
        claude = autopilot.spec_problems('claude', pinned)
        self.assertEqual(len(claude), 1)
        self.assertTrue(claude[0].startswith('review_model = gpt-6.1-sol:max'))
        self.assertEqual(autopilot.spec_problems('codex', {'plan_model': ':', 'task_model': ''}), [])

    def test_codex_efforts_follow_the_runner_and_the_catalog(self):
        # the runner passes "ultra" in a phase spec and a base "max" through to codex
        ok = {'plan_model': 'gpt-6.1-sol:ultra', 'task_model': ':', 'review_model': ':',
              'codex_reasoning_effort': 'max'}
        self.assertEqual(autopilot.spec_problems('codex', ok), [])
        catalog = {'codex': [{'name': 'gpt-6-luna', 'aliases': 'luna',
                              'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'about': ''},
                             {'name': 'gpt-6.1-sol', 'aliases': '',
                              'efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], 'about': ''}]}
        self.assertEqual(autopilot.spec_problems('codex', ok, catalog), [])
        bad = autopilot.spec_problems('codex', {'task_model': 'luna:ultra', 'plan_model': ':',
                                                'review_model': ':'}, catalog)
        self.assertEqual(len(bad), 1)
        self.assertIn('luna supports efforts low, medium, high, xhigh, max', bad[0])
        self.assertIn('not "ultra"', bad[0])
        base = autopilot.spec_problems('codex', {'codex_model': 'gpt-6-luna',
                                                 'codex_reasoning_effort': 'ultra'}, catalog)
        self.assertTrue(base and base[0].startswith('codex_reasoning_effort = ultra'))
        # an unknown or routed model is not judged
        self.assertEqual(autopilot.spec_problems('codex', {'task_model': ':ultra'}, catalog), [])


class RouterCatalog(unittest.TestCase):
    TEMPLATE = textwrap.dedent('''\
        # agrouter config (every key shows its default)
        # [cli.codex]
        # enabled = true
        # [model.gpt-6.1-sol]
        # cli = codex
        # efforts = low, medium, high, xhigh, max, ultra
        # [model.gpt-6-luna]
        # cli = codex
        # efforts = low, medium, high, xhigh, max
        # [model.claude-opus-5-5]
        # cli = claude
        # aliases = opus
        # efforts = low, medium, high, max
        ''')

    def test_partial_override_keeps_the_other_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            glob, repo = Path(tmp) / 'global', Path(tmp) / 'repo'
            glob.mkdir(); (repo / '.agrouter').mkdir(parents=True)
            (glob / 'config').write_text(self.TEMPLATE + '[model.gpt-6-luna]\nefforts = low, medium\n')
            (repo / '.agrouter/config').write_text('[model.gpt-6.1-sol]\nenabled = false\n')
            with unittest.mock.patch.dict(os.environ, {'AGROUTER_CONFIG_DIR': str(glob)}):
                models, _ = autopilot.router_catalog(str(repo))
            self.assertEqual([m['name'] for m in models['codex']], ['gpt-6-luna'])
            self.assertEqual(models['codex'][0]['efforts'], ['low', 'medium'])
            self.assertEqual([m['name'] for m in models['claude']], ['claude-opus-5-5'])


class ResponsePolicy(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'config'
            cfg.write_text('[timeouts]\ndecide = 2h\nquestion = 15m\n')
            with unittest.mock.patch.dict(os.environ, {'AUTOPILOT_CONFIG': str(cfg),
                                                       'AUTOPILOT_TIMEOUTS': 'question=never,draft=45m'}):
                policy = autopilot.load_policy()
        self.assertEqual(policy['decide']['timeout'], 7200)
        self.assertEqual(policy['decide']['remind_before'], 900)
        self.assertIsNone(policy['question']['timeout'])
        self.assertEqual(policy['draft']['timeout'], 2700)
        self.assertIsNone(policy['plan_pick']['timeout'])
        with unittest.mock.patch.dict(os.environ, {'AUTOPILOT_CONFIG': '/nonexistent',
                                                   'AUTOPILOT_TIMEOUTS': ''}):
            policy = autopilot.load_policy()
        self.assertEqual((policy['question']['timeout'], policy['decide']['timeout']), (600, 3600))

    def test_ask_timer_reminds_then_expires_and_cancels(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AUTOPILOT_STATE_DIR=tmp,
                       AUTOPILOT_TIMEOUTS='decide=3,decide.remind=2', AUTOPILOT_CONFIG='/none')
            def timer(*a):
                out = subprocess.run([sys.executable, str(DRIVER), 'ask-timer', *a], env=env,
                                     capture_output=True, text=True, check=True).stdout
                return json.loads(out)
            self.assertEqual(timer('start', '--id', 'impl', '--decision', 'decide')['state'], 'pending')
            self.assertEqual(timer('wait', '--id', 'impl')['state'], 'reminder')
            self.assertEqual(timer('wait', '--id', 'impl')['state'], 'expired')
            timer('start', '--id', 'impl2', '--decision', 'decide')
            timer('cancel', '--id', 'impl2')
            self.assertEqual(timer('wait', '--id', 'impl2')['state'], 'cancelled')
            timer('start', '--id', 'pick', '--decision', 'plan_pick')
            self.assertEqual(timer('wait', '--id', 'pick')['state'], 'never')
            timer('start', '--id', 'chunk', '--decision', 'decide', '--seconds', '1h')
            chunk = timer('wait', '--id', 'chunk', '--max', '1')
            self.assertEqual(chunk['state'], 'pending')
            self.assertGreater(chunk['left_s'], 3500)


class Reconfigure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo, glob = base / 'repo', base / 'global'
        (self.repo / 'docs/arch').mkdir(parents=True)
        glob.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, check=True)
        for name in ('AGENTS.md', 'README.md', 'docs/arch/a.md', 'docs/arch/b.md'):
            (self.repo / name).write_text('x')
        (glob / 'config').write_text('plan_model = opus:high\nwait_on_limit = 15m\n')
        (self.repo / '.ralphex').mkdir()
        (self.repo / '.ralphex/config').write_text(
            'claude_command = agrouter\n'
            'claude_args = exec claude --doc=AGENTS.md --doc=gone.md -- --verbose --output-format stream-json\n'
            'task_model = :\nreview_model = opus:\n')
        self.env = dict(os.environ, RALPHEX_CONFIG_DIR=str(glob), AGROUTER_CONFIG_DIR=str(glob / 'r'),
                        AUTOPILOT_STATE_DIR=str(base / 'state'), AUTOPILOT_CONFIG='/none')

    def ap(self, *args):
        res = subprocess.run([sys.executable, str(DRIVER), *args], cwd=self.repo, env=self.env,
                             capture_output=True, text=True)
        return json.loads(res.stdout)

    def test_summary_shows_sources_modes_and_issues(self):
        d = self.ap('config-summary')
        phases = {k: (v['value'], v['source'], v['mode']) for k, v in d['phases'].items()}
        self.assertEqual(phases, {'plan_model': ('opus:high', 'global', 'fixed'),
                                  'task_model': (':', 'local', 'routed'),
                                  'review_model': ('opus:', 'local', 'model pinned, effort routed')})
        self.assertEqual(d['limits']['wait_on_limit']['source'], 'global')
        self.assertEqual(d['docs']['claude_args'], ['AGENTS.md', 'gone.md'])
        self.assertTrue(any('gone.md' in i for i in d['issues']))
        self.assertEqual(d['files']['.ralphex/config'], 'untracked')

    def test_unset_returns_to_inherited_value(self):
        self.ap('config-set', '.ralphex/config', 'plan_model', '--value', ':')
        self.assertEqual(self.ap('config-summary')['phases']['plan_model']['source'], 'local')
        self.ap('config-unset', '.ralphex/config', 'plan_model')
        self.assertEqual(self.ap('config-summary')['phases']['plan_model']['value'], 'opus:high')

    def test_set_docs_preserves_and_refreshes_the_windows_wrapper_pair(self):
        installed = self.ap('install-wrapper', '--windows', '--docs', 'AGENTS.md')
        command = installed['codex_command']
        self.ap('config-set', '.ralphex/config', 'codex_command', '--value', command)
        self.ap('set-docs', '--docs', 'AGENTS.md', 'README.md', '--doc-dirs', 'docs/arch')
        wrapper = self.ap('config-summary')['docs']['wrapper']
        self.assertEqual(wrapper['path'], command)
        self.assertEqual(wrapper['style'], 'autopilot')
        self.assertEqual(wrapper['docs'], ['AGENTS.md', 'README.md'])
        self.assertEqual(wrapper['doc_dirs'], ['docs/arch'])

    def test_set_docs_syncs_claude_args_and_respects_hand_written_wrapper(self):
        hand = self.repo / '.ralphex/scripts/codex-agrouter.sh'
        hand.parent.mkdir()
        hand.write_text('docs=(\n  --doc="$root/AGENTS.md"\n)\nexec agrouter exec codex "${docs[@]}" -- "$@"\n')
        self.ap('config-set', '.ralphex/config', 'codex_command', '--value', '.ralphex/scripts/codex-agrouter.sh')
        self.assertEqual(self.ap('config-summary')['docs']['wrapper']['docs'], ['AGENTS.md'])
        out = self.ap('set-docs', '--docs', 'AGENTS.md', 'README.md', '--doc-dirs', 'docs/arch')
        self.assertEqual(out['claude_args'], 'exec claude --doc=AGENTS.md --doc=README.md '
                         '--doc=docs/arch/a.md --doc=docs/arch/b.md -- --verbose --output-format stream-json')
        self.assertIn('hand-written', out['wrapper'])
        self.assertIn('--doc="$root/AGENTS.md"', hand.read_text())
        self.ap('set-docs', '--docs', 'AGENTS.md', 'README.md', '--doc-dirs', 'docs/arch', '--replace-wrapper')
        w = self.ap('config-summary')['docs']['wrapper']
        self.assertEqual((w['style'], w['docs'], w['doc_dirs']),
                         ('autopilot', ['AGENTS.md', 'README.md'], ['docs/arch']))
        self.assertEqual(self.ap('config-summary')['issues'], [
            'the runner config (.ralphex/config) is partial (not from the template): '
            'run autopilot-setup to upgrade it with config-init --merge',
            'this repository has no router config (.agrouter/config): run autopilot-setup repo',
            'this repository has no autopilot config (.autopilot/config): run autopilot-setup repo'])
        (self.repo / '.agrouter').mkdir()
        (self.repo / '.agrouter/config').write_text(
            '# agrouter embedded defaults\n[agrouter]\nclassifier_provider = typesafe\n')
        (self.repo / '.autopilot').mkdir()
        (self.repo / '.autopilot/config').write_text(autopilot.autopilot_template())
        cfg = self.repo / '.ralphex/config'
        cfg.write_text('# ralphex configuration\n' + cfg.read_text())
        self.assertEqual(self.ap('config-summary')['issues'], [])
        self.assertIn('error', self.ap('set-docs', '--docs', 'nope.md'))

    def test_config_init_writes_full_router_and_autopilot_templates(self):
        fakebin = Path(self.tmp.name) / 'bin'
        fakebin.mkdir()
        python_tool(fakebin / 'agrouter', textwrap.dedent('''\
            import json, os, sys
            from pathlib import Path
            Path(os.environ['AGROUTER_CONFIG_DIR']).mkdir(parents=True, exist_ok=True)
            Path(os.environ['AGROUTER_CONFIG_DIR'], 'config').write_text(
                '# defaults\\n# [agrouter]\\n# timeout = 30s\\n# [classifier.typesafe]\\n'
                '# api_key = leaked\\n')
            Path(os.environ['PROBE']).write_text(json.dumps({
                'cwd': os.getcwd(), 'argv': sys.argv[1:],
                'key_env': 'AGROUTER_CLASSIFIER_API_KEY' in os.environ}))
            sys.exit(2)
            '''))
        probe = Path(self.tmp.name) / 'probe.json'
        self.env = dict(self.env, PATH=f"{fakebin}{os.pathsep}{self.env['PATH']}",
                        PROBE=str(probe), AGROUTER_CLASSIFIER_API_KEY='secret')
        out = self.ap('config-init', '--tool', 'agrouter')
        self.assertEqual((out['file'], out['created']), ('.agrouter/config', True))
        text = (self.repo / '.agrouter/config').read_text()
        self.assertIn('# timeout = 30s', text)
        self.assertNotIn('leaked', text)
        self.assertEqual(list(autopilot.ini_active(text)), [])
        seen = json.loads(probe.read_text())
        self.assertFalse(seen['key_env'])
        self.assertIn('--api-key=', seen['argv'])
        self.assertNotEqual(Path(seen['cwd']).resolve(), self.repo.resolve())
        self.assertFalse((Path(self.tmp.name) / 'global/r').exists())

        self.ap('config-init', '--tool', 'autopilot')
        cfg = self.repo / '.autopilot/config'
        text = cfg.read_text()
        self.assertEqual([s for s, _, _ in autopilot.ini_active(text)], [])
        for key in autopilot.POLICY:
            self.assertIn(f'# {key} = ', text)
        self.assertIn('# decide.remind = 15m', text)
        self.ap('config-set', '.autopilot/config', 'decide', '--section', 'timeouts', '--value', '2h')
        policy = self.ap('policy')
        self.assertEqual(policy['policy']['decide']['timeout'], 7200)
        self.assertEqual([Path(p).resolve() for p in policy['source']], [cfg.resolve()])
        self.assertNotIn('[review]', cfg.read_text())
        self.assertNotIn('[publish]', cfg.read_text())

    def test_merge_upgrades_a_partial_config_and_keeps_its_values(self):
        own = self.repo / '.autopilot/config'
        own.parent.mkdir()
        own.write_text('[review]\n# the team wants a second model on every plan\nplan = codex\n\n'
                       '[timeouts]\ndecide = 2h\n')
        self.assertEqual(autopilot.config_state(own, 'autopilot'), 'partial')
        issues = self.ap('config-summary')['issues']
        self.assertTrue(any('(.autopilot/config) is partial' in i for i in issues), issues)
        self.assertTrue(any('[review] plan, which autopilot no longer reads' in i
                            for i in issues), issues)
        kept = self.ap('config-init', '--tool', 'autopilot')
        self.assertEqual((kept['created'], kept['state']), (False, 'partial'))
        out = self.ap('config-init', '--tool', 'autopilot', '--merge')
        # the obsolete [review] key is dropped: the chat decides the plan review now
        self.assertEqual(out['merged'], ['[timeouts] decide'])
        text = own.read_text()
        self.assertEqual(autopilot.config_state(own, 'autopilot'), 'full')
        self.assertNotIn('plan = codex', text)
        self.assertIn('# decide: ', text)
        self.assertEqual(list(autopilot.ini_active(text)),
                         [('timeouts', 'decide', '2h')])
        backup = Path(out['backup'])
        self.assertIn('decide = 2h', backup.read_text())
        self.assertFalse(backup.resolve().is_relative_to(self.repo.resolve()))
        again = self.ap('config-init', '--tool', 'autopilot', '--merge')
        self.assertEqual((again['created'], again['state']), (False, 'full'))
        issues = self.ap('config-summary')['issues']
        self.assertFalse(any('.autopilot/config' in i for i in issues), issues)

    def test_router_config_without_provider_is_an_issue(self):
        router = self.repo / '.agrouter/config'
        router.parent.mkdir()
        router.write_text('# agrouter embedded defaults\n[agrouter]\ntimeout = 120s\n')
        issues = self.ap('config-summary')['issues']
        self.assertTrue(any('classifier_provider' in i for i in issues), issues)
        self.ap('config-set', '.agrouter/config', 'classifier_provider', '--section', 'agrouter',
                '--value', 'typesafe')
        issues = self.ap('config-summary')['issues']
        self.assertFalse(any('agrouter' in i for i in issues), issues)

    def test_secrets_never_go_into_repository_configs(self):
        res = subprocess.run([sys.executable, str(DRIVER), 'config-set', '.agrouter/config', 'api_key',
                              '--section', 'classifier.typesafe', '--value', 'K'],
                             cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse((self.repo / '.agrouter/config').exists())
        glob = Path(self.tmp.name) / 'global/r/config'
        self.ap('config-set', str(glob), 'api_key', '--section', 'classifier.typesafe', '--value', 'K')
        self.assertIn('api_key = K', glob.read_text())

    def test_set_docs_quotes_paths_the_way_the_runner_splits_them(self):
        (self.repo / 'docs/design notes').mkdir()
        (self.repo / 'docs/design notes/overview.md').write_text('x')
        out = self.ap('set-docs', '--docs', 'AGENTS.md', '--doc-dirs', 'docs/design notes')
        self.assertIn('--doc="docs/design notes/overview.md"', out['claude_args'])
        self.assertEqual(runner_split(out['claude_args'].split(' -- ')[0]),
                         ['exec', 'claude', '--doc=AGENTS.md', '--doc=docs/design notes/overview.md'])
        self.assertEqual(self.ap('config-summary')['docs']['claude_args'],
                         ['AGENTS.md', 'docs/design notes/overview.md'])
        # a second run replaces the quoted entry instead of keeping half of it
        again = self.ap('set-docs', '--docs', 'AGENTS.md', '--doc-dirs', 'docs/design notes')
        self.assertEqual(again['claude_args'], out['claude_args'])


class CheckPlan(unittest.TestCase):
    GOOD = """# Plan: cache
## Overview
Add a cache.
## Validation Commands
- `go test ./...`
### Task 1: Add store
- [x] write store
- [ ] add tests
### Task 2.5: Wire it
- [ ] wire
- [ ] run tests
```markdown
### Task 9: example inside a fence
- [ ] not a real box
```
"""

    def check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'plan.md'
            path.write_text(text)
            return autopilot.check_plan(path)

    def test_ready_plan_mirrors_runner_parser(self):
        r = self.check(self.GOOD)
        self.assertEqual((r['verdict'], r['status']), ('ready', 'in progress'))
        self.assertEqual([t['id'] for t in r['tasks']], ['1', '2.5'])
        self.assertEqual(r['checkboxes'], {'total': 4, 'done': 1})

    def test_complete_plan(self):
        r = self.check(self.GOOD.replace('- [ ]', '- [x]'))
        self.assertEqual(r['status'], 'complete')

    def test_redesign_cases(self):
        cases = {
            '# Design\n## 1. Decision\nWe will cache.\n## 2. Risks\nNone.\n': 'design or notes',
            '# Plan\n## Task 1: Store\n- [ ] a\n': 'do not use the runner',
            '# План\n### Задача 1: Хранилище\n- [ ] a\n': 'do not use the runner',
            '# Plan\n### Step 1 - store\n- [ ] a\n': 'do not use the runner',
            '# Plan\n### Task 1: Store\nJust prose.\n': 'without checkboxes',
        }
        for text, needle in cases.items():
            with self.subTest(text=text[:25]):
                r = self.check(text)
                self.assertEqual(r['verdict'], 'redesign')
                self.assertTrue(any(needle in p for p in r['problems']), r['problems'])

    def test_warnings_keep_the_plan_runnable(self):
        text = ('# Plan\n## Success criteria\n- [ ] fast\n### Task 1: Store\n- [ ] a\n'
                + '\n'.join('- [ ] step %d' % i for i in range(13)) + '\n')
        r = self.check(text)
        self.assertEqual(r['verdict'], 'warnings')
        joined = ' '.join(r['warnings'])
        self.assertIn('outside task sections', joined)
        self.assertIn('Validation Commands', joined)
        self.assertIn('very large tasks', joined)

    def test_missing_file(self):
        self.assertEqual(autopilot.check_plan('/nonexistent/plan.md')['verdict'], 'redesign')


class PlanRevision(unittest.TestCase):
    PLAN = '# Plan\n## Validation Commands\n- `make test`\n### Task 1: Store\n- [x] write\n- [ ] test it\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        (self.repo / 'docs/plans').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, check=True)
        self.plan = self.repo / 'docs/plans/p.md'
        self.plan.write_text(self.PLAN)
        self.env = dict(os.environ, AUTOPILOT_STATE_DIR=str(Path(self.tmp.name) / 'state'))

    def ap(self, *args):
        res = subprocess.run([sys.executable, str(DRIVER), *args], cwd=self.repo, env=self.env,
                             capture_output=True, text=True)
        return json.loads(res.stdout)

    def test_snapshot_edit_diff(self):
        self.assertIn('error', self.ap('plan-rev', 'diff', 'docs/plans/p.md'))
        self.assertEqual(self.ap('plan-rev', 'snapshot', 'docs/plans/p.md')['revision'], 1)
        self.plan.write_text(self.PLAN + '### Task 2: Cache\n- [ ] add cache\n- [ ] test cache\n')
        d = self.ap('plan-rev', 'diff', 'docs/plans/p.md')
        self.assertTrue(d['changed'])
        self.assertEqual((d['tasks_before'], d['tasks_after'], d['added'], d['removed']), (1, 2, 3, 0))
        self.assertEqual(d['checked_boxes_lost'], 0)
        self.assertEqual(d['check']['verdict'], 'ready')

    def test_lost_progress_is_reported(self):
        self.ap('plan-rev', 'snapshot', 'docs/plans/p.md')
        self.plan.write_text(self.PLAN.replace('- [x] write', '- [ ] write'))
        self.assertEqual(self.ap('plan-rev', 'diff', 'docs/plans/p.md')['checked_boxes_lost'], 1)

    def test_supersede_keeps_a_backup(self):
        new = self.repo / 'docs/plans/p2.md'
        new.write_text(self.PLAN)
        out = self.ap('plan-rev', 'supersede', 'docs/plans/p.md', '--by', 'docs/plans/p2.md')
        self.assertFalse(self.plan.exists())
        self.assertEqual(Path(out['backup']).read_text(), self.PLAN)
        self.assertIn('not committed', out['how'])
        # a committed plan is removed with git rm
        self.plan.write_text(self.PLAN)
        subprocess.run(['git', 'add', 'docs/plans/p.md'], cwd=self.repo, check=True)
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'p'],
                       cwd=self.repo, check=True)
        out = self.ap('plan-rev', 'supersede', 'docs/plans/p.md', '--by', 'docs/plans/p2.md')
        self.assertIn('git rm', out['how'])
        self.assertFalse(self.plan.exists())

    def test_replan_round_diffs_old_against_new(self):
        # the documented round: snapshot old, re-plan into a new file, supersede, diff the new one
        self.ap('plan-rev', 'snapshot', 'docs/plans/p.md')
        new = self.repo / 'docs/plans/p2.md'
        new.write_text(self.PLAN + '### Task 2: Cache\n- [ ] add cache\n- [ ] test cache\n')
        self.ap('plan-rev', 'supersede', 'docs/plans/p.md', '--by', 'docs/plans/p2.md')
        d = self.ap('plan-rev', 'diff', 'docs/plans/p2.md')
        self.assertTrue(d['changed'])
        self.assertEqual((d['tasks_before'], d['tasks_after'], d['added']), (1, 2, 3))


class StepsSummary(unittest.TestCase):
    def test_short_line_from_the_plan_steps(self):
        self.assertIsNone(autopilot.steps_summary([]))
        self.assertEqual(autopilot.steps_summary(['add `Cache` to `pkg/x.go`.', 'write tests;']),
                         'add Cache to pkg/x.go; write tests')
        long = ['step ' + 'x' * 120, 'two', 'three']
        out = autopilot.steps_summary(long, limit=100)
        self.assertTrue(out.startswith('step ') and '...; two' in out and out.endswith('(+1 more)'), out)


class PlanReviewPick(unittest.TestCase):
    def pick(self, external=True, same=True, executor='claude', revised=0):
        offered = {'External reviewer': (external, None if external else 'codex is not installed'),
                   'Same agent': (same, None if same else 'claude is logged out')}
        return autopilot.plan_review_pick(executor, offered, revised=revised)['recommended']

    def test_recommendation_keeps_the_flow_moving_with_the_best_usable_review(self):
        self.assertEqual(self.pick(), 'External reviewer')
        self.assertEqual(self.pick(external=False), 'Same agent')
        # the Codex executor: the runner offers no External reviewer
        self.assertEqual(self.pick(external=False, executor='codex'), 'Same agent')
        self.assertEqual(self.pick(external=False, same=False), 'Skip review')
        self.assertEqual(self.pick(revised=1), 'Skip review')

    def test_the_external_review_is_recommended_whatever_the_config_says(self):
        with unittest.mock.patch.object(autopilot, 'autopilot_config',
                                        return_value={'plan': 'off'}), \
                unittest.mock.patch.object(autopilot, 'codex_status', return_value=(True, None)), \
                unittest.mock.patch.object(autopilot, 'claude_status', return_value=(True, None)):
            plan = autopilot.review_capability('/nonexistent', {})['plan_review']
        self.assertNotIn('setting', plan)
        self.assertEqual((plan['recommended'], plan['external_tool']),
                         ('External reviewer', 'codex'))

    def test_runner_version_gates_the_native_review(self):
        self.assertEqual(autopilot.runner_version('ralphex v1.7.0-windows.4'), (1, 7, 0, 4))
        self.assertEqual(autopilot.runner_version('ralphex v1.7.0'), (1, 7, 0, 0))
        self.assertIsNone(autopilot.runner_version('ralphex (devel)'))
        self.assertTrue(autopilot.runner_version('ralphex v1.8.0') >= autopilot.PLAN_REVIEW_MIN)
        self.assertFalse(autopilot.runner_version('ralphex v1.7.0-windows.3')
                         >= autopilot.PLAN_REVIEW_MIN)


class ToolUpdates(unittest.TestCase):
    """Newer runner and router releases offered at the start, looked up once a day."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {
            'AUTOPILOT_STATE_DIR': self.tmp.name, 'AUTOPILOT_CONFIG': '/none'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('AUTOPILOT_LATEST', None)
        self.config = unittest.mock.patch.object(autopilot, 'autopilot_config', return_value={})
        self.config.start()
        self.addCleanup(self.config.stop)

    def info(self, cli='ralphex v1.7.0-windows.4', router='v0.6.0'):
        return {'cli': {'path': '/x/ralphex.windows', 'version': cli, 'name': 'ralphex.windows'},
                'router': {'path': '/x/agrouter' if router else None, 'version': router}}

    def test_only_older_installed_tools_get_an_update(self):
        latest = {'cli': 'v1.7.0-windows.5', 'router': 'v0.6.0'}
        updates = autopilot.pending_updates(self.info(), latest)
        self.assertEqual([(u['tool'], u['installed'], u['latest']) for u in updates],
                         [('ralphex.windows', 'v1.7.0-windows.4', 'v1.7.0-windows.5')])
        self.assertEqual(autopilot.pending_updates(self.info(cli='ralphex v1.8.0'), latest), [])
        self.assertEqual(autopilot.pending_updates(self.info(cli='ralphex (devel)'), latest), [])
        self.assertEqual(autopilot.pending_updates(self.info(router=None),
                                                   {'cli': None, 'router': 'v9.0.0'}), [])

    def test_lookup_is_cached_and_retried_sooner_after_a_failure(self):
        with unittest.mock.patch.object(autopilot, 'fetch_latest',
                                        return_value={'cli': 'v2.0.0', 'router': 'v1.0.0'}) as fetch:
            self.assertEqual(autopilot.latest_versions()[1], 'fresh')
            self.assertEqual(autopilot.latest_versions()[1], 'cached')
            self.assertEqual(autopilot.latest_versions(force=True)[1], 'fresh')
            self.assertEqual(fetch.call_count, 2)
        with unittest.mock.patch.object(autopilot, 'fetch_latest',
                                        return_value={'cli': None, 'router': None}) as fetch:
            autopilot.latest_versions(force=True)
            self.assertEqual(autopilot.latest_versions()[1], 'cached')
            cache = autopilot.read_update_cache()
            cache['checked'] -= autopilot.UPDATE_RETRY + 1
            autopilot.write_update_cache(cache)
            self.assertEqual(autopilot.latest_versions()[1], 'fresh')
            self.assertEqual(fetch.call_count, 2)

    def test_check_off_asks_nothing(self):
        self.config.stop()
        with unittest.mock.patch.object(autopilot, 'autopilot_config', return_value={'check': 'off'}), \
                unittest.mock.patch.object(autopilot, 'fetch_latest') as fetch:
            self.assertEqual(autopilot.latest_versions(), ({}, 'off', {}))
            fetch.assert_not_called()
        self.config.start()

    def test_skipped_release_is_not_offered_but_a_newer_one_is(self):
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        python_tool(bin_dir / 'agrouter', 'print("v0.6.0")\n')
        env = dict(os.environ, AUTOPILOT_LATEST='cli=v99.0.0-windows.1,router=v99.0.0',
                   PATH=os.pathsep.join([str(bin_dir), system_tool_path()]))
        def doctor(*flags):
            out = subprocess.run([sys.executable, str(autopilot.SCRIPT), 'doctor', '--json', *flags],
                                 capture_output=True, text=True, env=env, check=True).stdout
            return json.loads(out)
        first = doctor('--updates')
        self.assertIn('agrouter', [u['tool'] for u in first['updates']])
        doctor('--skip-updates')
        again = doctor('--updates')
        self.assertEqual((again['updates'], again['updates_check']), ([], 'cached'))
        env['AUTOPILOT_LATEST'] = 'cli=v99.0.0-windows.1,router=v99.1.0'
        newer = doctor('--latest')
        self.assertEqual([u['latest'] for u in newer['updates']], ['v99.1.0'])

    def test_install_method_from_the_path(self):
        self.assertEqual(autopilot.install_via('/opt/homebrew/bin/ralphex.windows'), 'brew')
        self.assertEqual(autopilot.install_via(str(Path.home() / '.local/bin/ralphex.windows')),
                         'script')
        with unittest.mock.patch.dict(os.environ, {'GOBIN': '/gobin'}):
            self.assertEqual(autopilot.install_via('/gobin/agrouter'), 'go')
        self.assertIsNone(autopilot.install_via('/usr/bin/agrouter'))
        self.assertEqual(autopilot.version_tag('ralphex v1.7.0-windows.4'), 'v1.7.0-windows.4')


class ImplementationPlace(unittest.TestCase):
    """Where the run works: a new worktree from the default branch, or here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'master')
        (self.repo / 'README.md').write_text('x')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'init')

    def place(self, where=None, cfg=None, plan=None):
        return autopilot.implementation_place(str(where or self.repo), cfg or {}, plan)

    def usable(self, place):
        return {o['option']: o['usable'] for o in place['options']}

    def test_planned_on_master_runs_in_a_new_worktree(self):
        p = self.place()
        self.assertEqual((p['checkout'], p['recommended']), ('main checkout', 'worktree'))
        self.assertEqual(p['options'][0]['run_args'], ['--new-worktree'])
        self.assertEqual(Path(p['options'][0]['path']).parent.resolve(), (self.repo.parent / 'workspaces/repo').resolve())
        self.assertEqual(self.usable(p), {'worktree': True, 'here': True})
        plan = self.repo / 'docs/plans/x.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('### Task 1: a\n- [ ] a\n')
        self.assertEqual(self.place(plan=plan)['dirty'], [])
        (self.repo / 'notes.txt').write_text('wip')
        (self.repo / 'README.md').write_text('changed')
        git(self.repo, 'mv', 'README.md', 'READ ME.md')
        self.assertEqual(sorted(self.place(plan=plan)['dirty']), ['READ ME.md', 'notes.txt'])
        p = self.place(plan=plan)
        self.assertEqual((p['recommended'], self.usable(p)['here']), ('worktree', False))
        self.assertIn('notes.txt', p['options'][1]['reason'])

    def test_inside_a_feature_worktree_the_run_stays_there(self):
        wt = Path(self.tmp.name) / 'wt'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'feature-x', str(wt))
        p = self.place(wt)
        self.assertEqual((p['checkout'], p['branch'], p['recommended']),
                         ('worktree', 'feature-x', 'here'))
        self.assertEqual(self.usable(p), {'here': True, 'worktree': False})
        self.assertIn('only from master', p['options'][1]['reason'])
        blocked = self.place(wt, {'use_worktree': 'true'})
        self.assertIsNone(blocked['recommended'])
        self.assertIn('use_worktree = false', blocked['why'])

    def test_feature_branch_in_the_main_checkout_stays_on_it(self):
        git(self.repo, 'checkout', '-qb', 'feature-y')
        p = self.place()
        self.assertEqual((p['recommended'], self.usable(p)['worktree']), ('here', False))

    def test_new_worktree_is_made_next_to_the_folder_with_plan_and_configs(self):
        plan = self.repo / 'docs/plans/20261116-add-cache.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('### Task 1: a\n- [ ] a\n')
        (self.repo / '.autopilot').mkdir()
        (self.repo / '.autopilot/config').write_text('[timeouts]\ndecide = 1h\n')
        (self.repo / 'notes.txt').write_text('wip')
        env = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
        with unittest.mock.patch.dict(os.environ, env):
            wt = autopilot.prepare_worktree(str(self.repo), plan, [])
        path = Path(wt['path'])
        self.assertEqual(path.resolve(), (self.repo.parent / 'workspaces/repo/Add-cache').resolve())
        self.assertEqual((wt['branch'], wt['base'], wt['reused']), ('add-cache', 'master', False))
        self.assertEqual(sorted(wt['copied']), ['.autopilot/config', 'docs/plans/20261116-add-cache.md'])
        self.assertTrue((path / 'docs/plans/20261116-add-cache.md').exists())
        self.assertFalse((path / 'notes.txt').exists())
        log = subprocess.check_output(['git', 'log', '--oneline', '-1'], cwd=path, text=True)
        self.assertIn('add plan: add-cache', log)
        # the source checkout stays on master, with its own files
        self.assertEqual(subprocess.check_output(['git', 'branch', '--show-current'],
                                                 cwd=self.repo, text=True).strip(), 'master')
        self.assertTrue((self.repo / 'notes.txt').exists())
        again = autopilot.prepare_worktree(str(self.repo), plan, [])
        self.assertEqual((again['reused'], Path(again['path']).resolve()), (True, path.resolve()))
        place = self.place(path)
        self.assertEqual((place['checkout'], place['recommended']), ('worktree', 'here'))

    def test_worktree_path_follows_the_workspaces_layout_or_the_config(self):
        wt = Path(self.tmp.name) / 'wt'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'side', str(wt))
        # from a linked worktree the layout still hangs off the main checkout's folder
        self.assertEqual(autopilot.sibling_path(str(wt), 'feature/x').resolve(),
                         (self.repo.parent / 'workspaces/repo/Feature-x').resolve())
        with unittest.mock.patch.object(autopilot, 'autopilot_config',
                                        return_value={'path': '{parent}/wt/{repo}-{branch}'}):
            self.assertEqual(autopilot.sibling_path(str(self.repo), 'add-cache').resolve(),
                             (self.repo.parent / 'wt/repo-add-cache').resolve())

    def test_run_with_a_new_worktree_starts_inside_it(self):
        plan = self.repo / 'docs/plans/20261116-add-cache.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('### Task 1: a\n- [ ] a\n')
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo)
        env = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
               'GIT_COMMITTER_EMAIL': 't@t', 'RALPHEX_CONFIG_DIR': str(Path(self.tmp.name) / 'none')}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch.object(autopilot, 'require_tools'), \
                unittest.mock.patch.object(autopilot, 'start_session') as start:
            args = autopilot.argparse.Namespace(plan='docs/plans/20261116-add-cache.md',
                                                cli_args=['--worktree', '--wait=900s'], force=False,
                                                max_restarts=1, flow='f', new_worktree=False,
                                                worktree_path=None)
            autopilot.cmd_run(args)
            started_in = Path(os.getcwd()).resolve()
        kind, target, cli_args, _, flow, extra = start.call_args.args
        self.assertEqual(started_in, (self.repo.parent / 'workspaces/repo/Add-cache').resolve())
        self.assertEqual((kind, target, cli_args), ('run', 'docs/plans/20261116-add-cache.md',
                                                    ['--wait=900s']))
        self.assertEqual(extra['source_repo'], str(self.repo.resolve()))
        self.assertEqual(autopilot.in_repo({'repo': 'x', 'source_repo': str(self.repo.resolve())},
                                           str(self.repo.resolve())), True)

    def test_run_refuses_a_worktree_the_runner_cannot_make(self):
        wt = Path(self.tmp.name) / 'wt'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'feature-z', str(wt))
        (wt / 'p.md').write_text('### Task 1: a\n- [ ] a\n')
        env = dict(os.environ, AUTOPILOT_STATE_DIR=str(Path(self.tmp.name) / 'state'),
                   RALPHEX_CONFIG_DIR=str(Path(self.tmp.name) / 'none'),
                   AUTOPILOT_CONFIG='/none')
        with unittest.mock.patch.dict(os.environ, env):
            res = subprocess.run([sys.executable, str(DRIVER), 'place', '--plan', 'p.md'],
                                 cwd=wt, capture_output=True, text=True)
        self.assertEqual(json.loads(res.stdout)['recommended'], 'here')
        with unittest.mock.patch.object(autopilot, 'require_tools'), \
                unittest.mock.patch.object(autopilot, 'start_session') as start, \
                unittest.mock.patch.object(autopilot, 'git_root', return_value=str(wt)), \
                unittest.mock.patch.object(autopilot, 'cli_config_paths', return_value=[]), \
                unittest.mock.patch('os.getcwd', return_value=str(wt)):
            args = autopilot.argparse.Namespace(plan=str(wt / 'p.md'), cli_args=['--worktree'],
                                                force=False, max_restarts=1, flow=None,
                                                new_worktree=False, worktree_path=None)
            with self.assertRaises(SystemExit), \
                    unittest.mock.patch('sys.stdout', new_callable=__import__('io').StringIO) as out:
                autopilot.cmd_run(args)
            self.assertIn('Recommended: here', out.getvalue())
            start.assert_not_called()
            args.cli_args = []
            autopilot.cmd_run(args)
            start.assert_called_once()


class Publish(unittest.TestCase):
    """Push the run's branch and open a pull request against the branch it started from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo, self.remote, fakebin = base / 'repo', base / 'remote.git', base / 'bin'
        fakebin.mkdir()
        git(base, 'init', '-q', '--bare', str(self.remote))
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'master')
        (self.repo / 'README.md').write_text('x')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'init')
        git(self.repo, 'remote', 'add', 'origin', 'https://github.com/o/r.git')
        git(self.repo, 'config', 'remote.origin.pushurl', str(self.remote))
        git(self.repo, 'config', 'url.' + str(self.remote) + '.insteadOf', 'https://github.com/o/r.git')
        git(self.repo, 'push', '-q', 'origin', 'master')
        self.calls = base / 'gh.jsonl'
        python_tool(fakebin / 'gh', textwrap.dedent(f'''\
            import json, os, sys
            with open({str(self.calls)!r}, 'a') as f:
                f.write(json.dumps(sys.argv[1:]) + '\\n')
            if sys.argv[1:3] == ['pr', 'view']:
                if os.environ.get('FAKE_PR_OPEN'):
                    print(json.dumps({{'url': 'https://github.com/o/r/pull/7', 'state': 'OPEN',
                                      'baseRefName': 'master'}}))
                    sys.exit(0)
                sys.exit(1)
            print('https://github.com/o/r/pull/8')
            '''))
        self.env = dict(os.environ, PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
                        AUTOPILOT_STATE_DIR=str(base / 'state'), AUTOPILOT_CONFIG='/none',
                        RALPHEX_CONFIG_DIR=str(base / 'none'))
        self.n = 0

    def session(self, start_branch, status='done', plan='docs/plans/20261116-add-cache.md'):
        self.n += 1
        sid = f'20261116-000000-run-{self.n:04d}'
        sdir = Path(self.env['AUTOPILOT_STATE_DIR']) / 'sessions' / sid
        sdir.mkdir(parents=True)
        (sdir / 'session.json').write_text(json.dumps({
            'kind': 'run', 'target': plan, 'repo': str(self.repo), 'argv': ['ralphex', plan],
            'start_branch': start_branch, 'flow': f'f{self.n}'}))
        (sdir / 'state.json').write_text(json.dumps({'status': status, 'events': []}))
        return sid

    def work(self, branch, start='master', worktree=True):
        where = self.repo
        if worktree:
            where = self.repo / '.ralphex/worktrees' / branch
            git(self.repo, 'worktree', 'add', '-q', '-b', branch, str(where), start)
        else:
            git(self.repo, 'checkout', '-qb', branch, start)
        (where / f'{branch}.txt').write_text('done')
        git(where, 'add', '.')
        git(where, 'commit', '-qm', f'work on {branch}')
        return where

    def publish(self, *args, env=None):
        res = subprocess.run([sys.executable, str(DRIVER), 'publish', *args], cwd=self.repo,
                             env=env or self.env, capture_output=True, text=True)
        return json.loads(res.stdout.strip().splitlines()[-1])

    def gh(self):
        return [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_worktree_run_from_master_opens_a_pr_into_master(self):
        folder = self.work('add-cache')
        # an old [publish] section is ignored: only the chat changes the mode (--mode, --draft)
        old = Path(self.tmp.name) / 'autopilot.conf'
        old.write_text('[publish]\nmode = off\ndraft = true\n')
        out = self.publish(self.session('master'), env=dict(self.env, AUTOPILOT_CONFIG=str(old)))
        self.assertIsNone(out['skipped'], out)
        self.assertEqual((out['branch'], out['base'], out['pushed'], out['commits']),
                         ('add-cache', 'master', True, 1))
        self.assertEqual(Path(out['folder']).resolve(), folder.resolve())
        self.assertEqual(out['pr'], {'url': 'https://github.com/o/r/pull/8', 'created': True,
                                     'draft': False, 'base': 'master'})
        create = self.gh()[-1]
        self.assertEqual(create[:2], ['pr', 'create'])
        self.assertEqual(create[create.index('--base') + 1], 'master')
        self.assertEqual(create[create.index('--head') + 1], 'add-cache')
        self.assertNotIn('--draft', create)
        self.assertIn('add-cache', subprocess.check_output(['git', 'ls-remote', '--heads', str(self.remote)], text=True))
        report = subprocess.run([sys.executable, str(DRIVER), 'report', '--flow', 'f1'],
                                cwd=self.repo, env=self.env, capture_output=True, text=True)
        rgit = json.loads(report.stdout)['git']
        self.assertEqual((rgit['branch'], len(rgit['commits'])), ('add-cache', 1))
        self.assertIn('Opened pull request', rgit['published'])

    def test_feature_worktree_targets_the_branch_it_was_created_from(self):
        git(self.repo, 'branch', 'develop')
        git(self.repo, 'push', '-q', 'origin', 'develop')
        self.work('feature-x', start='develop')
        out = self.publish(self.session('feature-x', status='failed'))
        self.assertEqual((out['branch'], out['base']), ('feature-x', 'develop'))
        self.assertIn('created from develop', out['base_why'])
        self.assertTrue(out['pr']['draft'])

    def test_base_missing_on_the_remote_falls_back_to_the_default_branch(self):
        git(self.repo, 'branch', 'local-only')
        self.work('feature-y', start='local-only')
        out = self.publish(self.session('feature-y'))
        self.assertEqual(out['base'], 'master')
        self.assertIn('not on origin', out['base_why'])

    def test_open_pr_is_reused_and_nothing_to_publish_is_skipped(self):
        self.work('add-cache')
        out = self.publish(self.session('master'), env=dict(self.env, FAKE_PR_OPEN='1'))
        self.assertEqual(out['pr'], {'url': 'https://github.com/o/r/pull/7', 'created': False,
                                     'base': 'master'})
        self.assertFalse(any(c[:2] == ['pr', 'create'] for c in self.gh()))
        empty = self.publish(self.session('master', plan='docs/plans/x-none.md'))
        self.assertIn('no branch', empty['skipped'])
        off = self.publish(self.session('master'), '--mode', 'off')
        self.assertEqual((off['pushed'], off['skipped']),
                         (False, 'publish --mode off: the user asked to keep the branch local'))


class ClaudeAvailability(unittest.TestCase):
    def test_missing_disabled_logged_out_unverified_and_usable(self):
        with unittest.mock.patch.object(autopilot.shutil, 'which', return_value=None):
            self.assertEqual(autopilot.claude_status(), (False, 'claude is not installed'))
        with unittest.mock.patch.object(autopilot.shutil, 'which', return_value='/bin/claude'), \
                unittest.mock.patch.object(autopilot, 'run_quiet') as auth:
            ok, reason = autopilot.claude_status({'cli.claude': {'enabled': 'false'}})
            self.assertFalse(ok)
            self.assertIn('disabled for routing', reason)
            auth.assert_not_called()
            for result, expected in [((0, '{"loggedIn": true}'), None),
                                     ((0, '{"loggedIn": false}'), 'not logged in'),
                                     ((1, '{"loggedIn": true}'), 'not logged in'),
                                     ((None, 'timeout'), 'could not be checked'),
                                     ((0, '[]'), 'not logged in')]:
                with self.subTest(result=result):
                    auth.return_value = result
                    ok, reason = autopilot.claude_status()
                    self.assertEqual(ok, expected is None)
                    if expected:
                        self.assertIn(expected, reason)
            auth.assert_called_with(['/bin/claude', 'auth', 'status', '--json'], timeout=20)

    def test_fallback_keeps_routing_and_requires_existing_codex_command(self):
        with tempfile.TemporaryDirectory() as repo, \
                unittest.mock.patch.object(autopilot.shutil, 'which', return_value='/bin/codex'):
            reason = autopilot.codex_fallback_blocker(repo, {'claude_command': 'agrouter'})
            self.assertIn('routing wrapper', reason)
            self.assertIsNone(autopilot.codex_fallback_blocker(repo, {}))
        with unittest.mock.patch.object(autopilot.shutil, 'which', return_value=None):
            self.assertIn('codex_command is missing',
                          autopilot.codex_fallback_blocker('/none', {'codex_command': 'missing'}))

    def test_fallback_preserves_explicit_external_review_requests(self):
        for flags in (['--external-only'], ['-c'], ['--external-review-tool=codex'],
                      ['--external-review-tool', 'custom']):
            with self.subTest(flags=flags):
                self.assertIn('requires Claude', autopilot.codex_fallback_blocker('/none', {}, flags))
        with unittest.mock.patch.object(autopilot.shutil, 'which', return_value='/bin/codex'):
            self.assertIsNone(autopilot.codex_fallback_blocker(
                '/none', {}, ['--external-review-tool=none']))


class WrapperInstall(unittest.TestCase):
    def test_windows_pair_routes_docs_arguments_stdin_and_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, fakebin = Path(tmp) / 'repo with spaces', Path(tmp) / 'bin'
            repo.mkdir(); fakebin.mkdir()
            (repo / 'docs/design notes').mkdir(parents=True)
            names = ["Agent's Guide.md", 'docs/design notes/架构.md']
            for name in names:
                (repo / name).write_text('document', encoding='utf-8')
            python_tool(fakebin / 'agrouter', textwrap.dedent('''\
                import json, os, sys
                print(json.dumps({'args': sys.argv[1:], 'prompt': sys.stdin.read()}))
                sys.exit(int(os.environ.get('FAKE_ROUTER_EXIT', '0')))
                '''))
            env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ['PATH'], PYTHONUTF8='1')
            result = subprocess.run([sys.executable, str(DRIVER), 'install-wrapper', '--windows',
                                     '--docs', names[0], '--doc-dirs', 'docs/design notes'],
                                    cwd=repo, env=env, capture_output=True, text=True,
                                    encoding='utf-8', check=True)
            command = json.loads(result.stdout)['codex_command']
            self.assertEqual(command, '.ralphex/scripts/codex-agrouter.cmd')
            launcher = (repo / command).read_bytes()
            self.assertNotIn(sys.executable.encode('utf-8'), launcher)
            self.assertIn(b'py -3 -X utf8', launcher)
            self.assertNotIn(b'__PYTHON__', launcher)
            self.assertNotIn(b'\r\r\n', launcher)
            wrapper = autopilot.parse_wrapper(str(repo), command)
            self.assertEqual(wrapper['style'], 'autopilot')
            self.assertTrue(wrapper['routes'])
            self.assertEqual(wrapper['docs'], names[:1])
            self.assertEqual(wrapper['doc_dirs'], ['docs/design notes'])
            payload = (repo / command).with_suffix('.py')
            # Exercise the actual CMD launcher on Windows, its Python payload elsewhere.
            argv = [str(repo / command)] if os.name == 'nt' else [sys.executable, str(payload)]
            flags = ['exec', '-c', 'model="x y"', '--json']
            prompt = 'Review the plan: שלום 世界\n'
            for exit_code in (0, 7):
                run_env = dict(env, FAKE_ROUTER_EXIT=str(exit_code))
                if exit_code:
                    run_env['AUTOPILOT_PYTHON'] = sys.executable
                res = subprocess.run(argv + flags, shell=os.name == 'nt', cwd=repo,
                                     env=run_env, input=prompt,
                                     capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(res.returncode, exit_code, res.stderr)
                captured = json.loads(res.stdout)
                self.assertEqual(captured['args'], ['exec', 'codex',
                    *[f'--doc={repo.resolve() / name}' for name in names], '--', *flags[1:]])
                self.assertEqual(captured['prompt'], prompt)
            with unittest.mock.patch.object(autopilot, 'WINDOWS', True):
                self.assertEqual(autopilot.command_kwargs([str(repo / command), *flags]),
                                 {'args': [sys.executable, str(payload), *flags]})

    def run_wrapper(self, docs, doc_dirs, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            repo, fakebin = Path(tmp) / 'repo', Path(tmp) / 'bin'
            (repo / 'docs/arch').mkdir(parents=True)
            fakebin.mkdir()
            for name in ('AGENTS.md', 'docs/arch/a.md', 'docs/arch/b.md', *extra):
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                (repo / name).write_text('x')
            fake = fakebin / 'agrouter'
            fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
            fake.chmod(0o755)
            res = subprocess.run([sys.executable, str(DRIVER), 'install-wrapper', '--docs', *docs,
                                  '--doc-dirs', *doc_dirs], cwd=repo, capture_output=True,
                                 text=True, check=True)
            cmd = json.loads(res.stdout)['codex_command']
            self.parsed = autopilot.parse_wrapper(str(repo), cmd)
            env = dict(os.environ, PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}")
            out = subprocess.run(['/bin/bash', str(repo / cmd), 'exec', '-c', 'model="x y"', '--json'],
                                 cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            roots = (str(repo.resolve()), str(repo))
            return [l.replace(roots[0], 'ROOT').replace(roots[1], 'ROOT')
                    for l in out.stdout.splitlines()]

    @unittest.skipIf(os.name == 'nt', 'POSIX wrapper')
    def test_docs_and_globbed_dirs_reach_router_before_native_args(self):
        self.assertEqual(self.run_wrapper(['AGENTS.md'], ['docs/arch']), [
            'exec', 'codex', '--doc=ROOT/AGENTS.md', '--doc=ROOT/docs/arch/a.md',
            '--doc=ROOT/docs/arch/b.md', '--', '-c', 'model="x y"', '--json'])

    @unittest.skipIf(os.name == 'nt', 'POSIX wrapper')
    def test_shell_characters_in_file_names_stay_literal(self):
        names = ['docs/$schema.md', "docs/it's `here`.md"]
        self.assertEqual(self.run_wrapper(names, ["docs/$dir's"], extra=["docs/$dir's/c.md"]), [
            'exec', 'codex', '--doc=ROOT/docs/$schema.md', "--doc=ROOT/docs/it's `here`.md",
            "--doc=ROOT/docs/$dir's/c.md", '--', '-c', 'model="x y"', '--json'])
        self.assertEqual((self.parsed['docs'], self.parsed['doc_dirs']),
                         (sorted(names), ["docs/$dir's"]))

    @unittest.skipIf(os.name == 'nt', 'POSIX wrapper')
    def test_no_docs_works_on_old_bash(self):
        self.assertEqual(self.run_wrapper([], []),
                         ['exec', 'codex', '--', '-c', 'model="x y"', '--json'])


class DocDrift(unittest.TestCase):
    def test_reports_missing_and_unlisted_docs_for_both_wrapper_styles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'docs/arch').mkdir(parents=True)
            for name in ('AGENTS.md', 'docs/arch/a.md', 'docs/arch/new.md'):
                (root / name).write_text('x')
            args = 'exec claude --doc=AGENTS.md --doc=docs/arch/a.md --doc=gone.md -- --verbose'
            for wrapper in ('docs=()\ndoc_dirs=(\n  "docs/arch"\n)\n',
                            'for doc in "$root"/docs/arch/*.md; do\n'):
                (root / 'w.sh').write_text(wrapper)
                self.assertEqual(autopilot.doc_drift(str(root), args, 'w.sh'),
                                 {'missing': ['gone.md'], 'unlisted': ['docs/arch/new.md']})
            synced = 'exec claude --doc=AGENTS.md --doc=docs/arch/a.md --doc=docs/arch/new.md --'
            self.assertIsNone(autopilot.doc_drift(str(root), synced, 'w.sh'))


class IniEditing(unittest.TestCase):
    TEMPLATE = textwrap.dedent('''\
        # [agrouter]
        # classifier_provider = typesafe

        # [classifier.typesafe]
        # url = https://example.test
        # api_key =

        # [classifier.openai]
        # api_key =
    ''')

    def test_uncomments_template_section_and_key(self):
        out = autopilot.ini_set(self.TEMPLATE, 'api_key', 'K1', 'classifier.typesafe')
        self.assertIn('[classifier.typesafe]\n# url = https://example.test\napi_key = K1', out)
        self.assertIn('# [classifier.openai]\n# api_key =', out)

    def test_replaces_active_key_and_appends_missing(self):
        text = '[agrouter]\ntimeout = 10s\n\n[cli.codex]\nenabled = true\n'
        out = autopilot.ini_set(text, 'timeout', '30s', 'agrouter')
        out = autopilot.ini_set(out, 'classifier_provider', 'openai', 'agrouter')
        self.assertIn('[agrouter]\ntimeout = 30s\nclassifier_provider = openai\n', out)
        out = autopilot.ini_set(out, 'api_key', 'K', 'classifier.openai')
        self.assertTrue(out.endswith('[classifier.openai]\napi_key = K\n'))

    def test_flat_file_without_sections(self):
        text = 'claude_command = claude\n# task_model =\n'
        out = autopilot.ini_set(text, 'task_model', 'opus:', None)
        out = autopilot.ini_set(out, 'claude_command', 'agrouter', None)
        self.assertEqual(out, 'claude_command = agrouter\ntask_model = opus:\n')

    def test_empty_value_has_no_trailing_space(self):
        out = autopilot.ini_set('# codex_model = gpt\n', 'codex_model', '', None)
        self.assertEqual(out, 'codex_model =\n')

    def test_comment_above_key_once(self):
        out = autopilot.ini_set('# task_model =\n', 'task_model', ':', None, 'routed by the classifier')
        out = autopilot.ini_set(out, 'task_model', ':', None, 'routed by the classifier')
        self.assertEqual(out, '# routed by the classifier\ntask_model = :\n')

    def test_template_copy_overrides_nothing_until_set(self):
        text = '# note\ncolor_task = #2e8b57\n# task_model =\n[sec]\nk = v\n'
        out = autopilot.comment_out_values(text)
        self.assertEqual(out, '# note\n# color_task = #2e8b57\n# task_model =\n[sec]\n# k = v\n')
        self.assertEqual(list(autopilot.ini_active(out)), [])

    def test_redaction(self):
        self.assertEqual(autopilot.redact('api_key', 'abc'), '<set>')
        self.assertEqual(autopilot.redact('notify_telegram_token', 'abc'), '<set>')
        self.assertEqual(autopilot.redact('task_model', 'opus'), 'opus')
        self.assertEqual(autopilot.redact('bytes_per_token', '2.5'), '2.5')
        self.assertEqual(autopilot.redact('request_tokens', '128000'), '128000')


if __name__ == '__main__':
    unittest.main()


DRAFT_MENU = ('\n━━━ Plan Draft ━━━\n# Plan: cache\n### Task 1: add\n- [ ] do it\n━━━━━━━━━━\n'
              'Review the plan draft\n  1) Accept\n  2) Revise\n  3) Interactive review\n'
              '  4) Reject\nEnter number (1-4): ')


class FakeProc:
    def __init__(self, rc=0):
        self.pid, self.rc = 0, rc
        self.stdin = unittest.mock.MagicMock()
        self.stdout = unittest.mock.MagicMock()
        self.returncode = rc

    def poll(self):
        return self.rc


class SupervisorUnits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sdir = Path(self.tmp.name)
        (self.sdir / 'inbox').mkdir()
        autopilot.write_json(self.sdir / 'session.json', {
            'kind': 'plan', 'target': 'add caching', 'repo': self.tmp.name,
            'argv': ['ralphex', '--plan', 'add caching'], 'policy': autopilot.load_policy()})
        self.sup = autopilot.Supervisor(self.sdir)
        self.addCleanup(self.sup.log.close)

    def test_polling_keeps_edits_until_a_new_draft_is_printed(self):
        self.sup.buf = DRAFT_MENU
        prompt = self.sup.detect_prompt()
        edited = Path(prompt['edited_file'])
        self.assertIn('# Plan: cache', edited.read_text())
        edited.write_text('MY USER EDIT\n')
        for _ in range(3):
            self.sup.detect_prompt()
        self.assertEqual(edited.read_text(), 'MY USER EDIT\n')
        # a revision prints the draft again: that one gets a fresh editable copy
        self.sup.buf += '2\nEnter revision feedback: x\n' + DRAFT_MENU
        self.sup.detect_prompt()
        self.assertIn('# Plan: cache', edited.read_text())

    def test_lines_split_across_reads_are_scanned_whole(self):
        self.sup.state['skip_external'] = True
        self.sup.proc = FakeProc(rc=None)
        chunks = [b'--- codex it', b'eration 1 ---\nagrouter: selection: {"cli":"codex",',
                  b'"model":"gpt-6.1-sol","effort":"high"}\n', b'tail without newline', b'']
        with unittest.mock.patch.object(autopilot.os, 'read', side_effect=chunks), \
                unittest.mock.patch.object(self.sup, 'break_external_review') as brk:
            self.sup.reader()
        self.assertEqual(self.sup.state['phase'], 'external review')
        brk.assert_called_once()
        self.assertEqual(self.sup.state['model'],
                         {'cli': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'})

    def test_a_restart_drops_the_open_prompt_and_its_recommendation(self):
        choice = {'kind': 'choice', 'question': 'Pick', 'options': ['A', 'B'], 'free_text': False}
        self.sup.handle_prompt(dict(choice))
        old = self.sup.state['waiting']
        autopilot.write_json(self.sdir / 'recommend.json', {
            'prompt_id': old['id'], 'send': '1\n', 'summary': 'A', 'reason': 'old'})
        self.sup.spawn = lambda: setattr(self.sup, 'proc', FakeProc(rc=1))
        self.sup.attempt()
        self.assertIsNone(self.sup.state['waiting'])
        self.assertIn('prompt-dropped', [e['kind'] for e in self.sup.state['events']])
        # the restarted runner asks the same question: it is a new prompt, open for answers
        self.sup.handle_prompt(dict(choice))
        new = self.sup.state['waiting']
        self.assertEqual(self.sup.state['status'], 'waiting')
        self.assertGreater(new['id'], old['id'])
        # the old recommendation is not sent into the new process when the deadline passes
        new['deadline'] = autopilot.now() - 1
        self.sup.proc = FakeProc(rc=None)
        self.sup.check_deadline()
        answered = [e for e in self.sup.state['events'] if e['kind'] == 'auto-answered']
        self.assertEqual(len(answered), 1)
        self.assertNotIn('recommended option', answered[0]['reason'])  # the safe default, not 'old'


def git(cwd, *args):
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', *args], cwd=cwd,
                   check=True, capture_output=True)


class WorktreeResume(unittest.TestCase):
    PLAN = '# Plan\n### Task 1: a\n- [ ] one\n### Task 2: b\n- [ ] two\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'master')
        (self.repo / 'README.md').write_text('x')
        git(self.repo, 'add', 'README.md')
        git(self.repo, 'commit', '-qm', 'init')
        # the source plan stays untracked on master, as after planning
        (self.repo / 'docs/plans').mkdir(parents=True)
        (self.repo / 'docs/plans/2026-11-14-cache.md').write_text(self.PLAN)
        self.sdir = base / 'session'
        (self.sdir / 'inbox').mkdir(parents=True)
        self.env = unittest.mock.patch.dict(os.environ, {
            'RALPHEX_CONFIG_DIR': str(base / 'no-global')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def supervisor(self, *flags):
        target = 'docs/plans/2026-11-14-cache.md'
        autopilot.write_json(self.sdir / 'session.json', {
            'kind': 'run', 'target': target, 'repo': str(self.repo),
            'argv': ['ralphex', '--no-color', target, *flags]})
        sup = autopilot.Supervisor(self.sdir)
        self.addCleanup(sup.log.close)
        return sup

    def make_branch_with_progress(self):
        """What a first --worktree attempt leaves: the branch with Task 1 ticked."""
        wt = self.repo / '.ralphex/worktrees/cache'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'cache', str(wt))
        (wt / 'docs/plans').mkdir(parents=True)
        (wt / 'docs/plans/2026-11-14-cache.md').write_text(self.PLAN.replace('- [ ] one', '- [x] one'))
        git(wt, 'add', '-A')
        git(wt, 'commit', '-qm', 'task 1')
        git(self.repo, 'worktree', 'remove', str(wt))  # the runner removes it when it exits

    def test_first_start_uses_the_runner_worktree(self):
        cwd, argv = self.supervisor('--worktree').launch()
        self.assertEqual((cwd, argv[-1]), (str(self.repo), '--worktree'))

    def test_restart_continues_the_branch_without_resetting_ticks(self):
        self.make_branch_with_progress()
        sup = self.supervisor('--worktree', '--wait=900s')
        cwd, argv = sup.launch()
        wt = self.repo / '.ralphex/worktrees/cache'
        self.assertEqual(cwd, str(wt))
        self.assertEqual(argv, ['ralphex', '--no-color', 'docs/plans/2026-11-14-cache.md', '--wait=900s'])
        self.assertIn('- [x] one', (wt / 'docs/plans/2026-11-14-cache.md').read_text())
        self.assertEqual(sup.plan_progress()['done'], 1)
        # the existing worktree is reused on the next restart
        self.assertEqual(sup.launch()[0], str(wt))

    def test_configured_worktree_mode_refuses_to_restart(self):
        self.make_branch_with_progress()
        (self.repo / '.ralphex').mkdir(exist_ok=True)
        (self.repo / '.ralphex/config').write_text('use_worktree = true\n')
        with self.assertRaises(autopilot.ResumeBlocked):
            self.supervisor().launch()


class ExecutionChecks(unittest.TestCase):
    def capability(self, repo, cfg, router=''):
        rdir = Path(repo) / 'router'
        rdir.mkdir(exist_ok=True)
        (rdir / 'config').write_text(router)
        with unittest.mock.patch.dict(os.environ, {'AGROUTER_CONFIG_DIR': str(rdir),
                                                   'AUTOPILOT_CODEX_LOGIN_OK': '1'}), \
                unittest.mock.patch.object(autopilot, 'tool_info',
                                           return_value={'version': 'codex-cli 0.169.0'}), \
                unittest.mock.patch.object(autopilot, 'claude_status', return_value=(True, None)), \
                unittest.mock.patch.object(autopilot.shutil, 'which',
                                           side_effect=lambda n: '/bin/codex' if n == 'codex' else None):
            return autopilot.review_capability(repo, cfg)['execution']

    def test_codex_executor_needs_a_runnable_command_and_a_writable_sandbox(self):
        with tempfile.TemporaryDirectory() as repo:
            base = {'executor': 'codex', 'plan_model': ':', 'task_model': ':', 'review_model': ':'}
            missing = self.capability(repo, dict(base, codex_command='.ralphex/scripts/missing.sh'))
            self.assertFalse(missing['usable'])
            self.assertIn('codex_command is missing', missing['reason'])
            readonly = self.capability(repo, dict(base, codex_sandbox='read-only'))
            self.assertFalse(readonly['usable'])
            self.assertIn('read-only', readonly['reason'])
            self.assertTrue(self.capability(repo, dict(base, codex_command='codex'))['usable'])

    def test_routing_switch_only_applies_to_routed_use(self):
        disabled = '[cli.codex]\nenabled = false\n'
        with tempfile.TemporaryDirectory() as repo:
            direct = {'executor': 'codex', 'codex_command': 'codex', 'task_model': 'gpt-6.1-sol:high'}
            self.assertTrue(self.capability(repo, direct, disabled)['usable'])
            wrapper = Path(repo) / '.ralphex/scripts/codex-agrouter.sh'
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text('# autopilot-wrapper\nexec agrouter exec codex -- "$@"\n')
            wrapper.chmod(0o755)
            routed = dict(direct, codex_command='.ralphex/scripts/codex-agrouter.sh')
            blocked = self.capability(repo, routed, disabled)
            self.assertFalse(blocked['usable'])
            self.assertIn('disabled for routing', blocked['reason'])
