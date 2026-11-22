#!/usr/bin/env python3
"""Drive the plan-and-execute CLI for the autopilot skills.

The CLI (ralphex) asks questions through a terminal picker and runs for hours.
Agents call tools without a terminal and cannot hold a session open, so this
helper starts the CLI under a detached supervisor, keeps its stdin open,
detects prompts, accepts answers through an inbox, restarts failed runs and
reports progress as compact JSON.

Commands:
  doctor [--json] [--latest|--updates|--skip-updates]  tools, versions, updates, configs
  plan  --desc TEXT [-- ARGS]         start interactive plan creation (with its plan review)
  plan-review PLAN [-- ARGS]          start the runner's review of an existing plan file
  run   --plan PATH [-- ARGS]         start plan execution with auto-restart
  wait  SESSION [--timeout S]         block until an event or a heartbeat
  answer SESSION ...                  answer the prompt the session waits on
  stop  SESSION                       stop the session and its process tree
  list  [--all]                       sessions for this repository
  config-set FILE KEY [...]           set one INI key (secrets via stdin)
  config-show FILE                    print active INI lines, secrets redacted
  catalog                             models and efforts known to the router
"""
import argparse
import ast
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

WINDOWS = os.name == 'nt'
SCRIPT = Path(__file__).resolve()
CLI_NAMES = ('ralphex.windows', 'ralphex')
ROUTER = 'agrouter'
OTHER = 'Other (type your own answer)'
DRAFT_OPTIONS = ['Accept', 'Revise', 'Interactive review', 'Reject']
# secret keys: api_key, *_token (not bytes_per_token), *secret*, *password*
SECRET_KEY = re.compile(r'(?i)(api_key|(?<!_per)_token$|^token$|secret|password|passwd)')
SECTION_RE = re.compile(r'^--- (.+) ---$')
TS_RE = re.compile(r'^\[\d\d-\d\d-\d\d \d\d:\d\d:\d\d\] ?')
# lines the CLI itself prints on trouble; model output mentioning "error" is ignored
ERROR_RE = re.compile(r"(?i)^(ERROR:|Failed:|panic:|fatal:|error:)|^agrouter: (?!selection:|warning:)|"
                      r"hit your (?:usage |session |weekly |org's monthly )?limit|"
                      r'API Error: \d{3}')
# broader net used only to pick the line that explains a failed attempt
EXIT_RE = re.compile(r'(?i)(\berror\b|failed|panic|exit status|limit|denied|not found)')
FATAL_RE = re.compile(r'(?i)(unknown flag|unknown option|no such file|not found in PATH|'
                      r'executable file not found|not a git repository|repository root|'
                      r'worktree already exists|not logged in|authentication|invalid api key|'
                      r'config(?:uration)? error|plan file .*not found|uncommitted changes|'
                      r'unfinished git operation|interactive plan creation is not available)')
# ralphex waiting on a rate limit itself (wait_on_limit / --wait)
LIMIT_WAIT_RE = re.compile(r'rate limit detected: "(.+?)" in (\S+) output, waiting (\S+) before retry')
# ralphex retrying a transient API error as a session timeout
TRANSIENT_RE = re.compile(r'transient (\S+) error detected: "(.+?)"')
# reset time in agrouter's quota line ("resets at 2026-10-13T15:00:00Z") or the CLI's own text
RESET_ISO_RE = re.compile(r'resets? at (\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:?\d\d)?)')
RESET_TEXT_RE = re.compile(r'(?i)\bresets? (?:at |on )?([^;|]{2,60})')
# lines that belong to a limit episode: limit wording only, so agrouter config errors and
# ordinary model text still count as normal output
LIMIT_LINE_RE = re.compile(r"(?i)(hit your (?:usage |session |weekly |org's monthly |individual spend )?"
                           r'limit|quota (?:exhausted|exceeded)|insufficient_quota|rate limit|'
                           r'API Error: 429|429 Too Many Requests|at capacity)')
# wording only the CLIs and the router print: may start an episode on its own
STRONG_LIMIT_RE = re.compile(r"(?i)(hit your (?:usage |session |weekly |org's monthly |individual "
                             r'spend )?limit|quota (?:exhausted|exceeded)|insufficient_quota|'
                             r'API Error: 429|429 Too Many Requests|Selected model is at capacity)')
DIAG_PREFIX_RE = re.compile(r'(?i)^(agrouter:|ERROR:|error: detected)')
# the runner's hint after a pattern error; neither a limit line nor new work
HELP_LINE_RE = re.compile(r"^run '.+' for more information")
# what is actually running: the router's selection line, or the codex executor's banner
SELECTION_RE = re.compile(r'^agrouter: selection: (\{.*\})')
ROUTER_WARNING_RE = re.compile(r'^agrouter: warning: (.+)')
# no output this long while running (not waiting on a prompt or a limit) is worth a look
STALL_S = int(os.environ.get('AUTOPILOT_STALL_S', 1200))
# a step without a milestone this long gets a "still running" line, so a slow review
# never looks like a stalled run
HEARTBEAT_S = float(os.environ.get('AUTOPILOT_HEARTBEAT_S', 1800))
PROGRESS_S = float(os.environ.get('AUTOPILOT_PROGRESS_S', 5))  # how often the plan is re-read
# waking the owner chat: after this long unwatched with news, then again every WAKE_EVERY_S;
# after the run ends the supervisor keeps trying for LINGER_S
WAKE_AFTER_S = float(os.environ.get('AUTOPILOT_WAKE_AFTER_S', 600))
WAKE_EVERY_S = float(os.environ.get('AUTOPILOT_WAKE_EVERY_S', 1200))
LINGER_S = float(os.environ.get('AUTOPILOT_LINGER_S', 6 * 3600))
WAKE_CHECK_S = float(os.environ.get('AUTOPILOT_WAKE_CHECK_S', 15))
WAKE_EVENTS = {'task-done', 'phase-start', 'restart', 'failed', 'done', 'stopped', 'stalled',
               'router-fallback', 'limit', 'limit-cleared', 'needs-input', 'auto-answered'}
BANNER_RE = re.compile(r'^(model|reasoning effort): (\S+)$')
CLAUDE_FAMILY = re.compile(r'(?i)^(opus|sonnet|fable|haiku|claude-)')
CODEX_FAMILY = re.compile(r'(?i)^(gpt-|o\d|codex)')
# codex itself cannot run (as opposed to a finding or a limit)
CODEX_DOWN_RE = re.compile(r'(?i)(codex[^\n]*(executable file not found|not found in PATH|no such file|'
                           r'not logged in|login|unauthori[sz]ed|authentication)|'
                           r'(not logged in|please run codex login))')
CLAUDE_DOWN_RE = re.compile(r'(?im)^(?:(?:ERROR:|error:|agrouter:)\s*claude[^\n]*'
                            r'(?:executable file not found|not found in PATH|not logged in|'
                            r'authentication failed|unauthori[sz]ed)|'
                            r'Not logged in(?:\b.*)?$|Please run claude auth login\b)')
PHASES = [
    (re.compile(r'^task iteration'), 'implementing tasks'),
    (re.compile(r'^claude review|^review \d'), 'code review'),
    (re.compile(r'^codex iteration|^custom review|evaluating codex findings'), 'external review'),
    (re.compile(r'^plan iteration'), 'planning'),
    (re.compile(r'^plan review round'), 'plan review'),
    (re.compile(r'finali[sz]e'), 'finalizing'),
]
# the runner's own plan review (ralphex.windows v1.7.0-windows.4+): its stable prompt texts
REVIEW_PICK_Q = 'Review the plan before implementation?'
REVIEW_APPLY_Q = 'Which findings should be applied?'
REVIEW_EXTERNAL, REVIEW_SAME, REVIEW_SKIP = 'External reviewer', 'Same agent', 'Skip review'
REVIEW_ALL, REVIEW_NONE = 'All findings', 'None'
PLAN_REVIEW_MIN = (1, 7, 0, 4)
# "  2. MAJOR: Task 3 - text" under "plan review findings:" (the indent is gone after clean_line)
REVIEW_FINDING_RE = re.compile(r'^(\d+)\.\s+(CRITICAL|MAJOR|MINOR):\s*(.*)$')
# lines that end the findings list: a section, the questions, a menu, the answer prompt
FINDINGS_END_RE = re.compile(r'^--- .+ ---$|' + re.escape(REVIEW_APPLY_Q) + '|'
                             + re.escape(REVIEW_PICK_Q) + r'|^Enter your answer|^\d+\)\s|'
                             r'^(plan review|plan revis|applying selected|SELECTED:)')
# the review loop's outcome lines: posted to the chat as plan review updates
REVIEW_LINE_RE = re.compile(
    r'^(plan review skipped|plan review found no issues|no findings selected, plan left unchanged|'
    r'applying selected findings to the plan|plan revised$|max plan review rounds|'
    r'warning: .*(plan review|plan revision|review round|read-only guard|valid findings selection)|'
    r'note: ralphex does not revert)')


# ---------------------------------------------------------------- utilities

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=None))


def die(msg, code=1):
    emit({'error': msg})
    sys.exit(code)


def now():
    return time.time()


def read_json(path, default=None):
    """The JSON in path, or default when it is missing or unreadable.

    A state file that exists but cannot be read is being replaced right now (on Windows the
    swap briefly denies access; elsewhere a reader can meet a half-written copy from a
    foreign tool): retry for a short time, so a reader never takes it for an empty state.
    """
    path = Path(path)
    for attempt in range(25):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return default
        except (OSError, ValueError):
            if attempt == 24 or not path.exists():
                return default
            time.sleep(0.02)
    return default


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows readers and virus scanners can briefly hold the previous state file.
            if not WINDOWS or attempt == 9:
                raise
            time.sleep(0.02)


# a detached supervisor has no console: without this flag every console program it starts
# (git, tasklist, codex ...) opens a window of its own for a moment
NO_WINDOW = {'creationflags': 0x08000000} if WINDOWS else {}  # CREATE_NO_WINDOW


def run_quiet(argv, timeout=15, cwd=None):
    try:
        res = subprocess.run(**command_kwargs(argv), capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=timeout,
                             cwd=cwd, stdin=subprocess.DEVNULL, **NO_WINDOW)
        return res.returncode, (res.stdout + res.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


def command_kwargs(argv):
    """Resolve Windows batch CLIs and invoke our Python helpers without file associations."""
    argv = list(map(str, argv))
    command = shutil.which(argv[0]) or argv[0]
    path = Path(command)
    if path.suffix.lower() == '.py':
        return {'args': [sys.executable, command, *argv[1:]]}
    if WINDOWS and path.suffix.lower() in ('.cmd', '.bat'):
        companion = path.with_suffix('.py')
        if path.name.lower() == 'codex-agrouter.cmd' and companion.is_file() \
                and WRAPPER_MARKER in companion.read_text(encoding='utf-8'):
            return {'args': [sys.executable, str(companion), *argv[1:]]}
        # npm installs codex/claude as batch launchers. Prompts stay on stdin.
        return {'args': [command, *argv[1:]], 'shell': True}
    return {'args': argv}


def find_cli():
    override = os.environ.get('AUTOPILOT_CLI')
    if override:
        return shutil.which(override) or override
    for name in CLI_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def git_root(start=None):
    code, out = run_quiet(['git', 'rev-parse', '--show-toplevel'], cwd=start)
    return str(Path(out).resolve()) if code == 0 and out else None


def writable(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f'.probe-{os.getpid()}'
        probe.write_text('')
        probe.unlink()
        return True
    except OSError:
        return False


def state_root():
    """User state dir; inside .git when a sandboxed host cannot write there."""
    base = os.environ.get('AUTOPILOT_STATE_DIR')
    if base:
        return Path(base)
    if WINDOWS:
        user = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'autopilot'
    else:
        user = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'autopilot'
    if writable(user):
        return user
    code, out = run_quiet(['git', 'rev-parse', '--absolute-git-dir'])
    if code == 0 and out:
        return Path(out) / 'autopilot'
    return user


# environment of the host agent's own session: never inherited by the runner's children,
# so a nested claude/codex does not think it runs inside the host's session or sandbox
HOST_SESSION_VARS = ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ID',
                     'CODEX_THREAD_ID', 'CODEX_SANDBOX', 'CODEX_SANDBOX_NETWORK_DISABLED',
                     'GEMINI_CLI', 'OPENCODE')


def detect_host():
    """Best-effort hint about the agent host running this script."""
    env = os.environ
    if env.get('CLAUDECODE') or env.get('CLAUDE_CODE_SESSION_ID'):
        name = 'claude-code'
    elif env.get('CODEX_THREAD_ID') or env.get('CODEX_SANDBOX'):
        name = 'codex'
    elif env.get('GEMINI_CLI'):
        name = 'gemini-cli'
    elif env.get('OPENCODE'):
        name = 'opencode'
    else:
        name = 'unknown'
    sandboxed = bool(env.get('CODEX_SANDBOX') or env.get('CODEX_SANDBOX_NETWORK_DISABLED'))
    return {'name': name, 'sandboxed': sandboxed,
            'network_disabled': bool(env.get('CODEX_SANDBOX_NETWORK_DISABLED'))}


def session_dir(ref):
    path = Path(ref)
    if path.is_dir() and (path / 'session.json').exists():
        return path
    candidate = state_root() / 'sessions' / ref
    if (candidate / 'session.json').exists():
        return candidate
    die(f'unknown session: {ref}')


def session_active(st):
    """The run is still going: its supervisor lives and has not ended (it may linger after the
    end to wake the chat, which does not count)."""
    return pid_alive(st.get('pid')) and st.get('status') not in ('done', 'failed', 'stopped')


def win_pid_alive(pid):
    """True/False from the Windows API without starting a process; None when it cannot tell."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def pid_alive(pid):
    if not pid:
        return False
    if WINDOWS:
        alive = win_pid_alive(pid)
        if alive is not None:
            return alive
        code, out = run_quiet(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'])
        return code == 0 and any(len(row) > 1 and row[1] == str(pid)
                                 for row in csv.reader(out.splitlines()))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_tree(pid, graceful=True):
    if not pid:
        return
    if WINDOWS:
        run_quiet(['taskkill', '/T', '/F', '/PID', str(pid)])
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    sigs = [signal.SIGINT, signal.SIGTERM, signal.SIGKILL] if graceful else [signal.SIGKILL]
    for sig in sigs:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        for _ in range(50):
            if not pid_alive(pid):
                return
            time.sleep(0.2)


def go_duration(text):
    """Seconds in a Go duration such as 15m0s or 1h30m; None when unparsable."""
    parts = re.findall(r'(\d+(?:\.\d+)?)(h|ms|m|s)', text or '')
    if not parts:
        return None
    unit = {'h': 3600, 'm': 60, 's': 1, 'ms': 0.001}
    return int(sum(float(n) * unit[u] for n, u in parts))


def parse_reset(line):
    """(epoch or None, display text or None) for a reset time in a limit line."""
    m = RESET_ISO_RE.search(line)
    if m:
        stamp = m.group(1).replace('Z', '+00:00')
        try:
            from datetime import datetime
            return datetime.fromisoformat(stamp).timestamp(), m.group(1)
        except ValueError:
            return None, m.group(1)
    m = RESET_TEXT_RE.search(line)
    return (None, m.group(1).strip()) if m else (None, None)


# How long to wait for the user before taking the recommended answer, and when to remind.
# None = never auto-answer. Override in ~/.config/autopilot/config [timeouts] (or
# AUTOPILOT_CONFIG) and with AUTOPILOT_TIMEOUTS="draft=45m,decide=2h,draft.remind=15m";
# values: 90s, 10m, 1h, never.
POLICY = {
    'question':  (600, None, 'planner clarifying question'),
    'choice':    (600, None, 'planner multiple choice'),
    'yesno':     (600, None, 'planner yes/no'),
    'draft':     (1800, 600, 'plan draft review'),
    'text':      (None, None, 'free-text prompt without a safe default'),
    'runtime':   (600, None, 'runner prompt during execution (the run is blocked)'),
    'decide':    (3600, 900, 'implement or stop after the plan explanation'),
    'run_start': (1800, 600, 'start a standalone run'),
    'limit':     (1800, None, 'long rate limit: switch executor or wait'),
    'recovery':  (900, None, 'recovery step that needs a choice'),
    'preflight': (600, None, 'preflight choice (worktree, spec override, doc drift, session)'),
    'plan_pick': (None, None, 'which plan to run when several exist'),
    'plan_check': (900, None, 'redesign a plan the runner cannot run well'),
    'plan_review': (1800, None, 'any changes to the finished plan (plan-only flow)'),
    'review_pick': (600, None, "the runner's plan review: External reviewer, Same agent or Skip review"),
    'review_apply': (900, None, "which of the runner's plan review findings to apply"),
}


def parse_duration(text):
    """Seconds for 90s / 10m / 1h / 1h30m / 600; None for never/off/none."""
    text = str(text).strip().lower()
    if text in ('never', 'off', 'none', ''):
        return None
    if text.isdigit():
        return int(text)
    value = go_duration(text)
    if value is None:
        raise ValueError(f'bad duration: {text}')
    return value


def autopilot_config_paths(repo=None):
    """Global then repository autopilot config; the repository file wins."""
    paths = [Path(os.environ.get('AUTOPILOT_CONFIG') or Path.home() / '.config/autopilot/config')]
    repo = repo or git_root()
    if repo:
        paths.append(Path(repo) / '.autopilot/config')
    return paths


def autopilot_config(section):
    """Merged [section] of ~/.config/autopilot/config (AUTOPILOT_CONFIG) and <repo>/.autopilot/config."""
    values = {}
    for cfg in autopilot_config_paths():
        if cfg.exists():
            for sec, key, value in ini_active(cfg.read_text(encoding='utf-8')):
                if sec == section:
                    values[key] = value
    return values


def load_policy():
    """{key: {'timeout': s|None, 'remind_before': s|None, 'about': str}} after overrides."""
    policy = {k: {'timeout': t, 'remind_before': r, 'about': a} for k, (t, r, a) in POLICY.items()}
    overrides = dict(autopilot_config('timeouts'))
    for item in os.environ.get('AUTOPILOT_TIMEOUTS', '').split(','):
        if '=' in item:
            key, value = item.split('=', 1)
            overrides[key.strip()] = value.strip()
    for key, value in overrides.items():
        if key.endswith('.remind') and key[:-7] in policy:
            policy[key[:-7]]['remind_before'] = parse_duration(value)
            continue
        if key in policy:
            policy[key]['timeout'] = parse_duration(value)
    for rule in policy.values():
        if rule['remind_before'] and (rule['timeout'] is None
                                      or rule['remind_before'] >= rule['timeout']):
            rule['remind_before'] = None
    return policy


def cmd_policy(args):
    emit({'policy': load_policy(),
          'source': [str(p) for p in autopilot_config_paths() if p.exists()]
          + (['AUTOPILOT_TIMEOUTS'] if os.environ.get('AUTOPILOT_TIMEOUTS') else [])})


def detach_kwargs():
    if WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
        return {'creationflags': flags}
    return {'start_new_session': True}


def clean_line(line):
    return TS_RE.sub('', line).strip()


# ---------------------------------------------------------------- INI files

HEADER_RE = re.compile(r'^\s*([#;]\s*)?\[([^\]]+)\]\s*$')


def _key_re(key):
    return re.compile(r'^\s*([#;]\s*)?' + re.escape(key) + r'\s*=')


def ini_set(text, key, value, section=None, comment=None):
    """Set key (optionally in [section]), reusing a commented template line.

    comment, when given, is written as full-line comments right above the key
    (inline comments would become part of the value) unless already present.
    """
    if not value:
        text = ini_set(text, key, '\0', section, comment)
        return text.replace(f'{key} = \0', f'{key} =')
    if comment:
        text = ini_set(text, key, value, section)
        lines = text.splitlines()
        start = lines.index(f'[{section}]') if section else 0
        target = next(i for i in range(start, len(lines))
                      if lines[i].strip() == f'{key} = {value}'.strip())
        notes = [f'# {c}'.rstrip() for c in comment.splitlines()]
        if lines[max(0, target - len(notes)):target] != notes:
            lines[target:target] = notes
        return '\n'.join(lines) + '\n'
    lines = text.splitlines()
    start, end = 0, len(lines)
    if section:
        start = None
        for i, line in enumerate(lines):
            m = HEADER_RE.match(line)
            if m and m.group(2).strip() == section and (start is None or not m.group(1)):
                start = i
                if not m.group(1):
                    break
        if start is None:
            if lines and lines[-1].strip():
                lines.append('')
            lines += [f'[{section}]', f'{key} = {value}']
            return '\n'.join(lines) + '\n'
        lines[start] = f'[{section}]'
        start += 1
        end = next((i for i in range(start, len(lines)) if HEADER_RE.match(lines[i])
                    and not HEADER_RE.match(lines[i]).group(1)), len(lines))
        # a commented header of another section also ends a template section
        end = next((i for i in range(start, end) if HEADER_RE.match(lines[i])), end)
    else:
        end = next((i for i, l in enumerate(lines) if HEADER_RE.match(l)
                    and not HEADER_RE.match(l).group(1)), len(lines))
    pattern = _key_re(key)
    active = [i for i in range(start, end) if pattern.match(lines[i])
              and not pattern.match(lines[i]).group(1)]
    commented = [i for i in range(start, end) if pattern.match(lines[i])]
    target = active[-1] if active else (commented[-1] if commented else None)
    if target is None:
        insert = end
        while insert > start and not lines[insert - 1].strip():
            insert -= 1
        lines.insert(insert, f'{key} = {value}')
    else:
        lines[target] = f'{key} = {value}'
    return '\n'.join(lines) + '\n'


def ini_active(text):
    """Yield (section, key, value) for active lines."""
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        m = HEADER_RE.match(line)
        if m:
            section = m.group(2).strip()
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            yield section, key.strip(), value.strip()


def redact(key, value):
    return '<set>' if value and SECRET_KEY.search(key) else value


def cmd_config_set(args):
    path = Path(args.file).expanduser()
    if args.value_stdin:
        value = sys.stdin.readline().strip()
    else:
        value = args.value if args.value is not None else ''
    if args.value_stdin and not value:
        die('no value on stdin')
    repo = git_root()
    if value and SECRET_KEY.search(args.key) and repo and \
            path.resolve().is_relative_to(Path(repo).resolve()):
        die(f'refusing to write the secret {args.key} into {path}: repository configs are '
            'committed; store it in the global config or the environment')
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.private and not WINDOWS:
        os.chmod(path.parent, 0o700)
    path.write_text(ini_set(text, args.key, value, args.section, args.comment),
                    encoding='utf-8')
    if args.private and not WINDOWS:
        os.chmod(path, 0o600)
    where = f'[{args.section}] ' if args.section else ''
    emit({'ok': True, 'file': str(path), 'set': f'{where}{args.key}',
          'value': redact(args.key, value) if not args.value_stdin else '<set>'})


def comment_out_values(text):
    """Comment every active key so a local copy overrides nothing until set."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        active = stripped and stripped[0] not in '#;' and '=' in stripped \
            and not HEADER_RE.match(stripped)
        out.append(f'# {line}' if active else line)
    return '\n'.join(out) + '\n'


CONFIG_DEST = {'ralphex': '.ralphex/config', 'agrouter': '.agrouter/config',
               'autopilot': '.autopilot/config'}
# a line each tool's template starts with: a file without it was written by hand (partial)
TEMPLATE_MARK = {'ralphex': '# ralphex configuration', 'agrouter': '# agrouter embedded defaults',
                 'autopilot': '# autopilot repository config'}


def config_state(path, tool):
    """missing, partial (hand-written, no template) or full (made from the tool's template)."""
    path = Path(path)
    if not path.exists():
        return 'missing'
    head = path.read_text(encoding='utf-8', errors='replace').splitlines()[:5]
    return 'full' if any(l.strip().startswith(TEMPLATE_MARK[tool]) for l in head) else 'partial'


def active_with_comments(text):
    """[(section, key, value, comment)] for active keys; comment = the # lines right above."""
    out, section, notes = [], None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            notes = []
            continue
        if line[0] in '#;':
            notes.append(line.lstrip('#;').strip())
            continue
        m = HEADER_RE.match(line)
        if m:
            section, notes = m.group(2).strip(), []
            continue
        if '=' in line:
            key, value = line.split('=', 1)
            out.append((section, key.strip(), value.strip(), '\n'.join(notes) or None))
        notes = []
    return out


def runner_template():
    cli = find_cli()
    if not cli:
        die('not installed: ralphex.windows; run the autopilot-setup skill first')
    tmp = state_root() / f'dump-{secrets.token_hex(4)}'
    try:
        code, out = run_quiet([cli, '--dump-defaults', str(tmp)], timeout=60)
        src = tmp / 'config'
        if code != 0 or not src.exists():
            die(f'--dump-defaults failed: {out[-300:]}')
        return src.read_text(encoding='utf-8')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def router_template():
    """The router's commented embedded defaults, as it writes them for a new global config.

    agrouter installs that template on its first run into an empty AGROUTER_CONFIG_DIR. Run it
    there, from an empty directory and with no key, so it reads no user config, sends no
    classifier request (it fails with classifier_no_key) and never touches the real global file.
    """
    if not shutil.which(ROUTER):
        die('not installed: agrouter; run the autopilot-setup skill first')
    tmp = state_root() / f'router-{secrets.token_hex(4)}'
    (tmp / 'cwd').mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != 'AGROUTER_CLASSIFIER_API_KEY'}
    env['AGROUTER_CONFIG_DIR'] = str(tmp / 'global')
    try:
        try:
            res = subprocess.run(**command_kwargs([ROUTER, '--api-key=', '-p', 'template']),
                                 capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=60, cwd=tmp / 'cwd', env=env,
                                 stdin=subprocess.DEVNULL)
            out = (res.stdout + res.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            out = str(exc)
        src = tmp / 'global' / 'config'
        if not src.exists():
            die(f'agrouter wrote no config template (upgrade agrouter): {out[-300:]}')
        text = src.read_text(encoding='utf-8')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # a template holds no secret; clear any key value anyway before it reaches a repository
    return re.sub(r'(?m)^(\s*[#;]?\s*api_key\s*=).*$', r'\1', text)


def autopilot_template():
    lines = ['# autopilot repository config: committed, shared by everyone who runs autopilot here.',
             '# Layering: built-in defaults < ~/.config/autopilot/config (AUTOPILOT_CONFIG)',
             '# < this file < AUTOPILOT_TIMEOUTS. Comments must be on their own lines.',
             '', '[updates]',
             '# check: how often the skills look for newer ralphex.windows and agrouter',
             '# releases on GitHub at their start and offer an upgrade (24h, 1h, 0 = every',
             '# start, off = never)',
             '# check = 24h',
             '', '[compact]',
             '# mode: at the end of planning, type /compact into the chat before the run',
             '# starts, then wake it to start the run (type), or keep the context (off)',
             '# mode = type',
             '', '[worktree]',
             '# path: where a new worktree for a run goes (the workspaces layout by default).',
             '#   {parent} the folder that holds this repository, {repo} its name,',
             '#   {branch} the run branch, {Name} the branch with a capital first letter',
             '# path = {parent}/workspaces/{repo}/{Name}',
             '# branch: the run branch name. {slug} the plan file name without its date,',
             '#   {ticket} a ticket id (ABC-123: given, or found in the current branch or the',
             '#   plan), {summary} the plan title. path knows {slug}, {ticket}, {summary} too.',
             '#   Example: branch = feature/{ticket}/{slug}, path = {parent}/wt/{ticket}-{summary}',
             '# branch = {slug}',
             '', '[timeouts]',
             '# Response time per decision: 90s, 10m, 1h, 1h30m or never. After it the',
             '# recommended answer is taken and logged. <key>.remind sets a reminder before it.']
    for key, (timeout, remind, about) in POLICY.items():
        lines.append(f'# {key}: {about}')
        lines.append(f'# {key} = {fmt_duration(timeout)}')
        if remind:
            lines.append(f'# {key}.remind = {fmt_duration(remind)}')
    return '\n'.join(lines) + '\n'


def fmt_duration(seconds):
    if seconds is None:
        return 'never'
    if seconds % 3600 == 0:
        return f'{seconds // 3600}h'
    if seconds % 60 == 0:
        return f'{seconds // 60}m'
    return f'{seconds}s'


# sections autopilot no longer reads: the chat decides these per run
OBSOLETE_SECTIONS = ('review', 'publish')


def cmd_config_init(args):
    """Write a tool's full commented default config into the repository (nothing active yet).

    --merge upgrades a partial (hand-written) file: the template is written and every active
    key of the old file is set again in it, with the comment lines that stood above it.
    """
    dest = Path(args.dest or CONFIG_DEST[args.tool])
    state = config_state(dest, args.tool)
    if state != 'missing' and not args.force and not (args.merge and state == 'partial'):
        emit({'ok': True, 'tool': args.tool, 'file': str(dest), 'created': False,
              'state': state})
        return
    kept = [item for item in active_with_comments(dest.read_text(encoding='utf-8'))
            if not (args.tool == 'autopilot' and item[0] in OBSOLETE_SECTIONS)] \
        if state == 'partial' and args.merge else []
    text = comment_out_values({'ralphex': runner_template, 'agrouter': router_template,
                               'autopilot': autopilot_template}[args.tool]())
    for section, key, value, comment in kept:
        text = ini_set(text, key, value, section, comment)
    if kept:
        # outside the repository, so the old file is never committed by accident
        backup = state_root() / 'config-backups' / \
            f'{dest.parent.name}-{dest.name}-{time.strftime("%Y%m%d-%H%M%S")}'
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest, backup)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding='utf-8')
    emit({'ok': True, 'tool': args.tool, 'file': str(dest), 'created': True,
          'state': 'full', 'merged': [f'[{sec}] {k}' if sec else k for sec, k, _, _ in kept],
          'backup': str(backup) if kept else None})


def sh_literal(value):
    """A POSIX shell word that is exactly value: always single-quoted."""
    return "'" + value.replace("'", "'\\''") + "'"


def cmd_install_wrapper(args):
    """Install the Codex-through-router wrapper into .ralphex/scripts/."""
    assets = SCRIPT.parents[2] / 'autopilot-setup' / 'assets'
    dest_dir = Path('.ralphex/scripts')
    dest_dir.mkdir(parents=True, exist_ok=True)
    windows = args.windows or WINDOWS
    names = ['codex-agrouter.cmd', 'codex-agrouter.py'] if windows else ['codex-agrouter.sh']
    for name in names:
        dest = dest_dir / name
        if dest.exists() and not args.force:
            die(f'{dest} exists; pass --force to replace it')
    for name in names:
        text = (assets / name).read_text(encoding='utf-8')
        if name.endswith('.sh'):
            # single quotes: bash expands nothing inside, so "$" or "`" in a file name stays literal
            text = text.replace('__DOCS__', '\n'.join(f'  {sh_literal(d)}' for d in args.docs)) \
                       .replace('__DOC_DIRS__', '\n'.join(f'  {sh_literal(d)}' for d in args.doc_dirs))
            text = re.sub(r'\(\n\n\)', '()', text)
        elif name.endswith('.py'):
            text = text.replace('DOCS = []', f'DOCS = {args.docs!r}', 1) \
                       .replace('DOC_DIRS = []', f'DOC_DIRS = {args.doc_dirs!r}', 1)
        dest = dest_dir / name
        with dest.open('w', encoding='utf-8', newline='') as handle:
            handle.write(text.replace('\r\n', '\n').replace('\n', '\r\n')
                         if name.endswith('.cmd') else text)
        if name.endswith('.sh'):
            dest.chmod(0o755)
    command = str(dest_dir / names[0]).replace('\\', '/')
    emit({'ok': True, 'files': [str(dest_dir / n) for n in names], 'codex_command': command})


def cmd_config_show(args):
    path = Path(args.file).expanduser()
    if not path.exists():
        die(f'missing: {path}')
    out, last = [], object()
    for section, key, value in ini_active(path.read_text(encoding='utf-8')):
        if section != last and section:
            out.append(f'[{section}]')
        last = section
        out.append(f'{key} = {redact(key, value)}')
    print('\n'.join(out) if out else '(no active settings: template only)')


# ---------------------------------------------------------------- doctor

def router_config_paths(repo):
    base = os.environ.get('AGROUTER_CONFIG_DIR')
    glob = Path(base) if base else Path.home() / '.config/agrouter'
    paths = [glob / 'config']
    if repo:
        paths.append(Path(repo) / '.agrouter/config')
    return paths


def cli_config_paths(repo):
    base = os.environ.get('RALPHEX_CONFIG_DIR')
    glob = Path(base) if base else Path.home() / '.config/ralphex'
    paths = [glob / 'config']
    if repo:
        paths.append(Path(repo) / '.ralphex/config')
    return paths


def merged(paths):
    data = {}
    for path in paths:
        if path.exists():
            for section, key, value in ini_active(path.read_text(encoding='utf-8')):
                data.setdefault(section or '', {})[key] = value
    return data


def latest_release(repo, contains=None, timeout=10):
    url = f'https://api.github.com/repos/{repo}/releases?per_page=10'
    req = urllib.request.Request(url, headers={'User-Agent': 'autopilot-skill',
                                               'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for rel in json.loads(resp.read().decode()):
                tag = rel.get('tag_name', '')
                if rel.get('draft') or (contains and contains not in tag):
                    continue
                return tag
    except (OSError, ValueError):
        return None
    return None


def tool_info(name):
    path = shutil.which(name)
    if not path:
        return {'path': None, 'version': None}
    code, out = run_quiet([path, '--version'])
    return {'path': path, 'version': out.splitlines()[0] if out else None}


# where the newest releases come from: doctor key -> (GitHub repository, tag filter)
UPDATE_SOURCES = {'cli': ('svtlv/ralphex', '-windows'), 'router': ('svtlv/agrouter', None)}
UPDATE_INTERVAL = '24h'   # [updates] check: how long a lookup stays fresh; off disables it
UPDATE_RETRY = 3600       # a failed lookup (offline, GitHub limit) is retried after an hour


def install_via(path):
    """How a tool was installed, from where it lives: brew, go, script or None (unknown)."""
    if not path:
        return None
    where, real = Path(path), str(Path(path).resolve()).replace('\\', '/')
    if '/Caskroom/' in real or '/Cellar/' in real or str(where).startswith(
            ('/opt/homebrew/', '/home/linuxbrew/')):
        return 'brew'
    gobin = os.environ.get('GOBIN') or Path(os.environ.get('GOPATH') or Path.home() / 'go') / 'bin'
    if where.parent == Path(gobin):
        return 'go'
    if where.parent == Path.home() / '.local/bin':
        return 'script'
    return None


def other_copies(name, first):
    """Other copies of a command later on PATH: an upgrade must replace the first one."""
    seen, out = {os.path.normcase(str(Path(first).resolve()))} if first else set(), []
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        found = folder and shutil.which(name, path=folder)
        key = found and os.path.normcase(str(Path(found).resolve()))
        if found and key not in seen:
            seen.add(key)
            out.append(found)
    return out


def version_tag(text):
    """'v1.7.0-windows.4' from 'ralphex v1.7.0-windows.4'; the text itself when no version is in it."""
    m = re.search(r'v?\d+\.\d+\.\d+(?:-[\w.]+)?', text or '')
    return m.group(0) if m else text


def update_cache_path():
    return state_root() / 'updates.json'


def read_update_cache():
    try:
        data = json.loads(update_cache_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_update_cache(data):
    path = update_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(data, indent=1), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass  # the check still works, it just runs again next time


def fetch_latest():
    """{'cli': tag|None, 'router': tag|None}; AUTOPILOT_LATEST="cli=v1,router=v2" stands in for GitHub."""
    fake = os.environ.get('AUTOPILOT_LATEST')
    if fake is not None:
        pairs = dict(item.split('=', 1) for item in fake.split(',') if '=' in item)
        return {key: pairs.get(key) or None for key in UPDATE_SOURCES}
    found = {}
    threads = [threading.Thread(target=lambda k=k, r=r, c=c: found.__setitem__(
        k, latest_release(r, c, timeout=5)), daemon=True) for k, (r, c) in UPDATE_SOURCES.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(12)
    return {key: found.get(key) for key in UPDATE_SOURCES}


def latest_versions(force=False):
    """(latest tags, how they were got: fresh / cached / off, skipped tags per tool).

    The lookup runs at most once per [updates] check interval (24h by default), so each start
    of a skill costs no GitHub request; force (doctor --latest) always asks GitHub.
    """
    cache = read_update_cache()
    skipped = cache.get('skipped') or {}
    try:
        interval = parse_duration(autopilot_config('updates').get('check', UPDATE_INTERVAL))
    except ValueError:
        interval = parse_duration(UPDATE_INTERVAL)
    if interval is None and not force:
        return {}, 'off', skipped
    latest = cache.get('latest') or {}
    complete = all(latest.get(key) for key in UPDATE_SOURCES)
    fresh_for = interval if complete else min(interval or 0, UPDATE_RETRY)
    if not force and latest and time.time() - cache.get('checked', 0) < fresh_for:
        return latest, 'cached', skipped
    latest = fetch_latest()
    cache.update(checked=time.time(), latest=latest)
    write_update_cache(cache)
    return latest, 'fresh', skipped


def pending_updates(info, latest):
    """Installed tools with a newer release: one entry per tool, newest tag and how to reach it."""
    out = []
    for key, name in (('cli', info['cli'].get('name') or CLI_NAMES[0]), ('router', ROUTER)):
        tool, tag = info.get(key) or {}, latest.get(key)
        have, want = runner_version(tool.get('version')), runner_version(tag)
        if not tool.get('path') or have is None or want is None or want <= have:
            continue
        out.append({'tool': name, 'key': key, 'installed': version_tag(tool['version']),
                    'latest': tag, 'path': tool['path'], 'via': install_via(tool['path']),
                    'other_copies': other_copies(name, tool['path'])})
    return out


def skip_updates(updates):
    """Do not offer these releases again; a newer release is offered as usual."""
    cache = read_update_cache()
    skipped = cache.get('skipped') or {}
    for item in updates:
        skipped[item['tool']] = item['latest']
    cache['skipped'] = skipped
    write_update_cache(cache)


WRAPPER_MARKER = 'autopilot-wrapper'


DOC_ARG_RE = re.compile(r'--doc=("(?:[^"\\]|\\.)*"|\S+)')


def runner_arg(value):
    """One value for a runner *_args line, quoted the way the runner's splitArgs reads it."""
    if not re.search(r'[\s"\'\\]', value):
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def claude_docs(claude_args):
    """--doc paths in claude_args before its ' -- ' separator."""
    docs = []
    for raw in DOC_ARG_RE.findall((claude_args or '').split(' -- ')[0]):
        if raw.startswith('"') and raw.endswith('"') and len(raw) > 1:
            raw = re.sub(r'\\(.)', r'\1', raw[1:-1])
        docs.append(raw)
    return docs


def parse_wrapper(repo, codex_command):
    """What the Codex wrapper feeds the router: docs, folders, and who wrote it."""
    wrapper = Path(repo) / codex_command if codex_command else None
    if not wrapper or not wrapper.is_file():
        return None
    text = wrapper.read_text(encoding='utf-8', errors='replace')
    if wrapper.suffix.lower() == '.cmd':
        companion = wrapper.with_suffix('.py')
        if companion.is_file() and companion.name in text:
            python_text = companion.read_text(encoding='utf-8', errors='replace')
            if WRAPPER_MARKER in python_text:
                text = python_text
    docs, folders = [], []
    literal = {'docs': [], 'doc_dirs': []}  # single-quoted words: exact paths, "$" included
    # template style: docs=( 'x' ) doc_dirs=( 'docs/x' ) (older: "x"); hand-written:
    # --doc="$root/x", "$root"/docs/x/*.md
    for name, target in (('docs', docs), ('doc_dirs', folders)):
        block = re.search(name + r"=\(((?:[^)'\\]|\\.|'[^']*')*)\)", text)
        if block:
            target += re.findall(r'"([^"$]+)"', block.group(1))
            for word in re.findall(r"(?:'[^']*'\\')*'[^']*'", block.group(1)):
                literal[name].append(re.sub(r"'\\''", "'", word)[1:-1])
    docs += re.findall(r'--doc="\$root/([^"]+)"', text)
    folders += re.findall(r'\$root"?/([^"*\s]+)/\*\.md', text)
    for name, target in (('DOCS', docs), ('DOC_DIRS', folders)):
        block = re.search(name + r' = (\[[^\n]*\])', text)
        if block:
            try:
                values = ast.literal_eval(block.group(1))
                target += [v for v in values if isinstance(v, str)]
            except (ValueError, SyntaxError):
                pass
    # quoted fragments of hand-written --doc="$root/x" entries are not paths
    # (nor are shell variables such as the template's own "$root/$doc" loop line)
    def is_path(d):
        return bool(d) and '--doc' not in d and '$' not in d
    docs = [d for d in docs if is_path(d)] + literal['docs']
    folders = [d for d in folders if is_path(d)] + literal['doc_dirs']
    return {'path': codex_command, 'style': 'autopilot' if WRAPPER_MARKER in text else 'hand-written',
            'routes': ROUTER in text, 'docs': sorted(set(docs)), 'doc_dirs': sorted(set(folders))}


def doc_drift(repo, claude_args, codex_command):
    """Compare claude_args --doc files with the disk and the wrapper's doc folders."""
    listed = claude_docs(claude_args)
    root = Path(repo)
    missing = [d for d in listed if not (root / d).is_file()]
    wrapper = parse_wrapper(repo, codex_command) or {'doc_dirs': []}
    found = sorted({str(p.relative_to(root)).replace('\\', '/')
                    for f in wrapper['doc_dirs'] for p in (root / f).glob('*.md')})
    unlisted = [f for f in found if f not in listed]
    return {'missing': missing, 'unlisted': unlisted} if missing or unlisted else None


def spec_mode(spec):
    if spec is None or spec == '':
        return 'not set (falls back)'
    model, _, effort = spec.partition(':')
    if not model and not effort:
        return 'routed'
    if model and not effort:
        return 'model pinned, effort routed' if ':' in spec else 'model pinned'
    if effort and not model:
        return 'effort pinned, model routed'
    return 'fixed'


def tracked_state(repo, path):
    if not (Path(repo) / path).exists():
        return 'missing'
    code, out = run_quiet(['git', 'status', '--porcelain', '--', path], cwd=repo)
    if code != 0:
        return 'unknown'
    if not out:
        return 'committed'
    return 'untracked' if out.startswith('??') else 'modified'


# the runner's own plan grammar (pkg/plan/parse.go)
TASK_HEADER_RE = re.compile(r'^###\s+(?:Task|Iteration)\s+([^:]+?):\s*(.*)$')
CHECKBOX_RE = re.compile(r'^\s*-\s+\[([ xX])\]\s*(.*)$')
FENCE_RE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
# headings that look like tasks but do not match the runner's format
NEAR_TASK_RE = re.compile(r'(?i)^#{1,6}\s*(?:task|iteration|step|phase|stage|milestone|'
                          r'задача|этап|шаг)\s*[\w.]+\s*[:.)-]?')
TEST_WORDS_RE = re.compile(r'(?i)\b(test|tests|testing|spec|lint|verify|validation|pytest|'
                           r'go test|npm test|dotnet test|cargo test)\b')


def check_plan(path, plans_dir='docs/plans'):
    """Can the runner execute this file well? Mirrors the runner's parser."""
    path = Path(path)
    if not path.is_file():
        return {'verdict': 'redesign', 'problems': [f'file not found: {path}'], 'warnings': [],
                'tasks': []}
    text = path.read_text(encoding='utf-8', errors='replace')
    tasks, task, title, fence = [], None, None, None
    stray, near, validation = [], [], False
    for number, line in enumerate(text.splitlines(), 1):
        m = FENCE_RE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        hm = TASK_HEADER_RE.match(line)
        if hm:
            task = {'id': hm.group(1).strip(), 'title': hm.group(2).strip(), 'boxes': 0,
                    'done': 0, 'tests': False, 'line': number}
            tasks.append(task)
            continue
        if re.match(r'^#\s+', line) and title is None:
            title = line[1:].strip()
        if re.match(r'(?i)^##\s+validation commands', line):
            validation = True
        is_h2 = line.startswith('##') and not line.startswith('###')
        is_h1 = line.startswith('#') and not line.startswith('##') and title is not None \
            and line[1:].strip() != title
        if task and (is_h2 or is_h1):
            task = None
        if NEAR_TASK_RE.match(line) and not hm:
            near.append({'line': number, 'text': line.strip()[:80]})
        cm = CHECKBOX_RE.match(line)
        if cm:
            if task:
                task['boxes'] += 1
                task['done'] += cm.group(1) != ' '
                task['tests'] = task['tests'] or bool(TEST_WORDS_RE.search(cm.group(2)))
            else:
                stray.append({'line': number, 'checked': cm.group(1) != ' ',
                              'text': cm.group(2)[:60]})
    problems, warnings = [], []
    if not tasks:
        if near:
            problems.append(f'{len(near)} task-like headings do not use the runner\'s format '
                            f'"### Task N: Title" (e.g. line {near[0]["line"]}: {near[0]["text"]})')
        else:
            problems.append('no "### Task N:" sections: this reads as a design or notes '
                            'document, not an executable plan')
    empty = [t for t in tasks if not t['boxes']]
    if empty:
        problems.append('tasks without checkboxes never finish: ' +
                        ', '.join(f"Task {t['id']}" for t in empty[:5]))
    unchecked_stray = [x for x in stray if not x['checked']]
    if unchecked_stray:
        warnings.append(f'{len(unchecked_stray)} unchecked checkboxes outside task sections '
                        f'(line {unchecked_stray[0]["line"]}) cause extra loop iterations')
    if tasks and near:
        warnings.append(f'{len(near)} headings look like tasks but are not "### Task N:" and '
                        f'will be skipped (line {near[0]["line"]}: {near[0]["text"]})')
    if tasks and not validation and not any(t['tests'] for t in tasks):
        warnings.append('no "## Validation Commands" section and no test steps: nothing '
                        'checks the work between tasks')
    untested = [t for t in tasks if t['boxes'] and not t['tests']]
    if tasks and validation is False and untested and len(untested) < len(tasks):
        warnings.append('tasks without a test step: ' +
                        ', '.join(f"Task {t['id']}" for t in untested[:5]))
    big = [t for t in tasks if t['boxes'] > 12]
    if big:
        warnings.append('very large tasks (more than 12 steps) are better split: ' +
                        ', '.join(f"Task {t['id']} ({t['boxes']})" for t in big[:5]))
    if len(tasks) > 20:
        warnings.append(f'{len(tasks)} tasks: consider splitting into several plans')
    ids = [t['id'] for t in tasks]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        warnings.append('duplicate task numbers: ' + ', '.join(dups))
    total = sum(t['boxes'] for t in tasks)
    done = sum(t['done'] for t in tasks)
    status = ('complete' if total and done == total else
              'in progress' if done else 'not started')
    verdict = 'redesign' if problems else ('warnings' if warnings else 'ready')
    try:
        inside = path.resolve().is_relative_to((Path(git_root() or '.') / plans_dir).resolve())
    except (AttributeError, ValueError):
        inside = str(path.resolve()).startswith(str((Path(git_root() or '.') / plans_dir).resolve()))
    return {'verdict': verdict, 'status': status, 'title': title, 'problems': problems,
            'warnings': warnings, 'validation_commands': validation,
            'in_plans_dir': inside, 'checkboxes': {'total': total, 'done': done},
            'tasks': [{k: t[k] for k in ('id', 'title', 'boxes', 'done', 'tests')}
                      for t in tasks]}


def cmd_check_plan(args):
    plans_dir = merged(cli_config_paths(git_root())).get('', {}).get('plans_dir', 'docs/plans')
    emit(dict(check_plan(args.plan, plans_dir), plan=args.plan))


def plan_store(plan):
    """Revision folder of one plan file, keyed by its absolute path."""
    key = hashlib.sha1(str(Path(plan).resolve()).encode()).hexdigest()[:12]
    folder = state_root() / 'plans' / f'{Path(plan).stem[:40]}-{key}'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def cmd_plan_rev(args):
    """Snapshot a plan before a revision, diff against it, or retire a superseded plan."""
    import difflib
    plan = Path(args.plan)
    if not plan.is_file():
        die(f'plan file not found: {plan}')
    folder = plan_store(plan)
    revs = sorted(folder.glob('rev-*.md'), key=lambda p: int(p.stem[4:]))
    if args.action == 'snapshot':
        dest = folder / f'rev-{len(revs) + 1}.md'
        shutil.copyfile(plan, dest)
        emit({'ok': True, 'revision': len(revs) + 1, 'snapshot': str(dest)})
        return
    if args.action == 'diff':
        if not revs:
            die('no snapshot: run plan-rev snapshot before changing the plan')
        before = revs[-1].read_text(encoding='utf-8').splitlines()
        after = plan.read_text(encoding='utf-8').splitlines()
        diff = list(difflib.unified_diff(before, after, f'{plan} (before)', f'{plan} (after)',
                                         lineterm='', n=1))
        check = check_plan(plan)
        old = check_plan(revs[-1])
        lost = old['checkboxes']['done'] - check['checkboxes']['done']
        emit({'revision': len(revs), 'changed': bool(diff), 'diff': '\n'.join(diff),
              'added': sum(1 for l in diff if l.startswith('+') and not l.startswith('+++')),
              'removed': sum(1 for l in diff if l.startswith('-') and not l.startswith('---')),
              'tasks_before': len(old['tasks']), 'tasks_after': len(check['tasks']),
              'checked_boxes_lost': max(0, lost), 'check': {k: check[k] for k in (
                  'verdict', 'status', 'problems', 'warnings')}})
        return
    # supersede: the plan was replaced by a re-planned file; keep it as a backup, out of plans_dir
    if not args.by or not Path(args.by).is_file():
        die('supersede needs --by <new plan file>')
    backup = folder / f'superseded-{time.strftime("%Y%m%d-%H%M%S")}.md'
    shutil.copyfile(plan, backup)
    # the retired plan becomes the new file's "before": plan-rev diff <new> shows the round
    new_folder = plan_store(args.by)
    new_revs = list(new_folder.glob('rev-*.md'))
    carried = new_folder / f'rev-{len(new_revs) + 1}.md'
    shutil.copyfile(plan, carried)
    code, out = run_quiet(['git', 'ls-files', '--error-unmatch', str(plan)])
    if code == 0:
        run_quiet(['git', 'rm', '-q', str(plan)])
        how = 'git rm (restore it from git history or the backup)'
    else:
        plan.unlink()
        how = 'removed (it was not committed; the backup keeps it)'
    emit({'ok': True, 'superseded': str(plan), 'by': args.by, 'backup': str(backup), 'how': how,
          'diff_with': f'plan-rev diff {args.by}', 'snapshot': str(carried)})


def cmd_config_summary(args):
    """Current repository setup, property by property, with where each value comes from."""
    repo = git_root()
    if not repo:
        die('not inside a git repository')
    cpaths, rpaths = cli_config_paths(repo), router_config_paths(repo)
    glob, local = merged([cpaths[0]]).get('', {}), merged([cpaths[1]]).get('', {})
    eff = merged(cpaths).get('', {})

    def prop(key, default=None):
        source = 'local' if key in local else 'global' if key in glob else 'default'
        return {'value': eff.get(key, default), 'source': source}

    executor = eff.get('executor') or 'claude'
    wrapper = parse_wrapper(repo, eff.get('codex_command'))
    rlocal = merged([rpaths[1]])
    rall = merged(rpaths)
    provider = rall.get('agrouter', {}).get('classifier_provider', 'typesafe')
    policy, defaults = load_policy(), {k: v[0] for k, v in POLICY.items()}
    drift = doc_drift(repo, eff.get('claude_args', ''), eff.get('codex_command')) \
        if ROUTER in eff.get('claude_command', '') else None
    issues = (missing_repo_configs(repo, eff) if cpaths[1].exists() else []) \
        + spec_problems(executor, eff, router_catalog(repo)[0]) \
        + ([f'doc drift: {drift}'] if drift else [])
    summary = {
        'configured': cpaths[1].exists(),
        'executor': dict(prop('executor', 'claude'), value=executor),
        'router': {'claude': ROUTER in eff.get('claude_command', ''),
                   'codex': bool(wrapper and wrapper['routes'])},
        'phases': {k: dict(prop(k), mode=spec_mode(eff.get(k)))
                   for k in ('plan_model', 'task_model', 'review_model')},
        'codex_model': prop('codex_model'), 'codex_reasoning_effort': prop('codex_reasoning_effort'),
        'docs': {'claude_args': claude_docs(eff.get('claude_args')), 'wrapper': wrapper},
        'external_review': ('skipped (codex executor)' if executor == 'codex'
                            else eff.get('external_review_tool', 'codex')),
        'reviews': review_capability(repo, eff),
        'codex_sandbox': prop('codex_sandbox'),
        'limits': {k: prop(k) for k in ('wait_on_limit', 'idle_timeout', 'session_timeout')},
        'router_local': {
            'file': str(rpaths[1]), 'exists': rpaths[1].exists(),
            'timeout': rlocal.get('agrouter', {}).get('timeout'),
            'long_context': rlocal.get(f'classifier.{provider}', {}).get('long_context'),
            'request_tokens': rlocal.get(f'classifier.{provider}', {}).get('request_tokens'),
            'bytes_per_token': rlocal.get(f'classifier.{provider}', {}).get('bytes_per_token'),
            'disabled_clis': sorted(s[4:] for s, v in rall.items()
                                    if s.startswith('cli.') and s.count('.') == 1
                                    and v.get('enabled') == 'false'),
        },
        'classifier': {'provider': provider,
                       'key_set': bool(rall.get(f'classifier.{provider}', {}).get('api_key')
                                       or os.environ.get('AGROUTER_CLASSIFIER_API_KEY'))},
        'response_times': {k: v['timeout'] for k, v in policy.items()
                           if v['timeout'] != defaults[k]},
        'issues': issues,
        'files': {f: tracked_state(repo, f) for f in
                  ('.ralphex/config', '.agrouter/config', '.autopilot/config',
                   (wrapper or {}).get('path') or '.ralphex/scripts/codex-agrouter.sh')},
    }
    emit(summary)


def cmd_config_unset(args):
    """Comment out a key so it inherits again (global config or default)."""
    path = Path(args.file).expanduser()
    if not path.exists():
        die(f'missing: {path}')
    lines = path.read_text(encoding='utf-8').splitlines()
    section, changed = None, 0
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line)
        if m and not m.group(1):
            section = m.group(2).strip()
            continue
        km = _key_re(args.key).match(line)
        if km and not km.group(1) and section == args.section:
            lines[i] = '# ' + line
            changed += 1
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    emit({'ok': True, 'file': str(path), 'unset': args.key, 'lines': changed})


def cmd_set_docs(args):
    """Change the routing docs in claude_args and the Codex wrapper together."""
    repo = git_root()
    local = Path(repo) / '.ralphex/config'
    eff = merged(cli_config_paths(repo)).get('', {})
    missing = [d for d in args.docs if not (Path(repo) / d).is_file()] + \
        [d for d in args.doc_dirs if not (Path(repo) / d).is_dir()]
    if missing:
        die(f'not found: {", ".join(missing)}')
    result = {'ok': True}
    claude_args = eff.get('claude_args', '')
    if ROUTER in eff.get('claude_command', '') and ' -- ' in claude_args:
        head, tail = claude_args.split(' -- ', 1)
        head = re.sub(r'\s*' + DOC_ARG_RE.pattern, '', head).strip()
        files = list(args.docs) + sorted(
            str(p.relative_to(repo)).replace('\\', '/')
            for d in args.doc_dirs for p in (Path(repo) / d).glob('*.md'))
        new = ' '.join([head] + [f'--doc={runner_arg(f)}' for f in dict.fromkeys(files)]) \
            + ' -- ' + tail
        local.write_text(ini_set(local.read_text(encoding='utf-8') if local.exists() else '',
                                 'claude_args', new), encoding='utf-8')
        result['claude_args'] = new
    wrapper = parse_wrapper(repo, eff.get('codex_command'))
    if wrapper:
        if wrapper['style'] == 'hand-written' and not args.replace_wrapper:
            result['wrapper'] = (f"{wrapper['path']} is hand-written and was not changed; "
                                 'pass --replace-wrapper to replace it with the template')
        else:
            argv = ['install-wrapper', '--force', '--docs', *args.docs, '--doc-dirs', *args.doc_dirs]
            if Path(wrapper['path']).suffix.lower() == '.cmd':
                argv.append('--windows')
            res = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=repo,
                                 capture_output=True, text=True)
            result['wrapper'] = json.loads(res.stdout or '{}')
            command = result['wrapper'].get('codex_command')
            if command and command != wrapper['path']:
                local.write_text(ini_set(local.read_text(encoding='utf-8') if local.exists() else '',
                                         'codex_command', command), encoding='utf-8')
    emit(result)


def codex_custom_provider():
    """The model_provider of ~/.codex/config.toml (CODEX_HOME) when it is not OpenAI, else None.

    `codex login status` checks only the OpenAI login. A custom provider (for example a company
    gateway) authenticates with its own key, so that check fails although codex works.
    """
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    try:
        text = (home / 'config.toml').read_text(encoding='utf-8')
    except OSError:
        return None
    top = re.split(r'(?m)^\s*\[', text, maxsplit=1)[0]  # keys before the first table
    m = re.search(r'(?m)^\s*model_provider\s*=\s*["\']([^"\']+)["\']', top)
    provider = m.group(1).strip() if m else None
    return provider if provider and provider.lower() not in ('openai', 'chatgpt') else None


def codex_status(router_cfg=None):
    """(usable, reason) for Codex as a reviewer: installed, new enough, logged in, routable."""
    path = shutil.which('codex')
    if not path:
        return False, 'codex is not installed'
    version = re.search(r'(\d+)\.(\d+)\.(\d+)', tool_info('codex')['version'] or '')
    if version and tuple(map(int, version.groups())) < (0, 130, 0):
        return False, 'codex is older than 0.130.0'
    if (router_cfg or {}).get('cli.codex', {}).get('enabled') == 'false':
        return False, 'codex is disabled for routing in the agrouter config'
    if os.environ.get('AUTOPILOT_CODEX_LOGIN_OK') != '1' and not codex_custom_provider():
        code, _ = run_quiet([path, 'login', 'status'], timeout=20)
        if code != 0:
            return False, 'codex is not logged in (run: codex login)'
    return True, None


def claude_status(router_cfg=None):
    """(usable, reason) without starting a model session or exposing auth details."""
    path = shutil.which('claude')
    if not path:
        return False, 'claude is not installed'
    if (router_cfg or {}).get('cli.claude', {}).get('enabled') == 'false':
        return False, 'claude is disabled for routing in the agrouter config'
    code, output = run_quiet([path, 'auth', 'status', '--json'], timeout=20)
    try:
        status = json.loads(output)
    except (ValueError, TypeError):
        return False, 'claude authentication could not be checked (run: claude auth status)'
    if code != 0 or not isinstance(status, dict) or status.get('loggedIn') is not True:
        return False, 'claude is not logged in (run: claude auth login)'
    return True, None


def codex_command_problem(repo, cfg):
    """Why the configured codex_command cannot run the Codex executor, or None."""
    if cfg.get('codex_sandbox') == 'read-only':
        # unset means danger-full-access for the Codex executor; an explicit value wins
        return 'codex_sandbox is read-only, so Codex cannot write or commit the tasks'
    command = cfg.get('codex_command') or 'codex'
    path = Path(repo) / command
    if not shutil.which(command) and not (path.is_file() and (WINDOWS or os.access(path, os.X_OK))):
        return f'codex_command is missing or not executable ({command})'
    return None


def codex_routed(repo, cfg):
    """True when Codex runs through the router (the agrouter wrapper as codex_command)."""
    wrapper = parse_wrapper(repo, cfg.get('codex_command'))
    return bool(wrapper and wrapper['routes'])


def codex_fallback_blocker(repo, cfg, cli_args=()):
    """Respect the configured sandbox and preserve routing when switching executors."""
    for i, arg in enumerate(cli_args):
        if arg in ('--external-only', '--codex-only', '-e', '-c'):
            return 'external-only review requires Claude; log in or skip the external review'
        if arg.startswith('--external-review-tool='):
            tool = arg.split('=', 1)[1]
        elif arg == '--external-review-tool' and i + 1 < len(cli_args):
            tool = cli_args[i + 1]
        else:
            continue
        if tool != 'none':
            return 'explicit external review requires Claude; log in or skip the external review'
    problem = codex_command_problem(repo, cfg)
    if problem:
        return f'{problem}; use autopilot-setup to configure the Codex executor'
    if ROUTER in cfg.get('claude_command', '') and not codex_routed(repo, cfg):
        return 'Codex routing wrapper is not configured; use autopilot-setup for the Codex executor'
    return None


def codex_fallback_args(cli_args):
    """Clear Claude phase selections for this run, including explicit phase flags."""
    result, skip = [], False
    flags = ('--plan-model', '--task-model', '--review-model')
    for arg in cli_args:
        if skip:
            skip = False
        elif arg in flags:
            skip = True
        elif arg == '--codex' or any(arg.startswith(f + '=') for f in flags):
            continue
        else:
            result.append(arg)
    return result + ['--codex', '--plan-model=:', '--task-model=:', '--review-model=:']


def plan_review_pick(executor, offered, external_tool='codex', revised=0):
    """The answer to the runner's "Review the plan before implementation?" when nobody replies.

    The runner asks the question itself (External reviewer, Same agent, Skip review) and
    autopilot relays it; this recommendation only keeps an unattended flow moving, and the
    user picks another reviewer (or none) in the chat. `offered` maps each reviewer option to
    (usable, reason). The external reviewer comes first, then the same agent, so a missing
    reviewer downgrades the review instead of dropping it. After a round that revised the
    plan, skip: one review round is enough without the user.
    """
    options = [{'option': name, 'usable': offered.get(name, (False,))[0],
                'reason': offered.get(name, (False, None))[1]}
               for name in (REVIEW_EXTERNAL, REVIEW_SAME)]
    options.append({'option': REVIEW_SKIP, 'usable': True, 'reason': None})

    def result(option, why):
        return {'options': options, 'recommended': option, 'recommended_why': why}

    if revised:
        return result(REVIEW_SKIP, 'one review round already revised the plan')
    want = REVIEW_EXTERNAL
    names = {REVIEW_EXTERNAL: f'the external review tool ({external_tool})',
             REVIEW_SAME: f'the same agent ({executor}, read-only)'}
    for option in (REVIEW_EXTERNAL, REVIEW_SAME):
        ok, why = offered.get(option, (False, 'not offered'))
        if ok:
            if option == want:
                return result(option, f'{names[option]} reviews plans here')
            reason = offered.get(want, (False, 'not offered'))[1] or 'not offered'
            return result(option, f'{want} is not usable ({reason}); {names[option]} reviews instead')
    return result(REVIEW_SKIP, 'no reviewer is usable: ' + '; '.join(
        f"{o['option']}: {o['reason']}" for o in options[:2]))


def runner_version(text):
    """(1, 7, 0, 4) from 'ralphex v1.7.0-windows.4'; None when the version is unknown."""
    m = re.search(r'v?(\d+)\.(\d+)\.(\d+)(?:-windows\.(\d+))?', text or '')
    return tuple(int(g or 0) for g in m.groups()) if m else None


def runner_has_plan_review(cli):
    """True when the runner reviews plans itself; None when its version cannot be read."""
    if os.environ.get('AUTOPILOT_PLAN_REVIEW') in ('0', '1'):  # tests and unusual builds
        return os.environ['AUTOPILOT_PLAN_REVIEW'] == '1'
    version = runner_version(tool_info(cli)['version'] if cli else None)
    return None if version is None else version >= PLAN_REVIEW_MIN


def review_capability(repo, cfg=None):
    """What can review here: the runner's external code review and autopilot's plan review."""
    cfg = cfg if cfg is not None else merged(cli_config_paths(repo)).get('', {})
    rcfg = merged(router_config_paths(repo))
    executor = cfg.get('executor') or 'claude'
    # "[cli.X] enabled = false" only matters where X is reached through the router: a direct
    # (No router) codex_command or claude_command runs the CLI without asking agrouter
    codex_ok, codex_why = codex_status(rcfg if codex_routed(repo, cfg) else None)
    claude_ok, claude_why = claude_status(rcfg if ROUTER in cfg.get('claude_command', '') else None)
    tool = cfg.get('external_review_tool') or (
        'none' if cfg.get('codex_enabled') == 'false' else 'codex')
    if executor == 'codex':
        code = {'tool': 'none', 'usable': False, 'reason': 'skipped by the Codex executor'}
    elif tool == 'codex':
        code = {'tool': 'codex', 'usable': codex_ok, 'reason': codex_why}
    elif tool == 'custom':
        script = cfg.get('custom_review_script', '')
        ok = bool(script) and Path(script).expanduser().exists()
        code = {'tool': 'custom', 'usable': ok, 'reason': None if ok else 'custom script missing'}
    else:
        code = {'tool': 'none', 'usable': False, 'reason': 'external review is off'}
    usable = {'codex': (codex_ok, codex_why), 'claude': (claude_ok, claude_why)}
    # the runner's plan review: External reviewer is its external review tool (never under the
    # Codex executor), Same agent is the planner executor, read-only
    offered = {REVIEW_EXTERNAL: (code['usable'], code['reason']),
               REVIEW_SAME: usable[executor]}
    plan = {'by': 'runner', 'external_tool': code['tool']}
    plan.update(plan_review_pick(executor, offered, code['tool']))
    fallback = 'codex' if executor == 'claude' and not claude_ok and codex_ok else None
    run_ok, run_why = usable[executor]
    if executor == 'codex' and run_ok:
        # installed and logged in is not enough: the runner must be able to start
        # codex_command and let it write (the same checks the Claude fallback uses)
        problem = codex_command_problem(repo, cfg)
        if problem:
            run_ok, run_why = False, problem
    execution = {'configured': executor, 'usable': run_ok,
                 'reason': run_why, 'fallback': fallback,
                 'fallback_blocker': codex_fallback_blocker(repo, cfg) if fallback else None}
    return {'code_review': code, 'plan_review': plan, 'execution': execution,
            'codex': {'usable': codex_ok, 'reason': codex_why},
            'claude': {'usable': claude_ok, 'reason': claude_why}}


def spec_problems(executor, cfg, catalog=None):
    """Phase model specs that name the other executor's models (often inherited globally),
    and Codex efforts the runner drops or the model does not support."""
    problems = []
    wrong = CLAUDE_FAMILY if executor == 'codex' else CODEX_FAMILY
    for key in ('plan_model', 'task_model', 'review_model'):
        model = cfg.get(key, '').split(':')[0].strip()
        if model and wrong.search(model):
            problems.append(f'{key} = {cfg[key]} names a model the {executor} executor cannot '
                            f'run (often inherited from the global config): set it to ":" or a '
                            f'{executor} model in .ralphex/config')
    if executor != 'codex':
        return problems
    base_model = cfg.get('codex_model', '').strip()
    base_effort = cfg.get('codex_reasoning_effort', '').strip()
    for key in ('plan_model', 'task_model', 'review_model'):
        spec = cfg.get(key, '')
        model, _, effort = spec.partition(':')
        model, effort = model.strip() or base_model, effort.strip()
        if effort.lower() == 'max':
            # ResolveCodexModelEffort drops a phase-level "max" and keeps codex_reasoning_effort;
            # a base codex_reasoning_effort = max (or ultra anywhere) reaches codex unchanged
            problems.append(f'{key} = {spec}: the runner drops a phase-level "max" effort for codex '
                            f'and keeps codex_reasoning_effort ({base_effort or "routed"}); set '
                            f'codex_reasoning_effort = max, or another effort the model supports')
        elif effort:
            problems += effort_problem(key, spec, model, effort, catalog)
    if base_effort:
        problems += effort_problem('codex_reasoning_effort', base_effort, base_model, base_effort,
                                   catalog)
    return problems


def effort_problem(key, value, model, effort, catalog):
    """An effort the catalog says the model does not support (unknown models pass)."""
    if not model or not catalog:
        return []
    entry = next((m for m in catalog.get('codex', []) if model == m['name']
                  or model in [a.strip() for a in m['aliases'].split(',')]), None)
    if not entry or not entry['efforts'] or effort in entry['efforts']:
        return []
    return [f'{key} = {value}: {model} supports efforts {", ".join(entry["efforts"])} '
            f'(router catalog), not "{effort}"']


def repo_config_states(repo, cfg):
    """{path: missing|partial|full} for the configs this repository needs."""
    wanted = {'.ralphex/config': 'ralphex', '.autopilot/config': 'autopilot'}
    if ROUTER in cfg.get('claude_command', '') + cfg.get('codex_command', '') or \
            codex_routed(repo, cfg):
        wanted['.agrouter/config'] = 'agrouter'
    return {path: config_state(Path(repo) / path, tool)
            for path, tool in sorted(wanted.items(), key=lambda i: list(CONFIG_DEST.values()).index(i[0]))}


def missing_repo_configs(repo, cfg):
    """Repository configs setup writes so a clone runs without any global config but the key."""
    what = {'.ralphex/config': 'runner config', '.agrouter/config': 'router config',
            '.autopilot/config': 'autopilot config'}
    issues = []
    for path, state in repo_config_states(repo, cfg).items():
        if state == 'missing':
            issues.append(f'this repository has no {what[path]} ({path}): run autopilot-setup repo')
        elif state == 'partial':
            issues.append(f'the {what[path]} ({path}) is partial (not from the template): '
                          'run autopilot-setup to upgrade it with config-init --merge')
    router = Path(repo) / '.agrouter/config'
    if router.exists() and not any(sec == 'agrouter' and k == 'classifier_provider'
                                   for sec, k, _ in ini_active(router.read_text(encoding='utf-8'))):
        issues.append('the router config (.agrouter/config) does not set '
                      '[agrouter] classifier_provider: users without a global config '
                      'would route with the default provider')
    if codex_routed(repo, cfg):
        prefix = merged(router_config_paths(repo)).get(
            'cli.codex.availability.limits', {}).get('error_prefix')
        if prefix is not None and prefix.strip('"\'') != 'ERROR: agrouter: ':
            issues.append(f'the Codex quota prefix is {prefix}, not "ERROR: agrouter: ": the '
                          'runner would stop on a Codex limit instead of waiting; set it in '
                          '.agrouter/config')
    own = Path(repo) / '.autopilot/config'
    old = sorted({f'[{sec}] {k}' for sec, k, _ in ini_active(own.read_text(encoding='utf-8'))
                  if sec in OBSOLETE_SECTIONS}) if own.exists() else []
    if old:
        issues.append(f"the autopilot config (.autopilot/config) sets {', '.join(old)}, which "
                      'autopilot no longer reads (plan review is always auto, runs always open '
                      'a pull request; change either in the chat): remove them')
    return issues


def cmd_doctor(args):
    repo = git_root()
    cli = find_cli()
    info = {
        'host': detect_host(),
        'os': sys.platform,
        'python': sys.version.split()[0],
        'cli': tool_info(cli) if cli else {'path': None, 'version': None},
        'router': tool_info(ROUTER),
        'tools': {n: bool(shutil.which(n)) for n in
                  ('claude', 'codex', 'go', 'brew', 'gh', 'git', 'fzf')},
        'repo': repo,
    }
    if cli:
        info['cli']['name'] = Path(cli).name
    if repo:
        _, status = run_quiet(['git', 'status', '--porcelain'], cwd=repo)
        _, branch = run_quiet(['git', 'branch', '--show-current'], cwd=repo)
        info['repo_clean'] = not status
        info['branch'] = branch
    cpaths, rpaths = cli_config_paths(repo), router_config_paths(repo)
    cfg = merged(cpaths).get('', {})
    info['cli_config'] = {
        'global': str(cpaths[0]), 'global_exists': cpaths[0].exists(),
        'local': str(cpaths[1]) if repo else None,
        'local_exists': bool(repo) and cpaths[1].exists(),
        'effective': {k: cfg[k] for k in (
            'claude_command', 'claude_args', 'executor', 'codex_command', 'plan_model',
            'task_model', 'review_model', 'codex_model', 'codex_reasoning_effort',
            'external_review_tool', 'wait_on_limit', 'idle_timeout', 'plans_dir') if k in cfg},
        'uses_router': ROUTER in cfg.get('claude_command', '') + cfg.get('codex_command', ''),
    }
    executor = cfg.get('executor') or 'claude'
    info['cli_config']['executor'] = executor
    info['cli_config']['spec_problems'] = spec_problems(
        executor, cfg, router_catalog(repo)[0] if executor == 'codex' else None)
    if repo and ROUTER in cfg.get('claude_command', ''):
        info['cli_config']['doc_drift'] = doc_drift(
            repo, cfg.get('claude_args', ''), cfg.get('codex_command', ''))
    rcfg = merged(rpaths)
    provider = rcfg.get('agrouter', {}).get('classifier_provider', 'typesafe')
    key_in_cfg = bool(rcfg.get(f'classifier.{provider}', {}).get('api_key'))
    info['router_config'] = {
        'global': str(rpaths[0]), 'global_exists': rpaths[0].exists(),
        'local_exists': bool(repo) and rpaths[1].exists(),
        'provider': provider,
        'key_set': key_in_cfg or bool(os.environ.get('AGROUTER_CLASSIFIER_API_KEY')),
        'key_source': 'config' if key_in_cfg else (
            'env' if os.environ.get('AGROUTER_CLASSIFIER_API_KEY') else None),
    }
    for key in ('cli', 'router'):
        if info[key]['path']:
            info[key]['via'] = install_via(info[key]['path'])
    if args.latest or args.updates or args.skip_updates:
        if info['host']['network_disabled'] and not args.latest:
            info['updates_check'] = 'offline'
        else:
            latest, how, skipped = latest_versions(force=args.latest)
            info['updates_check'] = how if any(latest.values()) or how == 'off' else 'offline'
            if how != 'off':
                info['latest'] = latest
            updates = pending_updates(info, latest)
            if args.skip_updates:
                skip_updates(updates)
                skipped = {u['tool']: u['latest'] for u in updates}
            info['updates'] = [u for u in updates if skipped.get(u['tool']) != u['latest']]
            info['updates_skipped'] = [u for u in updates if skipped.get(u['tool']) == u['latest']]
    missing = []
    if not cli:
        missing.append('plan runner (ralphex.windows) is not installed')
    if not info['router']['path']:
        missing.append('model router (agrouter) is not installed')
    incomplete = []
    if cli:
        info['cli']['plan_review'] = runner_has_plan_review(cli)
        if info['cli']['plan_review'] is False:
            incomplete.append(f"{info['cli']['name']} {info['cli']['version']} has no plan review "
                              '(v1.7.0-windows.4 or newer): upgrade it with autopilot-setup')
    if info['router']['path'] and not info['router_config']['key_set']:
        incomplete.append('classifier API key is not set')
    apaths = autopilot_config_paths(repo)
    info['autopilot_config'] = {'global': str(apaths[0]), 'global_exists': apaths[0].exists(),
                                'local_exists': bool(repo) and apaths[-1].exists()}
    if repo:
        info['repo_configs'] = repo_config_states(repo, cfg)
        incomplete += missing_repo_configs(repo, cfg)
    info['state_dir'] = str(state_root())
    if info['host']['network_disabled']:
        incomplete.append('the shell runs in a sandbox without network: run autopilot '
                          'commands with full access (outside the sandbox)')
    for problem in info['cli_config']['spec_problems']:
        incomplete.append(problem)
    if executor == 'codex' or cfg.get('external_review_tool', 'codex') == 'codex':
        version = re.search(r'(\d+)\.(\d+)\.(\d+)', tool_info('codex')['version'] or '')
        if shutil.which('codex') and version and tuple(map(int, version.groups())) < (0, 130, 0):
            incomplete.append('codex CLI is older than 0.130.0, which the runner needs')
    if repo:
        info['reviews'] = review_capability(repo, cfg)
        execution = info['reviews']['execution']
        if not execution['usable']:
            action = (execution['fallback_blocker'] or 'this run can fall back to Codex') \
                if execution['fallback'] else 'run autopilot-setup or log in'
            incomplete.append(f"{execution['reason']}: {action}")
        code_review = info['reviews']['code_review']
        if code_review['tool'] == 'codex' and not code_review['usable']:
            incomplete.append(f"external code review uses codex, but {code_review['reason']}: "
                              'runs will skip it (or set external_review_tool = none)')
    info['missing_tools'] = missing
    info['setup_incomplete'] = incomplete
    sessions = [s for s in list_sessions(repo) if s['alive']] if repo else []
    info['active_sessions'] = sessions
    if args.json:
        emit(info)
        return
    for key, value in info.items():
        print(f'{key}: {json.dumps(value, ensure_ascii=False)}')


def router_catalog(repo):
    """Models per CLI as agrouter merges them: embedded defaults, then global, then local.

    The embedded defaults are not exposed by the router; the installed global config is a
    commented copy of them, so its uncommented text stands in for that layer. Active global
    and local keys then override per section and key, so a partial override (one model
    disabled, one effort list changed) keeps every other model.
    """
    paths = router_config_paths(repo)
    data = {}
    if paths[0].exists():
        text = '\n'.join(re.sub(r'^\s*[#;]\s?', '', l)
                         for l in paths[0].read_text(encoding='utf-8').splitlines())
        for section, key, value in ini_active(text):
            data.setdefault(section or '', {})[key] = value
    for section, values in merged(paths).items():
        data.setdefault(section, {}).update(values)
    models = {}
    for section, values in data.items():
        if not section.startswith('model.') or values.get('enabled') == 'false':
            continue
        cli = values.get('cli', '?')
        if data.get(f'cli.{cli}', {}).get('enabled') == 'false':
            continue
        models.setdefault(cli, []).append({
            'name': values.get('name', section[6:]),
            'aliases': values.get('aliases', ''),
            'efforts': [e.strip() for e in values.get('efforts', '').split(',') if e.strip()],
            'about': values.get('description', '')[:160],
        })
    return models, [str(p) for p in paths if p.exists()]


def cmd_catalog(args):
    models, sources = router_catalog(git_root())
    emit({'models': models, 'source': sources})


# ---------------------------------------------------------------- sessions

def in_repo(meta, repo):
    """A session belongs to its folder and, for a run in a new worktree, to the source checkout."""
    return repo in (meta.get('repo'), meta.get('source_repo'))


def list_sessions(repo=None, all_repos=False):
    root = state_root() / 'sessions'
    out = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        meta = read_json(d / 'session.json')
        if not meta or (not all_repos and repo and not in_repo(meta, repo)):
            continue
        st = read_json(d / 'state.json', {})
        out.append({'session': d.name, 'kind': meta['kind'], 'target': meta.get('target'),
                    'status': st.get('status'), 'alive': session_active(st),
                    'created': meta.get('created_iso')})
    return out


def cmd_list(args):
    emit({'sessions': list_sessions(git_root(), args.all)[:20]})


def write_shims(sdir):
    bindir = sdir / 'bin'
    bindir.mkdir(exist_ok=True)
    py, me = sys.executable, str(SCRIPT)
    for name in ('fzf', 'autopilot-editor'):
        if WINDOWS:
            with (bindir / f'{name}.cmd').open('w', encoding='utf-8', newline='') as handle:
                handle.write(f'@echo off\r\n@"{py}" -X utf8 "{me}" _shim {name} %*\r\n'
                             'exit /b %ERRORLEVEL%\r\n')
        else:
            path = bindir / name
            path.write_text(f'#!/bin/sh\nexec "{py}" "{me}" _shim {name} "$@"\n')
            path.chmod(0o755)
    return bindir


def require_tools():
    """Stop before anything starts when the runner or the router is missing."""
    cli = find_cli()
    missing = [] if cli else ['ralphex.windows']
    # AUTOPILOT_NO_ROUTER=1 skips the router check (tests with a fake runner)
    if not shutil.which(ROUTER) and not os.environ.get('AUTOPILOT_NO_ROUTER'):
        missing.append(ROUTER)
    if missing:
        die(f"not installed: {', '.join(missing)}; run the autopilot-setup skill first")
    return cli


def host_session_id():
    """The chat that runs this command (Claude Code or Codex), or None elsewhere."""
    return os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('CODEX_THREAD_ID')


# Terminals that tell a process which tab it runs in and can type a line into that tab.
# Typing the wake line works for any agent CLI (Claude Code, Codex, Gemini, ...): to the
# agent it is an ordinary user message. {handle} is the tab id, {text} the line.
WAKE_TERMINALS = [
    ('orca', 'ORCA_TERMINAL_HANDLE',
     ['{bin}', 'terminal', 'send', '--terminal', '{handle}', '--text', '{text}', '--enter']),
    ('tmux', 'TMUX_PANE',
     ['{bin}', 'send-keys', '-t', '{handle}', '-l', '{text}', ';', 'send-keys', '-t', '{handle}', 'Enter']),
    ('wezterm', 'WEZTERM_PANE', ['{bin}', 'cli', 'send-text', '--pane-id', '{handle}', '--no-paste', '{text_nl}']),
    ('kitty', 'KITTY_WINDOW_ID', ['{bin}', '@', 'send-text', '--match', 'id:{handle}', '{text_nl}']),
    ('iterm', 'ITERM_SESSION_ID', ['osascript', '-e', '{applescript}']),
]


def own_tab():
    """The terminal tab this command runs in, when its terminal can be typed into; else None."""
    custom = autopilot_config('wake').get('command')
    for kind, var, _ in WAKE_TERMINALS:
        handle = os.environ.get(var, '').strip()
        if not handle:
            continue
        tab = {'kind': kind, 'handle': handle}
        if kind == 'orca':
            tab['bin'] = os.environ.get('ORCA_CLI_COMMAND') or shutil.which('orca')
        elif kind == 'tmux':
            tab['bin'] = shutil.which('tmux')
            tab['socket'] = os.environ.get('TMUX', '').split(',')[0] or None
        elif kind == 'kitty':
            tab['bin'] = shutil.which('kitty')
            tab['to'] = os.environ.get('KITTY_LISTEN_ON')
            if not tab['to']:
                continue  # kitty accepts remote control only through its socket
        elif kind == 'iterm':
            tab['handle'] = handle.split(':', 1)[-1]  # w0t0p0:<unique id>
            tab['bin'] = shutil.which('osascript')
        else:
            tab['bin'] = shutil.which(kind)
        if tab.get('bin'):
            return tab
    if custom:  # any other terminal: [wake] command = mytool send {text}
        return {'kind': 'custom', 'command': custom}
    return None


def wake_argv(tab, text):
    """The command that types text (and Enter) into tab."""
    if tab['kind'] == 'custom':
        return [a.replace('{text}', text) for a in split_args(tab['command'])]
    spec = next(t for k, _, t in WAKE_TERMINALS if k == tab['kind'])
    quoted = text.replace('\\', '\\\\').replace('"', '\\"')
    applescript = ('tell application "iTerm2"\nrepeat with w in windows\n'
                   'repeat with t in tabs of w\nrepeat with s in sessions of t\n'
                   f'if id of s is "{tab["handle"]}" then tell s to write text "{quoted}"\n'
                   'end repeat\nend repeat\nend repeat\nend tell')
    values = {'{bin}': tab.get('bin') or '', '{handle}': tab['handle'], '{text}': text,
              '{text_nl}': text + '\r', '{applescript}': applescript}
    argv = []
    for arg in spec:
        for key, value in values.items():
            arg = arg.replace(key, value)
        argv.append(arg)
    if tab['kind'] == 'tmux' and tab.get('socket'):
        argv[1:1] = ['-S', tab['socket']]
    if tab['kind'] == 'kitty' and tab.get('to'):
        argv[1:1] = ['--to', tab['to']]
    return argv


def notify(title, text):
    """A desktop notification, best effort: macOS, Linux; nothing elsewhere."""
    if sys.platform == 'darwin' and shutil.which('osascript'):
        msg = text.replace('\\', '').replace('"', "'")
        run_quiet(['osascript', '-e', f'display notification "{msg}" with title "{title}"'])
    elif shutil.which('notify-send'):
        run_quiet(['notify-send', title, text])


def split_args(text):
    import shlex
    return shlex.split(text, posix=not WINDOWS)


def wake_mode():
    """type (type into the owner's tab, else notify), notify, or off."""
    mode = (os.environ.get('AUTOPILOT_WAKE') or autopilot_config('wake').get('mode') or 'type').lower()
    return mode if mode in ('type', 'notify', 'off') else 'type'


def start_session(kind, target, cli_args, max_restarts, flow=None, extra=None):
    cli = require_tools()
    repo = git_root()
    if not repo:
        die('not inside a git repository')
    if Path.cwd().resolve() != Path(repo):
        die(f'run from the repository root: {repo}')
    for s in list_sessions(repo):
        if s['alive'] and s['kind'] == kind and s['target'] == target:
            die(f"session already active for this target: {s['session']}")
    effective = merged(cli_config_paths(repo)).get('', {})
    executor = 'codex' if '--codex' in cli_args else (effective.get('executor') or 'claude')
    capabilities = review_capability(repo, dict(effective, executor=executor))
    execution = capabilities['execution']
    executor_note = None
    if not execution['usable']:
        if not execution['fallback']:
            die(f"{execution['reason']}; run autopilot-setup or log in before starting")
        blocker = codex_fallback_blocker(repo, effective, cli_args)
        if blocker:
            die(f"{execution['reason']}; {blocker}")
        cli_args = codex_fallback_args(cli_args)
        executor, executor_note = 'codex', execution['reason']
    stamp = time.strftime('%Y%m%d-%H%M%S')
    sid = f'{stamp}-{kind}-{secrets.token_hex(2)}'
    sdir = state_root() / 'sessions' / sid
    (sdir / 'inbox').mkdir(parents=True)
    write_shims(sdir)
    review_only = (extra or {}).get('mode') == 'review'
    review_native = runner_has_plan_review(cli) if kind == 'plan' else None
    if review_only and review_native is False:
        die(f"{Path(cli).name} has no --plan-review (it needs v1.7.0-windows.4 or newer): "
            'upgrade it with autopilot-setup')
    argv = [cli, '--no-color']
    if kind == 'plan':
        argv += ['--plan-review', target] if review_only else ['--plan', target]
    else:
        argv += [target]
    added = []
    if not any(a == '--wait' or a.startswith('--wait=') for a in cli_args) \
            and not effective.get('wait_on_limit'):
        # without it the runner exits on a limit and the whole run restarts;
        # with it the runner retries the same step in place
        added = [f"--wait={int(os.environ.get('AUTOPILOT_LIMIT_WAIT', 900))}s"]
    review_note = None
    if kind == 'run' and executor == 'claude' and \
            not any(a.startswith('--external-review-tool') for a in cli_args):
        code_review = capabilities['code_review']
        if code_review['tool'] == 'codex' and not code_review['usable']:
            added.append('--external-review-tool=none')
            review_note = code_review['reason']
    argv += added + cli_args
    _, start_branch = run_quiet(['git', 'branch', '--show-current'], cwd=repo)
    # the files that had changes before this flow: a move to a new worktree leaves them here
    baseline = None if flow else dirty_snapshot(repo)
    write_json(sdir / 'session.json', {
        'start_branch': start_branch or None,
        'kind': kind, 'target': target, 'repo': repo, 'argv': argv, 'cli_args': list(cli_args),
        'owner': host_session_id(), 'tab': own_tab(),
        'max_restarts': max_restarts, 'created': now(),
        'flow': flow or f'flow-{stamp}-{secrets.token_hex(2)}', 'executor': executor,
        'policy': load_policy(),
        'created_iso': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'repo_hash': hashlib.sha1(repo.encode()).hexdigest()[:8], 'plan_review': review_native,
        **({'baseline': baseline} if baseline is not None else {}), **(extra or {})})
    write_json(sdir / 'state.json', {'status': 'starting', 'seq': 0, 'events': [],
                                     **({'plan_file': target} if review_only else {})})
    if kind == 'plan' and review_native is False:
        log_decision(sdir, kind='note', by='autopilot',
                     text='the runner has no plan review: the plan gets no review by another model',
                     reason=f'{Path(cli).name} is older than v1.7.0-windows.4; upgrade it with '
                            'autopilot-setup')
    if executor_note:
        log_decision(sdir, kind='decision', by='autopilot',
                     text='switched this session to the Codex executor; phase models reset to routing',
                     reason=executor_note)
    for flag in added:
        if flag.startswith('--wait'):
            log_decision(sdir, kind='decision', by='autopilot', text=f'added {flag}',
                         reason='wait_on_limit is not set: let the runner wait on rate limits '
                                'and retry the same step instead of exiting')
        else:
            log_decision(sdir, kind='decision', by='autopilot',
                         text='skipped the external code review for this run',
                         reason=review_note)
    wt = (extra or {}).get('worktree') or {}
    if wt.get('moved'):
        names = ', '.join(wt['moved'][:8]) + (f" (+{len(wt['moved']) - 8} more)"
                                              if len(wt['moved']) > 8 else '')
        log_decision(sdir, kind='decision', by='autopilot',
                     text=f"moved {len(wt['moved'])} file(s) changed during the flow to "
                          f"{wt['path']}: {names}",
                     reason='the run works in a new worktree; the source checkout got these '
                            'files back as committed'
                            + (f"; restore problems: {'; '.join(wt['restore_problems'])}"
                               if wt.get('restore_problems') else ''))
    if wt.get('kept_here'):
        log_decision(sdir, kind='decision', by='autopilot',
                     text=f"copied but kept in the source checkout: {', '.join(wt['kept_here'][:8])}",
                     reason='these files had changes before the flow started; only a copy moved')
    log = open(sdir / 'supervisor.log', 'ab')
    proc = subprocess.Popen([sys.executable, str(SCRIPT), '_supervise', str(sdir)],
                            cwd=repo, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, **detach_kwargs())
    for _ in range(50):
        st = read_json(sdir / 'state.json', {})
        if st.get('status') != 'starting':
            break
        time.sleep(0.1)
    meta = read_json(sdir / 'session.json')
    emit({'session': sid, 'flow': meta['flow'], 'state_dir': str(sdir),
          'supervisor_pid': proc.pid, 'command': ' '.join(argv),
          'executor': executor, 'executor_fallback_reason': executor_note,
          **({'plan_review': review_native} if kind == 'plan' else {})})


def cmd_plan(args):
    start_session('plan', args.desc, args.cli_args, args.max_restarts, args.flow)


def cmd_plan_review(args):
    """The runner's own review of an existing plan file (--plan-review), relayed like planning."""
    plan = Path(args.plan)
    if not plan.is_file():
        die(f'plan file not found: {plan}')
    start_session('plan', args.plan, args.cli_args, args.max_restarts, args.flow,
                  extra={'mode': 'review'})


def cmd_run(args):
    require_tools()
    plan = Path(args.plan)
    if not plan.exists():
        die(f'plan file not found: {plan}')
    verdict = check_plan(plan)
    if verdict['verdict'] == 'redesign' and not args.force:
        die('the runner cannot use this plan: ' + '; '.join(verdict['problems']) +
            '. Redesign it with the autopilot-plan skill (--redesign), or pass --force')
    repo = git_root() or '.'
    if run_quiet(['git', 'check-ignore', '-q', '--', str(plan.resolve())], cwd=repo)[0] == 0:
        # a setup error in the repository: the run commits its plan, git refuses an ignored one
        die(f'git ignores the plan {plan}: remove its folder from .gitignore. The run commits '
            'the plan and archives it to completed/ at the end')
    cli_args = list(args.cli_args)
    if getattr(args, 'branch', None) and not any(a == '--branch' or a.startswith('--branch=') for a in cli_args):
        cli_args += ['--branch', args.branch]  # the runner's flag: the run branch's name
    new_worktree = args.new_worktree or bool(args.worktree_path)
    if '--worktree' in cli_args:
        # the runner's own worktree lands inside .ralphex/worktrees/; make it beside the folder
        cli_args.remove('--worktree')
        new_worktree = True
    place = implementation_place(repo, plan=plan)
    wanted = 'worktree' if new_worktree else 'here'
    choice = next(o for o in place['options'] if o['option'] == wanted)
    if not choice['usable'] and not args.force:
        die(f"cannot run in {choice['text']}: {choice['reason']}. "
            + (f"Recommended: {place['recommended']} ({place['why']})" if place['recommended']
               else 'Fix it first') + '; see the place command')
    extra = None
    if new_worktree:
        wt = prepare_worktree(repo, plan, cli_args, args.worktree_path, flow_baseline(args.flow))
        os.chdir(wt['path'])
        plan = Path(wt['plan']).resolve().relative_to(Path(wt['path']).resolve())
        extra = {'source_repo': repo, 'worktree': wt}
    start_session('run', str(plan), cli_args, args.max_restarts, args.flow, extra)


# ---------------------------------------------------------------- supervisor

def plan_branch(meta):
    """The branch the runner makes for a plan: --branch, or the plan file name without its date."""
    argv = meta['argv']
    for i, arg in enumerate(argv):
        if arg == '--branch' and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith('--branch='):
            return arg.split('=', 1)[1]
    name = Path(meta['target']).name
    name = name[:-3] if name.endswith('.md') else name
    return re.sub(r'^[\d-]+', '', name).lstrip('-') or name


def on_default_branch(repo, branch, cfg=None):
    cfg = cfg if cfg is not None else merged(cli_config_paths(repo)).get('', {})
    default = (cfg.get('default_branch') or default_branch(repo) or '').removeprefix('origin/')
    return branch == default if default else branch in ('main', 'master')


def run_work(meta):
    """(branch, folder, base, base_why) of a run: where its commits are and what they fork from.

    From the default branch the runner makes the plan's branch (in place or in a worktree), so
    the base is the branch the run started on. On a feature branch it works on that branch; the
    base is then the branch it was created from (its reflog), else the default branch.
    """
    repo = meta['repo']
    start = meta.get('start_branch')
    default = default_branch(repo) or ''
    if meta.get('worktree'):
        wt = meta['worktree']
        return wt['branch'], wt['path'], wt['base'], f"the worktree was created from {wt['base']}"
    if start and not on_default_branch(repo, start):
        branch, base, why = start, None, None
        _, log = run_quiet(['git', 'reflog', 'show', '--format=%gs', f'refs/heads/{start}'], cwd=repo)
        created = re.findall(r'branch: Created from (\S+)', log or '')
        source = created[-1] if created else None
        if source and source not in ('HEAD', start) and \
                run_quiet(['git', 'rev-parse', '--verify', '-q', source], cwd=repo)[0] == 0:
            base, why = source.removeprefix('origin/'), f'{start} was created from {source}'
        if not base:
            base, why = default.removeprefix('origin/'), 'the default branch (the source of ' \
                f'{start} is not known)'
    else:
        branch = plan_branch(meta)
        base = (start or default).removeprefix('origin/')
        why = f'the run started on {base}'
    folder = repo
    _, listing = run_quiet(['git', 'worktree', 'list', '--porcelain'], cwd=repo)
    path = None
    for line in (listing or '').splitlines():
        if line.startswith('worktree '):
            path = line[9:]
        elif line == f'branch refs/heads/{branch}' and path:
            folder = path
    return branch, folder, base, why


class ResumeBlocked(Exception):
    """Restarting this worktree run would lose its progress."""


class Supervisor:
    def __init__(self, sdir):
        self.sdir = Path(sdir)
        self.meta = read_json(self.sdir / 'session.json')
        self.state = read_json(self.sdir / 'state.json', {}) or {}
        self.state.update({'status': 'running', 'pid': os.getpid(), 'attempt': 0,
                           'restarts': [], 'seq': 0, 'events': [], 'started': now()})
        self.lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.buf = ''           # output of the current attempt
        self.consumed = 0       # buffer length when the last prompt was answered
        self.last_output = now()
        self.stop_requested = False
        self.restart_now = False
        self.proc = None
        self.last_draft = None  # output offset of the draft whose copies were last written
        self.log = open(self.sdir / 'output.log', 'a', encoding='utf-8')
        # milestones: one event per step start (with its model), task done and commit
        self.sel_lines = None   # lines of a selection JSON printed under its own header
        self.step = None        # the section running now, announced once its model is known
        self.plan_path = None   # the plan file progress is read from (worktree copy or repo)
        self.plan_tasks = []    # [(title, done, total)] from the last progress read
        self.complete = None    # titles of finished tasks; None until the first read
        self.done_pending = []  # finished tasks waiting briefly for their commit
        self.head = None        # last commit already reported
        self.head_where = None  # the checkout self.head belongs to
        self.stalled = False
        self.findings = None    # plan review findings being read, None outside that block
        self.state.setdefault('task_log', {})
        self.router_mark = None  # (agrouter.log path, size) when a codex step started
        try:
            self.setup = model_setup(self.meta['repo'], self.meta.get('cli_args'),
                                     self.meta.get('executor'))
        except Exception:  # never let the model-source label stop a run
            self.setup = None

    # -- state
    def save(self):
        with self.state_lock:
            self.state['updated'] = now()
            write_json(self.sdir / 'state.json', self.state)

    def event(self, kind, **data):
        if kind in ('done', 'failed', 'stopped'):
            self.state['ended'] = now()
        if kind not in WAIT_QUIET:
            self.last_news = now()
        with self.state_lock:
            self.state['seq'] += 1
            ev = {'seq': self.state['seq'], 't': time.strftime('%H:%M:%S'),
                  'kind': kind, **data}
            self.state['events'] = (self.state['events'] + [ev])[-40:]
            self.save()
            # the full timeline: a chat that reattaches after hours catches up from here
            with open(self.sdir / 'events.jsonl', 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + '\n')

    # -- worktree runs
    def worktree_mode(self):
        argv = self.meta['argv']
        if '--worktree' in argv:
            return 'flag'
        cfg = merged(cli_config_paths(self.meta['repo'])).get('', {})
        return 'config' if cfg.get('use_worktree', '').lower() == 'true' else None

    def run_branch(self):
        """The branch the runner uses for this plan (--branch, or the plan name sans date)."""
        return plan_branch(self.meta)

    def launch(self):
        """(cwd, argv) for the next attempt.

        A worktree run that already has its branch must not start with --worktree again: the
        runner would copy the source checkout's plan (unchecked, when it was never committed
        there) over the branch's ticked plan and redo finished tasks. Such a run continues
        inside the branch's worktree without --worktree; the runner then works on the
        checked-out branch and reads its plan with the progress kept.
        """
        repo, argv = self.meta['repo'], list(self.meta['argv'])
        mode = self.meta.get('kind') == 'run' and self.worktree_mode()
        if not mode:
            return repo, argv
        branch = self.run_branch()
        target = Path(self.meta['target'])
        try:
            rel = (target if not target.is_absolute()
                   else target.resolve().relative_to(Path(repo).resolve())).as_posix()
        except ValueError:
            return repo, argv
        if run_quiet(['git', 'rev-parse', '--verify', '-q', f'refs/heads/{branch}'], cwd=repo)[0] != 0 \
                or run_quiet(['git', 'cat-file', '-e', f'{branch}:{rel}'], cwd=repo)[0] != 0:
            return repo, argv  # first start: the runner creates the branch and commits the plan
        wt = Path(repo) / '.ralphex' / 'worktrees' / branch
        if not wt.is_dir():
            run_quiet(['git', 'worktree', 'prune'], cwd=repo)
            code, out = run_quiet(['git', 'worktree', 'add', str(wt), branch], cwd=repo, timeout=60)
            if code != 0:
                raise ResumeBlocked(f'cannot check out {branch} in {wt}: {out}')
        if mode == 'config' or merged([cli_config_paths(None)[0], wt / '.ralphex/config']).get(
                '', {}).get('use_worktree', '').lower() == 'true':
            raise ResumeBlocked(
                f'use_worktree = true would make the runner copy the unticked plan over the '
                f'progress on {branch}; set use_worktree = false (or remove it) and start the run '
                f'again with -- --worktree, which autopilot resumes in {wt}')
        result, skip = [], False
        for arg in argv:
            if skip:
                skip = False
            elif arg == '--branch':
                skip = True
            elif arg == '--worktree' or arg.startswith('--branch='):
                continue
            else:
                result.append(rel if arg == self.meta['target'] else arg)
        if not self.state.get('resumed_in'):
            self.state['resumed_in'] = str(wt)
            log_decision(self.sdir, kind='recovery', by='autopilot',
                         text=f'continued the run on branch {branch} in {wt} without --worktree',
                         reason='the branch already holds the ticked plan; --worktree would '
                                'replace it with the unticked source plan')
        return str(wt), result

    # -- child
    def spawn(self):
        env = {k: v for k, v in os.environ.items() if k not in HOST_SESSION_VARS}
        bindir = self.sdir / 'bin'
        env['PATH'] = str(bindir) + os.pathsep + env.get('PATH', '')
        env['AUTOPILOT_SESSION'] = str(self.sdir)
        env['VISUAL'] = env['EDITOR'] = 'autopilot-editor'
        env['NO_COLOR'] = '1'
        env['PYTHONUTF8'] = '1'
        env['AUTOPILOT_PYTHON'] = sys.executable
        kwargs = {} if WINDOWS else {'start_new_session': True}
        if WINDOWS:
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        cwd, argv = self.launch()
        if self.state.get('skip_external'):
            # Replace explicit flags too: Windows resumes without the review after a stop.
            result, skip = [], False
            for arg in argv:
                if skip:
                    skip = False
                elif arg == '--external-review-tool':
                    skip = True
                elif not arg.startswith('--external-review-tool='):
                    result.append(arg)
            argv = result
            argv.append('--external-review-tool=none')
        self.proc = subprocess.Popen(**command_kwargs(argv), cwd=cwd, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, **kwargs)
        self.state['child_pid'] = self.proc.pid
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        fd = self.proc.stdout.fileno()
        pending = b''
        partial = ''  # an unfinished line: a pipe read can end anywhere
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            pending += chunk
            try:
                text, pending = pending.decode('utf-8'), b''
            except UnicodeDecodeError:
                continue
            with self.lock:
                self.buf += text
                self.last_output = now()
            self.log.write(text)
            self.log.flush()
            lines = (partial + text).split('\n')
            partial = lines.pop()
            for line in lines:
                self.scan_line(clean_line(line), TS_RE.sub('', line))
        if partial:
            self.scan_line(clean_line(partial), TS_RE.sub('', partial))

    def scan_line(self, line, raw=None):
        if self.meta['kind'] == 'plan' and self.scan_review(line, raw if raw is not None else line):
            return
        m = SECTION_RE.match(line)
        if m:
            label = m.group(1)
            phase = next((p for rx, p in PHASES if rx.search(label)), label)
            self.state.update({'section': label, 'phase': phase})
            self.event('section', section=label, phase=phase)
            self.sel_lines = None
            self.flush_done(force=True)
            self.announce_step()  # a previous step that never reported its model
            if self.step and self.step['phase'] != 'implementing tasks':
                self.safe_point(self.step['section'])  # a review round ended
            self.step = {'section': label, 'phase': phase, 'at': now(), 'model': None}
            self.router_mark = self.router_log_mark()  # agrouter logs every selection there
            if phase == 'external review' and self.state.get('skip_external'):
                self.break_external_review()
            return
        m = re.search(r'created (\S+\.md)\s*$', line)
        if m and self.meta['kind'] == 'plan':
            self.state['plan_file'] = m.group(1)
            self.save()
        if self.scan_model(line):
            return
        if self.scan_limit(line):
            return
        m = TRANSIENT_RE.search(line)
        if m:
            self.event('transient', tool=m.group(1), pattern=m.group(2))
            return
        m = ROUTER_WARNING_RE.match(line)
        if m:
            # the classifier-failure warning repeats the selection JSON's fallback: reported there
            if not m.group(1).startswith('Classifier routing failed'):
                self.event('router-warning', text=m.group(1)[:300])
            return
        if line and ERROR_RE.search(line):
            errs = self.state.setdefault('errors', [])
            errs.append({'t': time.strftime('%H:%M:%S'), 'line': line[:300]})
            self.state['errors'] = errs[-8:]
            self.event('error', line=line[:300])

    def scan_review(self, line, raw):
        """The runner's plan review: collect its numbered findings, report its outcome lines.

        The findings come between "plan review findings:" and the findings question, one
        "N. SEVERITY: text" line each. The runner wraps a long finding at the terminal width
        without an indent, and a finding can hold line breaks of its own: every other text line
        belongs to the finding above it, until a section, an outcome line or the question.
        """
        if line == 'plan review findings:':
            self.findings = []
            self.state['review_findings'] = self.findings
            return True
        if self.findings is not None:
            m = REVIEW_FINDING_RE.match(line)
            if m:
                self.findings.append({'n': int(m.group(1)), 'severity': m.group(2),
                                      'text': m.group(3).strip()})
                return True
            if self.findings and line and not FINDINGS_END_RE.search(line) \
                    and not REVIEW_LINE_RE.match(line):
                self.findings[-1]['text'] += ' ' + line
                return True
            self.findings = None
        if not REVIEW_LINE_RE.match(line):
            return False
        if line == 'plan revised':
            self.state['review_revised'] = self.state.get('review_revised', 0) + 1
        what = re.sub(r'^(warning|note): ', '', line).rstrip('.')
        warn = line.startswith(('warning:', 'note:'))
        self.event('plan-review', text=what, warning=warn)
        log_decision(self.sdir, kind='note', by='ralphex', text=f'Plan review: {what}')
        return True

    def scan_model(self, line):
        """Record which CLI/model/effort runs now; True when the line was a selection.

        The runner prints the router's selection either on one line or as a bare
        "agrouter: selection:" header followed by the JSON on the next line(s).
        """
        if self.sel_lines is not None:
            self.sel_lines.append(TS_RE.sub('', line))
            try:
                # the runner word-wraps long lines: a break was a space (JSON allows it between tokens)
                sel = json.loads(' '.join(self.sel_lines))
            except ValueError:
                if len(self.sel_lines) >= 40 or not self.sel_lines[0].startswith('{'):
                    self.sel_lines = None
                    return False
                return True
            self.sel_lines = None
            self.take_selection(sel)
            return True
        if line.rstrip() == 'agrouter: selection:':
            self.sel_lines = []
            return True
        m = SELECTION_RE.search(line)
        if m:
            try:
                sel = json.loads(m.group(1))
            except ValueError:
                return True
            self.take_selection(sel)
            return True
        m = BANNER_RE.match(line)
        if m and m.group(1) != 'model' and self.step and self.use_logged_selection():
            return True  # agrouter's log names what it chose: that beats the banner
        if m:
            model = self.state.setdefault('model', {}) or {}
            if m.group(1) == 'model':
                model = {'cli': 'codex', 'model': m.group(2)}
            else:
                model['effort'] = m.group(2)
            self.state['model'] = model
            if m.group(1) != 'model':
                self.count_model(model)
                if self.step:
                    self.step['model'] = dict(model)
                    self.announce_step()
            self.save()
            return True
        return False

    def codex_step(self, section):
        return (section or '').startswith('codex') or self.meta.get('executor') == 'codex'

    def router_log(self):
        """agrouter's console log for this repo (<log_dir>/agrouter.log), or None."""
        repo = Path(self.meta['repo'])
        log_dir = merged(router_config_paths(repo)).get('agrouter', {}).get('log_dir')
        if not log_dir:
            return None
        path = Path(log_dir).expanduser()
        return (path if path.is_absolute() else repo / path) / 'agrouter.log'

    def router_log_mark(self):
        path = self.router_log()
        try:
            return (path, path.stat().st_size) if path else None
        except OSError:
            return (path, 0)

    def take_selection(self, sel):
        """A selection line in the runner's output: agrouter's log first, this line if not."""
        if not (self.step and self.use_logged_selection()):
            self.use_selection(sel)

    def use_logged_selection(self):
        """Take the step's model from agrouter's log; True when the log had a new selection."""
        sel = self.selection_from_log()
        if not sel:
            return False
        self.step['model_via'] = 'agrouter log'
        self.use_selection(sel)  # fills the step's model and announces it
        return True

    def selection_from_log(self):
        """The last selection agrouter logged since the last read in this step, or None.

        agrouter appends every selection to its console log (<log_dir>/agrouter.log), also when
        the runner hides its stderr (the runner never shows agrouter's line for Codex and
        prints the Codex banner only once). So the log is the model source for every step;
        the runner's output is the fallback when there is no log. Only whole lines are read,
        and the mark moves past them, so a second call in the same step sees only new ones.
        """
        if not self.router_mark or not self.step:
            return None
        want = 'codex' if self.codex_step(self.step['section']) else 'claude'
        path, start = self.router_mark
        try:
            with open(path, 'rb') as fh:
                fh.seek(start)
                data = fh.read(256 * 1024)
        except OSError:
            return None
        whole = data[:data.rfind(b'\n') + 1]
        self.router_mark = (path, start + len(whole))
        found = None
        for line in whole.decode('utf-8', 'replace').splitlines():
            m = re.search(r'agrouter: selection: (\{.*\})\s*$', line)
            if m:
                try:
                    sel = json.loads(m.group(1))
                except ValueError:
                    continue
                if sel.get('cli') in (None, want):
                    found = sel
        return found

    def use_selection(self, sel):
        model = {k: sel.get(k) for k in ('cli', 'model', 'effort')}
        if self.step:
            self.step['selected'] = True  # an agrouter selection line: the router ran this step
        # a value agrouter left to the CLI: record what the CLI really runs with, and why
        effective = sel.get('effective') or {}
        defaults = {}
        for field in ('model', 'effort'):
            eff = effective.get(field)
            if model[field] is None and eff:
                model[field] = eff.get('value')
                defaults[field] = {'value': eff.get('value'), 'source': eff.get('source'),
                                   'why': (eff.get('reason') or {}).get('code')}
        reason = sel.get('reason') or {}
        fallback = sel.get('fallback')
        if fallback or reason:
            model['fallback'] = {'fields': (fallback or {}).get('fields') or list(defaults),
                                 'code': reason.get('code'), 'stage': reason.get('stage'),
                                 'message': reason.get('message'), 'defaults': defaults}
        elif defaults:
            model['defaults'] = defaults  # routing worked and left these to the CLI on purpose
        if (sel.get('readiness') or {}).get('excluded'):
            model['excluded'] = sel['readiness']['excluded']
        self.state['model'] = model
        self.count_model(model)
        if model.get('fallback'):
            tasks = self.plan_progress() or {}
            title = tasks.get('current_task') if self.step and \
                self.step['phase'] == 'implementing tasks' else None
            index, count = self.task_position(title)
            entry = {'t': time.strftime('%H:%M:%S'), 'section': self.state.get('section'),
                     'task': index, 'tasks': count, 'title': title, 'cli': model.get('cli'),
                     **model['fallback']}
            self.state.setdefault('router_fallbacks', []).append(entry)
            self.event('router-fallback', **entry)
            where = entry['title'] or entry['section'] or 'this step'
            log_decision(self.sdir, kind='recovery', by='agrouter',
                         text=f"{where}: not routed, ran on {entry['cli'] or 'CLI'} defaults: "
                              f"{fallback_values(entry)}",
                         reason=' '.join(x for x in (entry.get('message'), entry.get('code')
                                                     and f"({entry['code']})") if x))
        if self.step:
            self.step['model'] = dict(self.state['model'])
            self.announce_step()

    def count_model(self, model):
        """Tally calls per cli/model/effort for the final report."""
        key = '/'.join(str(model.get(k) or '?') for k in ('cli', 'model', 'effort'))
        if model.get('fallback'):
            key += ' (router fallback)'
        used = self.state.setdefault('models_used', {})
        used[key] = used.get(key, 0) + 1
        self.save()

    def scan_limit(self, line):
        """Track a rate-limit episode; True when the line belongs to one."""
        limit = self.state.get('limit')
        m = LIMIT_WAIT_RE.search(line)
        if m:
            wait_s = go_duration(m.group(3)) or 0
            # the router's quota line may have opened the episode: the first wait still counts
            first = not limit or not limit.get('retries')
            limit = limit or {'since': now(), 'retries': 0}
            limit.update({'pattern': m.group(1), 'tool': m.group(2), 'wait_s': wait_s,
                          'next_retry_at': now() + wait_s, 'handled_by': 'ralphex'})
            limit['retries'] += 1
            self.state['limit'] = limit
            if first:
                log_decision(self.sdir, kind='recovery', by='ralphex',
                             text=f"waiting on a {limit['tool']} rate limit, retrying every "
                                  f'{wait_s} s', reason=limit['pattern'])
            self.event('limit' if first else 'limit-retry', tool=limit['tool'],
                       pattern=limit['pattern'], wait_s=wait_s, retries=limit['retries'],
                       resets=limit.get('resets'))
            return True
        starts = STRONG_LIMIT_RE.search(line) or (
            DIAG_PREFIX_RE.search(line) and LIMIT_LINE_RE.search(line))
        if starts or (limit and LIMIT_LINE_RE.search(line)):
            epoch, text = parse_reset(line)
            limit = limit or {'since': now(), 'retries': 0}
            limit['message'] = line[:200]
            if text:
                limit.update({'resets': text, 'resets_at': epoch})
            self.state['limit'] = limit
            self.save()
            return True
        if HELP_LINE_RE.search(line):
            return True
        if limit and line:
            self.state['limit'] = None
            if not limit.get('retries'):
                return False  # a limit message without any wait: nothing to report
            minutes = round((now() - limit['since']) / 60)
            log_decision(self.sdir, kind='recovery', by='ralphex',
                         text=f'rate limit cleared after {minutes} min, work resumed',
                         reason=limit.get('pattern'))
            self.event('limit-cleared', minutes=minutes, retries=limit.get('retries', 0))
        return False

    def send(self, text):
        try:
            self.proc.stdin.write(text.encode('utf-8'))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        with self.lock:
            self.consumed = len(self.buf)

    # -- prompts
    def detect_prompt(self):
        with self.lock:
            if len(self.buf) <= self.consumed:
                return None
            buf, idle = self.buf, now() - self.last_output
        tail = buf[buf.rfind('\n') + 1:]
        lines = buf.splitlines()
        if tail.endswith('Enter your answer: '):
            qfile, ofile = self.sdir / 'fzf-question.txt', self.sdir / 'fzf-options.txt'
            if qfile.exists() and qfile.stat().st_mtime >= self.state.get('answered_at', 0):
                opts = [o for o in ofile.read_text(encoding='utf-8').splitlines()
                        if o.strip() and o != OTHER]
                q = qfile.read_text(encoding='utf-8').strip()
                return {'kind': 'question', 'question': q.rstrip(': ').strip(),
                        'options': opts, 'free_text': True}
            return {'kind': 'text', 'prompt': 'Enter your answer'}
        m = re.search(r'Enter number \(1-(\d+)\): $', tail)
        if m:
            n = int(m.group(1))
            opts, question = [], ''
            for line in reversed(lines[:-1]):
                om = re.match(r'^\s+(\d+)\) (.*)$', line)
                if om and len(opts) < n:
                    opts.insert(0, om.group(2))
                    continue
                if line.strip():
                    question = clean_line(line)
                    break
            if opts == DRAFT_OPTIONS:
                draft = self.extract_draft(buf)
                return {'kind': 'draft', 'question': question,
                        'actions': ['accept', 'revise', 'edited', 'reject'],
                        'draft_file': str(draft) if draft else None,
                        'edited_file': str(self.sdir / 'draft-edited.md')}
            return {'kind': 'choice', 'question': question, 'options': opts,
                    'free_text': OTHER in opts}
        if tail.endswith('[y/N]: '):
            return {'kind': 'yesno', 'question': tail[:-7].strip()}
        if tail.endswith('Enter revision feedback: '):
            return {'kind': 'text', 'prompt': 'revision feedback'}
        if tail.strip() and idle > 5 and re.search(r'[:?]\s*$', tail):
            return {'kind': 'text', 'prompt': tail.strip()}
        return None

    def extract_draft(self, buf):
        start = buf.rfind('━━━ Plan Draft ━━━')
        if start < 0:
            return None
        body = buf[start:].split('\n', 1)[1] if '\n' in buf[start:] else ''
        end = body.find('\n━━━━')
        draft = (body[:end] if end >= 0 else body).strip('\n') + '\n'
        path = self.sdir / 'draft.md'
        if start != self.last_draft:
            # a newly printed draft: write both copies once; later polls keep the user's edits
            path.write_text(draft, encoding='utf-8')
            (self.sdir / 'draft-edited.md').write_text(draft, encoding='utf-8')
            self.last_draft = start
        return path

    def handle_prompt(self, prompt):
        if (prompt['kind'] == 'yesno' and self.meta['kind'] == 'plan'
                and 'Continue with plan implementation' in prompt['question']):
            self.send('n\n')
            log_decision(self.sdir, kind='answer', by='autopilot', prompt='yesno',
                         question=prompt['question'], answer='no',
                         reason='planning never starts implementation by itself')
            self.event('auto-answer', question=prompt['question'], answer='no')
            return
        current = dict(self.state.get('waiting') or {})
        for field in ('id', 'asked_at', 'deadline', 'remind_at', 'reminded', 'policy_key',
                      'review'):
            current.pop(field, None)
        if current != prompt:
            prompt['id'] = self.state['seq'] + 1
            prompt['asked_at'] = now()
            key = prompt['kind'] if self.meta['kind'] == 'plan' or prompt['kind'] == 'text' \
                else 'runtime'
            review = self.review_prompt(prompt)
            if review:
                prompt['review'] = review
                key = 'review_pick' if review['step'] == 'pick' else 'review_apply'
            rule = (self.meta.get('policy') or load_policy()).get(key) or {}
            prompt['policy_key'] = key
            self.set_deadline(prompt, rule.get('timeout'), rule.get('remind_before'))
            self.state['waiting'] = prompt
            self.state['status'] = 'waiting'
            self.event('waiting', prompt=prompt)

    def review_prompt(self, prompt):
        """Context for one of the runner's plan review questions, or None for other prompts.

        `default` is the answer taken when the response time passes with no recommendation
        registered: the external reviewer (else the same agent) for the first round, skip
        after a revision,
        and the CRITICAL and MAJOR findings for the findings question.
        """
        if self.meta['kind'] != 'plan' or prompt['kind'] not in ('question', 'choice'):
            return None
        question = prompt.get('question', '').rstrip(': ').strip()
        offered = [o for o in prompt.get('options') or [] if o != OTHER]
        plan = self.review_plan_file()
        if question == REVIEW_PICK_Q:
            self.state['review_rounds'] = self.state.get('review_rounds', 0) + 1
            gone = 'the runner does not offer it (no external review tool, or the Codex executor)'
            pick = plan_review_pick(
                self.meta.get('executor') or 'claude',
                {REVIEW_EXTERNAL: (REVIEW_EXTERNAL in offered, None if REVIEW_EXTERNAL in offered
                                   else gone),
                 REVIEW_SAME: (REVIEW_SAME in offered, None)},
                revised=self.state.get('review_revised', 0))
            return {'step': 'pick', 'round': self.state['review_rounds'], 'plan_file': plan,
                    'default': {'answer': pick['recommended'], 'reason': pick['recommended_why']}}
        if question == REVIEW_APPLY_Q:
            findings = list(self.state.get('review_findings') or [])
            serious = [str(f['n']) for f in findings if f['severity'] in ('CRITICAL', 'MAJOR')]
            if serious:
                default = {'answer': ','.join(serious),
                           'reason': 'applies the critical and major findings'}
            else:
                default = {'answer': REVIEW_NONE, 'reason': 'only minor findings' if findings
                           else 'the findings could not be read'}
            counts = ', '.join(f"{sum(f['severity'] == s for f in findings)} {s.lower()}"
                               for s in ('CRITICAL', 'MAJOR', 'MINOR'))
            log_decision(self.sdir, kind='note', by='ralphex',
                         text=f"Plan review round {self.state.get('review_rounds', 1)} "
                              f"({self.state.get('review_reviewer') or 'reviewer'}): "
                              f"{len(findings)} findings ({counts})")
            return {'step': 'apply', 'round': self.state.get('review_rounds', 1),
                    'plan_file': plan, 'reviewer': self.state.get('review_reviewer'),
                    'findings': findings, 'default': default}
        return None

    def review_plan_file(self):
        """The plan under review: known for --plan-review, else the newest plan of this session."""
        if self.state.get('plan_file'):
            return self.state['plan_file']
        repo = Path(self.meta['repo'])
        plans = repo / (merged(cli_config_paths(str(repo))).get('', {}).get('plans_dir')
                        or 'docs/plans')
        since = self.meta.get('created', 0) - 2
        found = sorted((f.stat().st_mtime, f) for f in plans.glob('*.md')
                       if f.stat().st_mtime >= since) if plans.is_dir() else []
        return found[-1][1].relative_to(repo).as_posix() if found else None

    def process_inbox(self):
        for path in sorted((self.sdir / 'inbox').glob('*.json')):
            msg = read_json(path, {})
            path.unlink(missing_ok=True)
            if msg.get('stop'):
                self.stop_requested = True
                self.kill_child()
                continue
            if 'stop_after_task' in msg:
                self.state['stop_after'] = bool(msg['stop_after_task'])
                tasks = self.state.get('tasks') or {}
                title = tasks.get('current_task') if self.state.get('phase') == \
                    'implementing tasks' else None
                index, count = self.task_position(title)
                self.event('stop-queued' if msg['stop_after_task'] else 'stop-cancelled',
                           task=index, tasks=count, title=title, section=self.state.get('section'))
                log_decision(self.sdir, kind='decision', by=msg.get('by', 'user'),
                             text=('stop after the current ' + ('task' if title else 'step'))
                             if msg['stop_after_task'] else 'cancelled the stop after this task',
                             reason=msg.get('reason'))
                continue
            if msg.get('skip_external'):
                if not self.state.get('skip_external'):
                    self.state['skip_external'] = True
                    log_decision(self.sdir, kind='decision', by=msg.get('by', 'user'),
                                 text='skip the external code review',
                                 reason=msg.get('reason') or 'asked by the user')
                if self.state.get('phase') == 'external review':
                    self.break_external_review()
                else:
                    self.event('review-skip-queued')
                continue
            if 'extend' in msg:
                waiting = self.state.get('waiting')
                if waiting and msg.get('prompt_id') == waiting.get('id'):
                    self.set_deadline(waiting, msg['extend'])
                    log_decision(self.sdir, kind='note', by='user',
                                 text='more time to answer: ' + (
                                     f"{msg['extend'] // 60} min" if msg['extend'] is not None
                                     else 'no auto-answer'), reason=describe_prompt(waiting))
                    self.event('extended', seconds=msg['extend'])
                continue
            if 'send' not in msg:
                continue
            waiting = self.state.get('waiting') or {}
            if msg.get('prompt_id') != waiting.get('id'):
                self.event('stale-answer', summary=msg.get('summary', ''))
                continue
            if waiting:
                self.deliver(waiting, msg['send'], msg.get('summary', ''),
                             msg.get('by', 'user'), msg.get('reason'))

    def deliver(self, waiting, send, summary, by, reason=None, recommended=None):
        self.send(send)
        extra = {}
        if by == 'auto':
            # timeouts get their own report section: how long nobody answered, and whether
            # the answer was the agent's recommendation or the driver's blind fallback
            extra = {'waited_min': round((now() - waiting.get('asked_at', now())) / 60, 1),
                     'recommended': bool(recommended)}
        self.state['answered_at'] = now()
        self.state['waiting'] = None
        self.state['status'] = 'running'
        if (waiting.get('review') or {}).get('step') == 'pick':
            self.state['review_reviewer'] = summary
        log_decision(self.sdir, kind='answer', by=by, prompt=waiting['kind'],
                     question=describe_prompt(waiting), options=waiting.get('options'),
                     answer=summary, reason=reason, **extra)
        self.event('auto-answered' if by == 'auto' else 'answered',
                   summary=summary, by=by, reason=reason)

    @staticmethod
    def set_deadline(prompt, timeout, remind_before=None):
        prompt['deadline'] = now() + timeout if timeout is not None else None
        prompt['remind_at'] = (prompt['deadline'] - remind_before
                               if prompt['deadline'] and remind_before else None)
        prompt['reminded'] = False

    def break_external_review(self):
        """End the running external review: SIGQUIT is the runner's manual break.

        The runner treats SIGQUIT as "end the external review loop" only in that phase; during
        tasks it pauses instead, so this is called only while the review runs. Windows has no
        SIGQUIT: the attempt is stopped and restarted with --external-review-tool=none.
        """
        if not self.proc or self.proc.poll() is not None:
            return
        if WINDOWS or not hasattr(signal, 'SIGQUIT'):
            self.restart_now = True
            kill_tree(self.proc.pid)
            self.event('review-skipped', how='restart without external review')
            return
        os.kill(self.proc.pid, signal.SIGQUIT)
        self.event('review-skipped', how='manual break (SIGQUIT)')

    def check_deadline(self):
        waiting = self.state.get('waiting')
        if not waiting:
            return
        if waiting.get('remind_at') and not waiting.get('reminded') \
                and now() >= waiting['remind_at'] and not answer_pending(self.sdir, self.state):
            waiting['reminded'] = True
            self.event('reminder', question=describe_prompt(waiting),
                       left_s=int(waiting['deadline'] - now()))
        if waiting.get('deadline') is None or now() < waiting['deadline']:
            return
        if answer_pending(self.sdir, self.state):
            return
        rec = read_json(self.sdir / 'recommend.json', {}) or {}
        if rec.get('prompt_id') == waiting['id']:
            minutes = round((now() - waiting['asked_at']) / 60)
            choice = (rec['send'], rec['summary'],
                      f'no reply in {minutes} min: took the recommended option. '
                      + (rec.get('reason') or ''))
            recommended = True
        else:
            choice = fallback_answer(waiting)
            recommended = False
        if not choice:
            if not waiting.get('needs_input'):
                waiting['needs_input'] = True
                self.event('needs-input', question=describe_prompt(waiting))
            return
        self.deliver(waiting, choice[0], choice[1], 'auto', choice[2].strip(),
                     recommended=recommended)

    # -- progress
    def plan_progress(self):
        target = self.state.get('plan_file') or (
            self.meta['target'] if self.meta['kind'] == 'run' else None)
        if not target:
            return None
        repo = Path(self.meta['repo'])
        rel = Path(target)
        if rel.is_absolute():
            try:
                rel = rel.resolve().relative_to(repo.resolve())
            except ValueError:
                rel = None
        # a worktree run ticks the copy inside its worktree, not the source checkout's plan
        candidates = [repo / target]
        if rel is not None and self.meta['kind'] == 'run':
            candidates = [repo / '.ralphex' / 'worktrees' / self.run_branch() / rel] + candidates
        path = next((c for c in candidates if c.exists()), None)
        if not path:
            return None
        self.plan_path = path
        done = total = 0
        current = None
        task = None
        counts = {}  # title -> [done, total], in plan order
        items = {}   # title -> [(ticked, text)]
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            hm = re.match(r'^###\s+(.*)', line)
            if hm:
                task = hm.group(1).strip()
            cm = re.match(r'^\s*- \[([ xX])\]\s*(.*)', line)
            if cm:
                total += 1
                items.setdefault(task, []).append((cm.group(1) != ' ', cm.group(2).strip()))
                c = counts.setdefault(task, [0, 0])
                c[1] += 1
                if cm.group(1) != ' ':
                    done += 1
                    c[0] += 1
                elif current is None:
                    current = task
        self.plan_tasks = [(t, d, n) for t, (d, n) in counts.items()]
        self.plan_items = items
        return {'done': done, 'total': total, 'current_task': current}

    # -- milestones
    def task_position(self, title):
        titles = [t for t, _, _ in self.plan_tasks]
        return (titles.index(title) + 1 if title in titles else None), len(titles)

    def announce_step(self, timed_out=False):
        """One event per step: task-start (task, model, steps) or phase-start (review, ...)."""
        step = self.step
        if not step or step.get('announced') or not (step['model'] or timed_out):
            return
        if not step['model'] and self.router_mark and self.use_logged_selection():
            return
        step['announced'] = True
        model = step['model']
        how = model_how(model, self.setup, step['phase'], step['section'], step.get('selected'))
        via = step.get('model_via')
        steps = self.state.setdefault('steps', [])
        steps.append({'t': time.strftime('%H:%M:%S'), 'section': step['section'],
                      'phase': step['phase'], 'model': model_label(model, how), 'how': how,
                      **({'via': via} if via else {})})
        self.state['steps'] = steps[-300:]
        if step['phase'] != 'implementing tasks':
            self.event('phase-start', section=step['section'], phase=step['phase'], model=model,
                       how=how, **({'via': via} if via else {}))
            return
        tasks = self.plan_progress() or {}
        self.state['tasks'] = tasks
        title = tasks.get('current_task')
        index, count = self.task_position(title)
        log = self.state['task_log']
        entry = log.get(title) if title else None
        continued = bool(entry)
        if title and not entry:
            entry = log[title] = {'index': index, 'started': now(), 'models': [], 'iterations': 0}
        if entry is not None:
            entry['iterations'] += 1
            key = model_label(model)
            if key not in entry['models']:
                entry['models'].append(key)
        m = re.search(r'(\d+)\s*$', step['section'])
        todo = [t for ticked, t in getattr(self, 'plan_items', {}).get(title, []) if not ticked]
        about = steps_summary(todo)
        if entry is not None and about and not entry.get('about'):
            entry['about'] = about
        self.event('task-start', task=index, tasks=count, title=title, model=model, how=how,
                   about=about,
                   iteration=int(m.group(1)) if m else None, continued=continued,
                   steps_done=tasks.get('done'), steps_total=tasks.get('total'))

    def agent_activity_age(self):
        """Seconds since the agent CLI last wrote its transcript for this run, or None.

        Claude Code writes ~/.claude/projects/<cwd with non-alphanumerics as ->/<id>.jsonl on
        every message and tool call; Codex writes today's rollout files under
        ~/.codex/sessions/. Only files written after the current step started count.
        """
        if not self.step:
            return None
        t = now()
        if t - getattr(self, 'activity_checked', 0) < 30:
            return getattr(self, 'activity_age', None)
        self.activity_checked = t
        cwd = self.state.get('resumed_in') or self.meta['repo']
        dirs = [Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'projects'
                / re.sub(r'[^A-Za-z0-9]', '-', str(Path(cwd).resolve())),
                Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'sessions'
                / time.strftime('%Y/%m/%d')]
        newest = None
        for folder in dirs:
            try:
                for f in folder.glob('*.jsonl'):
                    m = f.stat().st_mtime
                    if m >= self.step['at'] - 5 and (newest is None or m > newest):
                        newest = m
            except OSError:
                continue
        self.activity_age = None if newest is None else max(0.0, t - newest)
        return self.activity_age

    def heartbeat(self, active):
        """One "still running" event when a step had no news for HEARTBEAT_S."""
        step = self.step
        if not step or not step.get('announced') or self.state.get('waiting') \
                or self.state.get('limit') or self.state.get('status') != 'running':
            return
        t = now()
        if t - max(step['at'], getattr(self, 'last_news', 0)) < HEARTBEAT_S:
            return
        title = (self.state.get('tasks') or {}).get('current_task') \
            if step['phase'] == 'implementing tasks' else None
        index, count = self.task_position(title)
        self.event('heartbeat', section=step['section'], phase=step['phase'], title=title,
                   task=index, tasks=count, minutes=round((t - step['at']) / 60),
                   output_min=round((t - self.last_output) / 60),
                   active_min=None if active is None else round(active / 60))

    def git_here(self, *argv):
        where = self.plan_path.parent if self.plan_path else Path(self.meta['repo'])
        return run_quiet(['git', *argv], cwd=where)

    def new_commits(self):
        """[(hash, subject)] committed since the last check, oldest first."""
        code, head = self.git_here('rev-parse', '--short', 'HEAD')
        if code != 0 or not head:
            return []
        where = str(self.plan_path.parent if self.plan_path else self.meta['repo'])
        if self.head is None or head == self.head or where != self.head_where:
            # first look, nothing new, or progress moved into a worktree: new baseline
            self.head, self.head_where = head, where
            return []
        code, out = self.git_here('log', '--reverse', '--format=%h%x09%s', f'{self.head}..HEAD')
        self.head = head
        return [tuple(l.split('\t', 1)) for l in out.splitlines() if '\t' in l] if code == 0 else []

    def track_progress(self):
        """Called every few seconds: record progress, report finished tasks and new commits."""
        self.state['tasks'] = self.plan_progress()
        finished = {t for t, d, n in self.plan_tasks if n and d == n}
        if self.complete is None:
            self.complete = finished  # done before this session: not news
        for title in [t for t, _, _ in self.plan_tasks if t in finished - self.complete]:
            self.done_pending.append({'title': title, 'at': now()})
        self.complete |= finished
        commits = self.new_commits()
        # the runner ticks a task's boxes, then commits it: the newest commits belong to
        # the newest finished tasks, anything older is a commit of its own (review fixes, ...)
        paired = min(len(commits), len(self.done_pending))
        for h, subject in commits[:len(commits) - paired]:
            self.event('commit', commit=h, subject=subject)
        for pending, commit in zip(self.done_pending[:paired], commits[len(commits) - paired:]):
            self.emit_done(pending, commit)
        del self.done_pending[:paired]
        self.flush_done()
        step = self.step
        if step and not step.get('announced') and now() - step['at'] > 20:
            self.announce_step(timed_out=True)
        # the runner prints little in a review: the agent's own transcript shows it works
        active = self.agent_activity_age()
        idle = now() - self.last_output
        if active is not None:
            idle = min(idle, active)
        self.heartbeat(active)
        if idle < 60:
            self.stalled = False
        elif not self.stalled and idle > STALL_S and not self.state.get('waiting') \
                and not self.state.get('limit'):
            self.stalled = True
            self.event('stalled', minutes=round(idle / 60), task=self.task_position(
                (self.state.get('tasks') or {}).get('current_task'))[0],
                title=(self.state.get('tasks') or {}).get('current_task'),
                section=self.state.get('section'))
        self.save()

    def flush_done(self, force=False):
        """Report finished tasks whose commit did not show up within a minute."""
        while self.done_pending and (force or now() - self.done_pending[0]['at'] > 60):
            self.emit_done(self.done_pending.pop(0), None)

    def commit_changes(self, commit):
        """'3 files (+120 -8): a.go, b.go, c_test.go' for one commit, or None.

        The plan file is left out: the runner commits its ticked boxes with the code.
        """
        code, out = self.git_here('show', '--numstat', '--format=', commit)
        if code != 0:
            return None
        plan = self.plan_path.name if self.plan_path else None
        files, added, removed = [], 0, 0
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) != 3 or (plan and Path(parts[2]).name == plan):
                continue
            files.append(parts[2])
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
        if not files:
            return None
        shown = ', '.join(Path(f).name for f in files[:4]) + (f', +{len(files) - 4} more'
                                                             if len(files) > 4 else '')
        return f"{len(files)} file{'s' if len(files) != 1 else ''} (+{added} -{removed}): {shown}"

    def emit_done(self, pending, commit):
        title = pending['title']
        index, count = self.task_position(title)
        entry = self.state['task_log'].setdefault(
            title, {'index': index, 'started': None, 'models': [], 'iterations': 0})
        entry['done'] = now()
        if commit:
            entry['commit'] = f'{commit[0]} {commit[1]}'
        minutes = round((entry['done'] - entry['started']) / 60) if entry.get('started') else None
        entry['minutes'] = minutes
        did = steps_summary([t for ticked, t in getattr(self, 'plan_items', {}).get(title, [])
                             if ticked])
        changes = self.commit_changes(commit[0]) if commit else None
        entry['did'], entry['changes'] = did, changes
        tasks = self.state.get('tasks') or {}
        self.event('task-done', task=index, tasks=count, title=title, minutes=minutes,
                   models=entry['models'], iterations=entry['iterations'],
                   commit=commit[0] if commit else None, subject=commit[1] if commit else None,
                   did=did, changes=changes,
                   steps_done=tasks.get('done'), steps_total=tasks.get('total'))
        self.safe_point(f'Task {index}/{count}' if index else title)

    # -- waking the chat in charge
    def needs_chat(self):
        """Why the owner chat is needed now, or None: unseen news, an open prompt, an unreported end."""
        w = watch_status(self.sdir)
        if w['watched'] or w['reported'] or self.superseded_in_flow():
            return None
        if self.state.get('waiting'):
            return 'a question is waiting'
        if self.state.get('status') in ('done', 'failed', 'stopped'):
            return f"the run ended ({self.state['status']})"
        important = [e for e in events_since(self.sdir, self.state, w['seen'])
                     if e['kind'] in WAKE_EVENTS]
        return f'{len(important)} update(s) not seen' if important else None

    def check_wake(self):
        """Wake the owner chat when nobody watches and something needs it.

        After its own usage limit, or when its turn ended without a watcher, nothing else wakes
        an idle chat in most hosts. The supervisor is always alive, so it types one line into the
        owner's terminal tab (any agent CLI reads it as a user message), or shows a desktop
        notification when the tab cannot be typed into. Repeats every WAKE_EVERY_S until watched.
        """
        t = now()
        if t - getattr(self, 'wake_checked', 0) < WAKE_CHECK_S:
            return
        self.wake_checked = t
        mode = self.meta.get('wake') or wake_mode()
        if mode == 'off':
            return
        why = self.needs_chat()
        if not why:
            self.unwatched_since = None
            return
        self.unwatched_since = getattr(self, 'unwatched_since', None) or t
        if t - self.unwatched_since < WAKE_AFTER_S or t - getattr(self, 'woke_at', 0) < WAKE_EVERY_S:
            return
        self.woke_at = t
        meta = read_json(self.sdir / 'session.json', {}) or self.meta
        tab = meta.get('tab') if mode == 'type' else None
        text = (f'autopilot: reattach to run {self.sdir.name} ({why}): run watch-status and '
                'next --catch-up, then post the missed updates')
        how = 'notification'
        if tab:
            code, out = run_quiet(wake_argv(tab, text), timeout=20)
            how = f"typed into the {tab['kind']} tab" if code == 0 else 'notification'
            if code != 0:
                self.state['wake_error'] = out[-200:]
        if how == 'notification':
            notify('autopilot', f'{self.sdir.name}: {why}. Open the chat that runs it.')
        self.event('wake', how=how, why=why)

    def superseded_in_flow(self):
        """A later session of the same flow (a resume, a recovery restart) carries on."""
        flow, root = self.meta.get('flow'), self.sdir.parent
        return bool(flow) and any(
            d.name > self.sdir.name and (read_json(d / 'session.json', {}) or {}).get('flow') == flow
            for d in root.iterdir() if d.is_dir())

    def linger(self):
        """After the end, keep waking the owner chat until it has seen the end (bounded)."""
        if (self.meta.get('wake') or wake_mode()) == 'off':
            return
        self.state['lingering'] = True
        self.save()
        end = now() + LINGER_S
        while now() < end and self.needs_chat():
            self.check_wake()
            time.sleep(min(5, WAKE_CHECK_S))

    def kill_child(self):
        """Stop the runner's process group and reap it.

        kill_tree checks liveness with signal 0, which an unreaped child (a zombie) still
        passes; the Popen handle sees the exit, so stopping takes a moment, not 30 s.
        """
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        if WINDOWS:
            kill_tree(proc.pid)
            return
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            for _ in range(50):
                if proc.poll() is not None:
                    return
                time.sleep(0.2)

    def safe_point(self, where):
        """A task or step just finished: stop here when the user asked to."""
        if not self.state.get('stop_after') or self.stop_requested:
            return
        self.stop_requested = True
        self.state['stopped_after'] = where
        self.kill_child()

    # -- main loop
    def attempt(self):
        self.state['attempt'] += 1
        with self.lock:
            self.buf, self.consumed = '', 0
        # a prompt of the previous process is gone with it: never answer it into the new one
        stale = self.state.get('waiting')
        self.state['waiting'] = None
        self.last_draft = None
        if stale:
            self.event('prompt-dropped', prompt_id=stale.get('id'),
                       question=describe_prompt(stale), reason='the runner restarted')
        self.log.write(f"\n===== autopilot attempt {self.state['attempt']} "
                       f"{time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        self.log.flush()
        try:
            self.spawn()
        except ResumeBlocked as exc:
            return exc
        self.state['status'] = 'running'
        self.event('started', attempt=self.state['attempt'])
        if self.head is None:
            self.plan_progress()
            self.new_commits()
        checked = now()
        while self.proc.poll() is None:
            self.process_inbox()
            prompt = self.detect_prompt()
            if prompt:
                self.handle_prompt(prompt)
            self.check_deadline()
            if now() - checked >= PROGRESS_S:
                checked = now()
                self.track_progress()
                self.check_wake()
            time.sleep(0.2)
        time.sleep(0.3)  # let the reader drain
        self.track_progress()
        self.flush_done(force=True)
        self.announce_step(timed_out=True)
        self.step = None
        return self.proc.returncode

    def classify(self):
        with self.lock:
            lines = [clean_line(l) for l in self.buf.splitlines()[-80:]]
        lines = [l for l in lines if l]
        failed = next((l for l in reversed(lines) if l.startswith('Failed:')), None)
        err = failed or next((l for l in reversed(lines) if ERROR_RE.search(l)), None) \
            or next((l for l in reversed(lines) if EXIT_RE.search(l)), None)
        err = err or (lines[-1] if lines else 'exited without output')
        text = '\n'.join(lines[-30:])
        if FATAL_RE.search(text):
            return 'fatal', err
        if self.state.get('limit') or STRONG_LIMIT_RE.search(text):
            limit_line = (self.state.get('limit') or {}).get('message')
            return 'limit', limit_line or err
        return 'transient', err

    def run(self):
        self.save()
        max_restarts = int(self.meta.get('max_restarts', 5))
        streak, last_sig = 0, None
        while True:
            rc = self.attempt()
            if isinstance(rc, ResumeBlocked):
                self.state['status'] = 'failed'
                self.state['last_error'] = str(rc)
                log_decision(self.sdir, kind='recovery', by='autopilot',
                             text='did not restart: it would redo finished tasks', reason=str(rc))
                self.event('failed', error=str(rc), category='fatal',
                           reason='restarting would lose the progress on the run branch')
                return
            self.state['exit_code'] = rc
            if self.stop_requested:
                self.state['status'] = 'stopped'
                self.event('stopped', after=self.state.get('stopped_after'))
                return
            if rc == 0:
                self.state['status'] = 'done'
                self.state['waiting'] = None
                self.event('done', plan_file=self.state.get('plan_file'))
                return
            if self.restart_now:
                self.restart_now = False
                continue
            category, err = self.classify()
            self.state['last_error'] = err
            with self.lock:
                recent = '\n'.join(self.buf.splitlines()[-60:])
            if self.state.get('phase') == 'external review' and CODEX_DOWN_RE.search(recent) \
                    and not self.state.get('skip_external') and self.meta.get('executor') != 'codex':
                self.state['skip_external'] = True
                log_decision(self.sdir, kind='recovery', by='autopilot',
                             text='codex failed during the external review: restarted without it',
                             reason=err)
                self.event('restart', error=err, category='review-skipped', delay_s=0,
                           restart=len(self.state['restarts']) + 1)
                self.state['restarts'].append({'t': time.strftime('%H:%M:%S'), 'error': err,
                                               'category': 'review-skipped', 'exit_code': rc})
                continue
            if self.meta.get('executor') == 'claude' and category != 'limit' \
                    and self.state.get('phase') != 'external review' \
                    and CLAUDE_DOWN_RE.search('\n'.join(clean_line(l) for l in recent.splitlines())):
                cfg = merged(cli_config_paths(self.meta['repo'])).get('', {})
                cap = review_capability(self.meta['repo'], cfg)
                blocker = codex_fallback_blocker(self.meta['repo'], cfg, self.meta['argv'])
                if cap['codex']['usable'] and not blocker:
                    self.meta['argv'] = codex_fallback_args(self.meta['argv'])
                    self.meta['executor'] = 'codex'
                    write_json(self.sdir / 'session.json', self.meta)
                    self.state.update({'waiting': None, 'model': None, 'phase': None})
                    log_decision(self.sdir, kind='recovery', by='autopilot',
                                 text='Claude unavailable: restarted with the Codex executor; '
                                      'phase models reset to routing', reason=err)
                    self.state['restarts'].append({'t': time.strftime('%H:%M:%S'), 'error': err,
                                                   'category': 'executor-fallback', 'exit_code': rc})
                    self.event('restart', error=err, category='executor-fallback', delay_s=0,
                               restart=len(self.state['restarts']), executor='codex')
                    continue
                reason = blocker if cap['codex']['usable'] else cap['codex']['reason']
                self.state['status'] = 'failed'
                log_decision(self.sdir, kind='recovery', by='autopilot',
                             text=f'Claude unavailable; Codex fallback unavailable: {reason}', reason=err)
                self.event('failed', error=err, category='fatal', exit_code=rc,
                           reason=f'Claude unavailable; Codex fallback unavailable: {reason}')
                return
            if category == 'limit':
                if self.wait_out_limit(err):
                    continue
                return
            tasks = self.state.get('tasks') or {}
            sig = (err, tasks.get('done'))
            streak = streak + 1 if sig == last_sig else 1
            last_sig = sig
            used = len([r for r in self.state['restarts'] if r['category'] != 'limit'])
            reason = None
            if category == 'fatal':
                reason = 'needs a fix before it can continue'
            elif used >= max_restarts:
                reason = f'restart limit reached ({max_restarts})'
            elif streak >= 3:
                reason = 'same error three times with no progress'
            if reason:
                self.state['status'] = 'failed'
                log_decision(self.sdir, kind='recovery', by='autopilot',
                             text=f'stopped restarting: {reason}', reason=err)
                self.event('failed', error=err, category=category, reason=reason, exit_code=rc)
                return
            delay = min(int(os.environ.get('AUTOPILOT_BACKOFF', 30)) * 2 ** used, 600)
            self.state['restarts'].append({'t': time.strftime('%H:%M:%S'), 'error': err,
                                           'category': category, 'exit_code': rc})
            log_decision(self.sdir, kind='recovery', by='autopilot',
                         text=f'restarted after a {category} failure (restart {used + 1}, '
                              f'after {delay} s)', reason=err)
            self.state['status'] = 'restarting'
            self.state['next_attempt_at'] = now() + delay
            self.event('restart', error=err, category=category, delay_s=delay,
                       restart=used + 1, max_restarts=max_restarts)
            if not self.sleep_interruptible(delay):
                return

    def sleep_interruptible(self, delay):
        """Sleep while serving the inbox; False when a stop arrived."""
        end = now() + delay
        while now() < end:
            self.process_inbox()
            if self.stop_requested:
                self.state['status'] = 'stopped'
                self.event('stopped')
                return False
            self.check_wake()
            time.sleep(0.5)
        return True

    def wait_out_limit(self, err):
        """The CLI exited on a limit: wait for the reset and restart, or hand over."""
        limit = self.state.get('limit') or {'since': now(), 'retries': 0}
        default = int(os.environ.get('AUTOPILOT_LIMIT_WAIT', 900))
        longest = int(os.environ.get('AUTOPILOT_MAX_LIMIT_WAIT', 6 * 3600))
        margin = int(os.environ.get('AUTOPILOT_LIMIT_MIN', 60))
        resets_at = limit.get('resets_at')
        delay = max(margin, int(resets_at - now()) + margin) if resets_at else default
        if delay > longest:
            self.state['status'] = 'failed'
            self.state['limit'] = dict(limit, handled_by='agent')
            reason = (f"limit resets in {round(delay / 3600, 1)} h "
                      f"({limit.get('resets', 'unknown time')}): needs a decision")
            log_decision(self.sdir, kind='recovery', by='autopilot',
                         text='handed a long rate limit to the agent', reason=f'{err}; {reason}')
            self.event('failed', error=err, category='limit', reason=reason,
                       resets=limit.get('resets'), resets_in_s=delay)
            return False
        limit.update({'handled_by': 'autopilot', 'next_retry_at': now() + delay})
        limit['retries'] = limit.get('retries', 0) + 1
        self.state['limit'] = limit
        self.state['restarts'].append({'t': time.strftime('%H:%M:%S'), 'error': err,
                                       'category': 'limit', 'exit_code': self.state['exit_code']})
        log_decision(self.sdir, kind='recovery', by='autopilot',
                     text=f'waiting {delay} s for a rate limit, then restarting',
                     reason=f"{err}; resets {limit.get('resets', 'unknown')}")
        self.state['status'] = 'restarting'
        self.state['next_attempt_at'] = now() + delay
        self.event('limit', tool=limit.get('tool'), pattern=err, wait_s=delay,
                   resets=limit.get('resets'), handled_by='autopilot')
        return self.sleep_interruptible(delay)


def log_decision(sdir, **entry):
    entry = {'t': time.strftime('%Y-%m-%d %H:%M:%S'), **entry}
    with open(Path(sdir) / 'decisions.jsonl', 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + '\n')


def describe_prompt(w):
    return w.get('question') or w.get('prompt') or w.get('kind')


def fallback_answer(w):
    """Safe default when nobody answered and no recommendation was registered."""
    kind = w['kind']
    default = (w.get('review') or {}).get('default')
    if default:
        send, summary = build_answer(w, argparse.Namespace(
            text=default['answer'], choice=None, text_file=None))
        return send, summary, f"took the default: {default['reason']}"
    if kind in ('question', 'choice') and w.get('options'):
        first = w['options'][0]
        send = first + '\n' if kind == 'question' else '1\n'
        return send, first, 'no reply and no recommendation: took the first option'
    if kind == 'draft':
        return '1\n', 'accept', 'no reply: accepted the draft as written'
    if kind == 'yesno':
        return 'n\n', 'no', 'no reply: answered no (the safe default)'
    return None


def cmd_supervise(args):
    sup = Supervisor(args.sdir)
    try:
        sup.run()
        sup.linger()
    except Exception as exc:  # report, never leave a silent dead session
        sup.state['status'] = 'failed'
        sup.event('failed', error=f'supervisor crashed: {exc!r}', category='internal',
                  reason='autopilot helper error')
        raise


# ---------------------------------------------------------------- client side

def report(sdir, st, since):
    meta = read_json(sdir / 'session.json', {})
    log = sdir / 'output.log'
    activity = []
    if log.exists():
        with open(log, 'rb') as fh:
            offset = max(0, log.stat().st_size - 16000)
            fh.seek(offset)
            tail = fh.read().decode('utf-8', 'replace').splitlines()[1 if offset else 0:]
        activity = [clean_line(l)[:220] for l in tail if clean_line(l)
                    and not clean_line(l).startswith('=====')][-10:]
    alive = pid_alive(st.get('pid'))
    status, waiting = st.get('status'), st.get('waiting')
    fresh = events_since(sdir, st, since)
    if answer_pending(sdir, st):
        status, waiting = 'running', None
    if status in ('running', 'waiting', 'restarting', 'starting') and not alive:
        status = 'lost'
    return {
        'session': sdir.name, 'kind': meta.get('kind'), 'target': meta.get('target'),
        'status': status, 'seq': st.get('seq', 0),
        'new_events': [dict(e, line=event_line(e)) for e in fresh],
        'updates': [event_line(e) for e in fresh if event_line(e)],
        'phase': st.get('phase'), 'section': st.get('section'), 'tasks': st.get('tasks'),
        'waiting': waiting, 'plan_file': st.get('plan_file'), 'flow': meta.get('flow'),
        'auto_answer_in_s': max(0, int(waiting['deadline'] - now()))
        if waiting and waiting.get('deadline') else None,
        'auto_answer': (waiting or {}).get('policy_key'),
        'attempt': st.get('attempt'), 'restarts': len(st.get('restarts', [])),
        'last_error': st.get('last_error'), 'recent_errors': st.get('errors', [])[-3:],
        'elapsed_min': round((now() - st.get('started', now())) / 60, 1),
        'limit': limit_view(st.get('limit')),
        'model': st.get('model'), 'executor': meta.get('executor'),
        'activity': activity, 'log': str(log),
    }


def events_since(sdir, st, since):
    """Events after since: from state (the last 40), or the full timeline when older."""
    recent = st.get('events', [])
    if not recent or recent[0]['seq'] <= since + 1:
        return [e for e in recent if e['seq'] > since]
    out = []
    path = sdir / 'events.jsonl'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get('seq', 0) > since:
                out.append(e)
    # a restart of the supervisor starts seq again: keep only the current run of numbers
    seqs = [e['seq'] for e in out]
    if seqs and seqs != sorted(seqs):
        cut = max(i for i in range(1, len(seqs)) if seqs[i] < seqs[i - 1])
        out = out[cut:]
    return out or [e for e in recent if e['seq'] > since]


WATCH_GAP_S = float(os.environ.get('AUTOPILOT_WATCH_GAP_S', 180))  # a chat restarts its watch


def short_text(text, limit=80):
    """The first line of a long text (a task description can be kilobytes), cut to limit."""
    first = (str(text or '').strip().splitlines() or [''])[0].strip()
    return first if len(first) <= limit else first[:limit - 3].rstrip() + '...'


def watch_status(sdir):
    """Who watches a run, and how much the chat has not seen yet."""
    meta = read_json(sdir / 'session.json', {}) or {}
    st = read_json(sdir / 'state.json', {}) or {}
    watcher = read_json(sdir / 'watcher.json', {}) or {}
    seen = (read_json(sdir / 'seen.json', {}) or {}).get('seq', 0)
    alive = session_active(st)
    unseen = [e for e in events_since(sdir, st, seen) if event_line(e)]
    tasks = st.get('tasks') or {}
    return {'session': sdir.name, 'kind': meta.get('kind'), 'target': meta.get('target'),
            'flow': meta.get('flow'), 'status': st.get('status') if alive or st.get('status') in
            ('done', 'failed', 'stopped') else 'lost',
            'alive': alive, 'owner': meta.get('owner'),
            'watched': bool(watcher.get('pid')) and pid_alive(watcher.get('pid')),
            'between_watches': not watcher.get('pid') and now() - (watcher.get('ended') or 0)
            < WATCH_GAP_S,
            'seen': seen, 'seq': st.get('seq', 0), 'unseen_updates': len(unseen),
            'waiting': bool(st.get('waiting')), 'tasks': tasks,
            'reported': bool((read_json(sdir / 'reported.json', {}) or {}).get('t'))}


def cmd_watch_status(args):
    """For the watchdog and the hooks: runs of this repo that need a chat's attention."""
    if args.session:
        emit({'sessions': [watch_status(session_dir(args.session))]})
        return
    root = state_root() / 'sessions'
    repo = git_root()
    out = []
    if root.exists():
        for d in sorted(root.iterdir(), reverse=True)[:40]:
            meta = read_json(d / 'session.json', {}) or {}
            if not meta or (repo and not in_repo(meta, repo)):
                continue
            w = watch_status(d)
            # live runs, and ended ones whose end the chat has not seen or reported
            if w['alive'] or (w['unseen_updates'] and not w['reported']):
                out.append(w)
    emit({'sessions': out})


def runs_needing_attention(cwd):
    """watch_status of this repo's runs that are live and unwatched, or ended unseen."""
    try:
        os.chdir(cwd)
    except OSError:
        return []
    repo = git_root()
    root = state_root() / 'sessions'
    if not repo or not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir(), reverse=True)[:40]:
        meta = read_json(d / 'session.json', {}) or {}
        # runs started before owners and watcher leases existed cannot tell who watches them
        if not in_repo(meta, repo) or 'owner' not in meta:
            continue
        w = watch_status(d)
        if (w['alive'] and not w['watched']) or (
                not w['alive'] and w['unseen_updates'] and not w['reported']):
            out.append(w)
    return out


def describe_run(w, chat):
    tasks = w.get('tasks') or {}
    where = f" · {tasks.get('done')}/{tasks.get('total')} steps, now {tasks['current_task']}" \
        if tasks.get('current_task') else ''
    who = 'This chat started it' if w.get('owner') and w.get('owner') == chat \
        else 'Another chat started it'
    state = (f"is {w['status']}{where}; no chat is watching it" if w['alive']
             else f"ended ({w['status']}) and its end was not reported yet")
    return (f"autopilot {w['kind']} session {w['session']} ({short_text(w['target'])}) {state}; "
            f"{w['unseen_updates']} update(s) not posted yet. {who}.")


# ---------------------------------------------------------------- compaction before the run

COMPACT_IDLE_S = float(os.environ.get('AUTOPILOT_COMPACT_IDLE_S', 900))   # wait for the turn end
COMPACT_DONE_S = float(os.environ.get('AUTOPILOT_COMPACT_DONE_S', 600))   # wait for the compaction
COMPACT_NO_HOOK_S = float(os.environ.get('AUTOPILOT_COMPACT_NO_HOOK_S', 180))  # host without a hook
COMPACT_POLL_S = float(os.environ.get('AUTOPILOT_COMPACT_POLL_S', 1))


def compact_dir():
    return state_root() / 'compact'


def compact_mode():
    """type (type /compact into the chat's tab) or off."""
    mode = (os.environ.get('AUTOPILOT_COMPACT') or autopilot_config('compact').get('mode')
            or 'type').lower()
    return mode if mode in ('type', 'off') else 'type'


def compact_requests(chat, status):
    """Open compaction requests of this chat in the given status."""
    root = compact_dir()
    if not chat or not root.exists():
        return []
    out = []
    for path in sorted(root.glob('*.json')):
        req = read_json(path, {}) or {}
        if req.get('owner') == chat and req.get('status') == status:
            out.append((path, req))
    return out


def resume_line(req):
    return (f"autopilot: context compacted. Continue flow {req['flow']} at Step 5 (Run): read "
            f"{req['handoff']} and start the run as it says")


def cmd_compact(args):
    """Compact the chat's context at the end of planning, then wake it to start the run.

    No agent host lets the agent compact its own context. The chat writes a handoff file,
    calls this and ends its turn. A detached helper waits for the Stop hook (the turn ended),
    types `/compact <focus>` into the chat's terminal tab, waits for the SessionStart
    (compact) hook, and types the resume line. The SessionStart hook also puts the handoff
    pointer into the new context.
    """
    handoff = Path(args.handoff).expanduser().resolve()
    if not handoff.is_file():
        die(f'handoff file not found: {handoff}')
    tab = own_tab()
    owner = host_session_id()
    skipped = ('compaction is off ([compact] mode = off)' if compact_mode() == 'off'
               else 'this terminal cannot be typed into (no supported terminal and no '
                    '[wake] command)' if not tab
               else 'the chat session id is unknown, so the turn end cannot be seen' if not owner
               else None)
    if skipped:
        emit({'ok': False, 'skipped': skipped})
        return
    focus = args.focus or (f'Keep only what the autopilot run needs: flow {args.flow}, the plan '
                           f'file, the decision to implement and where, open wishes of the '
                           f'user. The handoff file {handoff} holds the details.')
    compact_dir().mkdir(parents=True, exist_ok=True)
    path = compact_dir() / f"{args.flow}-{time.strftime('%H%M%S')}.json"
    req = {'flow': args.flow, 'owner': owner, 'tab': tab, 'handoff': str(handoff),
           'focus': one_line(focus), 'created': now(), 'status': 'waiting-idle'}
    write_json(path, req)
    with open(compact_dir() / 'helper.log', 'ab') as log:  # the helper keeps its own copy
        subprocess.Popen([sys.executable, str(SCRIPT), '_compact', str(path)], cwd=os.getcwd(),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         **detach_kwargs())
    emit({'ok': True, 'request': str(path), 'how': f"typed into the {tab['kind']} tab",
          'resume_line': resume_line(req)})


def cmd_compact_helper(args):
    """_compact REQUEST: wait for the turn end, type /compact, wait, type the resume line."""
    path = Path(args.request)

    def update(**fields):
        req = read_json(path, {}) or {}
        req.update(fields)
        write_json(path, req)
        return req

    def wait_for(key, limit):
        end = now() + limit
        while now() < end:
            req = read_json(path, {}) or {}
            if req.get(key) or req.get('status') == 'cancelled':
                return req
            time.sleep(COMPACT_POLL_S)
        return read_json(path, {}) or {}

    def type_line(req, text):
        code, out = run_quiet(wake_argv(req['tab'], text), timeout=20)
        return code == 0, out

    req = wait_for('idle_at', COMPACT_IDLE_S)
    if req.get('status') == 'cancelled':
        return
    if not req.get('idle_at'):
        # the turn never ended in time: typing /compact into a busy chat is not safe;
        # the resume line is (a busy chat queues it as a message)
        ok, out = type_line(req, resume_line(req))
        update(status='resumed-without-compact', resumed_at=now(),
               error=None if ok else out[-200:])
        return
    time.sleep(2)  # the host finishes the turn after its Stop hook
    update(status='compacting', typed_at=now())  # before typing: the hook can come fast
    ok, out = type_line(req, f"/compact {req['focus']}")
    if not ok:
        # /compact did not reach the chat: still wake it, so the run starts
        ok, out2 = type_line(req, resume_line(req))
        if not ok:
            notify('autopilot', 'Could not wake the chat. Open it and say "continue".')
        update(status='resumed-without-compact' if ok else 'failed', resumed_at=now(),
               error=(out if ok else out2)[-200:])
        return
    req = wait_for('compacted_at', COMPACT_DONE_S)
    if not req.get('compacted_at'):
        # a host without a SessionStart (compact) hook: give the compaction a fixed time
        time.sleep(max(0.0, COMPACT_NO_HOOK_S - (now() - req.get('typed_at', now()))))
    time.sleep(3)  # the host shows its prompt again after the compaction
    ok, out = type_line(req, resume_line(req))
    update(status='done' if ok else 'failed', resumed_at=now(),
           compacted=bool(req.get('compacted_at')), error=None if ok else out[-200:])


def compact_hook(event, data):
    """The Stop and SessionStart (compact) parts of the compaction; True when they handled it."""
    chat = data.get('session_id')
    if event == 'stop':
        pending = compact_requests(chat, 'waiting-idle')
        for path, req in pending:
            req['idle_at'] = now()
            write_json(path, req)
        return bool(pending)  # the turn must end now: never block it
    if event == 'session-start' and data.get('source') == 'compact':
        done = compact_requests(chat, 'compacting')
        for path, req in done:
            req['compacted_at'] = now()
            write_json(path, req)
        if done:
            req = done[-1][1]
            print(json.dumps({'hookSpecificOutput': {
                'hookEventName': 'SessionStart',
                'additionalContext': f"Autopilot compacted this chat before the run of flow "
                                     f"{req['flow']}. The handoff file {req['handoff']} holds the "
                                     'plan, the decision and the run arguments. A resume line '
                                     'follows: then read it and continue the autopilot skill at '
                                     'Step 5.'}}))
        return bool(done)
    return False


def cmd_hook(args):
    """Plugin hooks: tell a chat about runs nobody watches; keep the owner chat watching.

    Fails open: any problem means no output, so a hook never blocks normal work.
    """
    try:
        data = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        data = {}
    try:
        if compact_hook(args.event, data):
            return
    except Exception:
        pass
    chat = data.get('session_id')
    runs = runs_needing_attention(data.get('cwd') or os.getcwd())
    if not runs:
        return
    ap = f'python3 "{SCRIPT}"'
    if args.event == 'stop':
        if data.get('stop_hook_active'):
            return
        mine = [w for w in runs if w.get('owner') and w['owner'] == chat]
        if mine:
            time.sleep(2)  # a wait started in the background a moment ago may not have its lease yet
            mine = [w for w in mine if not watch_status(state_root() / 'sessions' / w['session'])
                    ['watched'] or not w['alive']]
        if not mine:
            return
        w = mine[0]
        todo = (f"start `{ap} next {w['session']} --catch-up` in the background (Bash timeout 7200000 ms) "
                'and post its updates') if w['alive'] else \
            (f"run `{ap} next {w['session']} --catch-up`, post its updates and the final report, "
             f"then `{ap} mark-reported {w['session']}`")
        print(json.dumps({'decision': 'block',
                          'reason': describe_run(w, chat) + f' Before ending the turn, {todo} '
                          '(autopilot-run skill, "Staying in Charge").'}))
        return
    # a live run between two watches of its chat is watched: say nothing about it
    runs = [w for w in runs if not (w['alive'] and w.get('between_watches'))]
    if not runs:
        return
    lines = [describe_run(w, chat) for w in runs]
    lines.append(f'To take charge here, follow "Staying in Charge" in the autopilot-run skill: '
                 f'`{ap} next <session> --catch-up` posts what was missed. Answer the user first; '
                 'in a chat that did not start the run, offer this instead of doing it.')
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'SessionStart' if args.event == 'session-start' else 'UserPromptSubmit',
        'additionalContext': ' '.join(lines)}}))


def cmd_mark_reported(args):
    write_json(session_dir(args.session) / 'reported.json', {'t': now()})
    emit({'ok': True})


SPEC_FLAGS = {'plan_model': '--plan-model', 'task_model': '--task-model',
              'review_model': '--review-model'}


def model_setup(repo, cli_args=(), executor='claude'):
    """How the runner picks models in repo: each phase spec with where it is set, and routing.

    {'specs': {key: {'value', 'where'}}, 'routed': {'claude': bool, 'codex': bool},
     'executor': ...}. A run flag (--task-model=...) beats the repo config, which beats
    the global one.
    """
    paths = cli_config_paths(repo)
    names = [str(paths[0]).replace(str(Path.home()), '~'), '.ralphex/config']
    specs = {}
    for path, name in zip(paths, names):
        for key, value in merged([path]).get('', {}).items():
            if key in SPEC_FLAGS or key in ('codex_model', 'codex_reasoning_effort'):
                specs[key] = {'value': value, 'where': f'{key} = {value} in {name}'}
    args = list(cli_args or [])
    for key, flag in SPEC_FLAGS.items():
        for i, arg in enumerate(args):
            value = arg.split('=', 1)[1] if arg.startswith(flag + '=') else \
                args[i + 1] if arg == flag and i + 1 < len(args) else None
            if value is not None:
                specs[key] = {'value': value, 'where': f'{flag}={value} on the run'}
    eff = merged(paths).get('', {})
    wrapper = parse_wrapper(repo, eff.get('codex_command'))
    return {'specs': specs, 'executor': executor or 'claude',
            'routed': {'claude': ROUTER in eff.get('claude_command', ''),
                       'codex': bool(wrapper and wrapper['routes'])}}


def step_pins(setup, phase, section):
    """{'model': where or None, 'effort': where or None}: the config that fixes each field.

    Follows the runner: tasks use task_model; reviews and finalize review_model, else
    task_model; planning plan_model, else task_model; the external codex review (claude
    mode) codex_model and codex_reasoning_effort. Under the codex executor an unset half of a
    spec inherits codex_model / codex_reasoning_effort.
    """
    specs = setup['specs']

    def spec(*keys):
        return next((specs[k] for k in keys if (specs.get(k) or {}).get('value')), None)

    def default(key):
        s = specs.get(key) or {}
        return s['where'] if s.get('value') else None

    external = setup['executor'] != 'codex' and (section or '').startswith('codex')
    if external:
        return {'model': default('codex_model'), 'effort': default('codex_reasoning_effort')}
    key = {'implementing tasks': ('task_model',),
           'planning': ('plan_model', 'task_model'),
           'plan review': ('plan_model', 'task_model')}.get(phase, ('review_model', 'task_model'))
    s = spec(*key)
    model, _, effort = (s['value'] if s else '').partition(':')
    pins = {'model': s['where'] if s and model else None,
            'effort': s['where'] if s and effort else None}
    if setup['executor'] == 'codex':
        pins['model'] = pins['model'] or default('codex_model')
        pins['effort'] = pins['effort'] or default('codex_reasoning_effort')
    return pins


def model_how(model, setup, phase, section, selected=False):
    """Who chose the step's model and effort: 'chosen by agrouter', 'set by <config>', ...

    Per field: a config spec that fixes it wins (agrouter only passes it on); else agrouter
    when the CLI runs through it; else the CLI's own default. A router fallback is the CLI
    default. Fields with the same source are said once.
    """
    if not setup:
        return None
    model = model or {}
    cli = model.get('cli') or ('codex' if (section or '').startswith('codex')
                               or setup['executor'] == 'codex' else 'claude')
    pins = step_pins(setup, phase, section)
    routed = setup['routed'].get(cli) or selected
    fallback = set((model.get('fallback') or {}).get('fields') or [])
    left = set((model.get('defaults') or {}))
    how = {}
    for field in ('model', 'effort'):
        if pins[field]:
            how[field] = f'set by {pins[field]}'
        elif field in fallback:
            how[field] = f'{cli} default (router fallback)'
        elif field in left:
            how[field] = f'{cli} default (agrouter left it to {cli})'
        elif routed and model.get(field):
            how[field] = 'chosen by agrouter'
        elif routed:
            how[field] = 'not reported'
        else:
            how[field] = f'{cli} default (not routed)'
    if how['model'] == how['effort']:
        return how['model']
    return f"model {how['model']}, effort {how['effort']}"


def model_label(model, how=None):
    """'claude-opus-5-5 (xhigh)'; marks CLI defaults and router fallbacks."""
    if not model or not (model.get('model') or model.get('cli')):
        return 'model and effort not reported by the runner' if how == 'not reported' \
            else 'runner default model'
    left = (model.get('fallback') or {}).get('defaults') or model.get('defaults') or {}
    name = model.get('model') or f"{model.get('cli')} default model"
    if model.get('effort'):
        label = f"{name} ({model['effort']})"
    else:
        label = f"{name} (default effort)" if 'effort' in left or model.get('fallback') else name
    return label + (' [router fallback]' if model.get('fallback') else '')


DEFAULT_WHY = {'default_not_reported': 'did not report it',
               'default_for_other_model': 'reported it for another model'}


def fallback_values(e):
    """'model claude-opus-5-5 (verified default), effort unknown (claude did not report it)'."""
    cli = e.get('cli') or 'the CLI'
    out = []
    for field in e.get('fields') or []:
        d = (e.get('defaults') or {}).get(field) or {}
        if d.get('value'):
            how = 'verified default' if d.get('source') == 'verified_child_default' else d.get('source')
            out.append(f"{field} {d['value']} ({how})")
        else:
            out.append(f"{field} unknown ({cli} {DEFAULT_WHY.get(d.get('why'), 'did not report it')})")
    return ', '.join(out) or f'{cli} defaults'


def steps_summary(steps, limit=200):
    """The plan's checklist items of one task as one short line, or None."""
    out, used = [], 0
    for text in steps:
        text = re.sub(r'\s+', ' ', text.replace('`', '')).strip().rstrip('.;')
        if not text:
            continue
        text = text if len(text) <= 90 else text[:87].rstrip() + '...'
        if out and used + len(text) + 2 > limit:
            break
        out.append(text)
        used += len(text) + 2
    if not out:
        return None
    rest = len([t for t in steps if t.strip()]) - len(out)
    return '; '.join(out) + (f' (+{rest} more)' if rest > 0 else '')


def event_line(e):
    """The fixed one-line chat update for an event; None for events the chat skips."""
    k = e['kind']
    task = f"Task {e.get('task') or '?'}/{e.get('tasks') or '?'}"
    # "### Task 2: Seed migration" -> "Seed migration": the number is already in `task`
    title = re.sub(r'^Task\s+\d+\s*[:.\-–]?\s*', '', e.get('title') or '') or e.get('title') or '?'
    steps = f"steps {e['steps_done']}/{e['steps_total']}" if e.get('steps_total') else None
    if k == 'task-start':
        how = f"continued (iteration {e['iteration']})" if e.get('continued') and e.get('iteration') \
            else 'continued' if e.get('continued') else 'started'
        line = ' · '.join(x for x in (f"▶ {task} {how}: {title}",
                                      model_label(e.get('model'), e.get('how')),
                                      e.get('how'), steps) if x)
        return line + (f"\n   Plan: {e['about']}" if e.get('about') else '')
    if k == 'task-done':
        took = f" in {e['minutes']} min" if e.get('minutes') is not None else ''
        commit = f"{e['commit']} {e['subject']}" if e.get('commit') else 'no commit yet'
        models = ', '.join(e.get('models') or []) or 'runner default model'
        line = ' · '.join(x for x in (f"✓ {task} done{took}: {title}",
                                      models, steps, commit) if x)
        done = ' · '.join(x for x in (e.get('did'), e.get('changes')) if x)
        return line + (f"\n   Done: {done}" if done else '')
    if k == 'phase-start':
        return ' · '.join(x for x in (
            f"▶ {e['phase'][:1].upper()}{e['phase'][1:]}: {e['section']}",
            model_label(e.get('model'), e.get('how')), e.get('how')) if x)
    if k == 'commit':
        return f"• Commit {e['commit']}: {e['subject']}"
    if k == 'router-fallback':
        where = f"{task}: {title}" if e.get('title') else e.get('section') or 'this step'
        code = ', '.join(x for x in (e.get('code'), e.get('stage') and f"{e['stage']} stage") if x)
        why = (e.get('message') or 'routing failed') + (f' ({code})' if code else '')
        return (f"⚠ Router fallback on {where}: {why}. Not chosen by agrouter; "
                f"runs on {e.get('cli') or 'CLI'} defaults: {fallback_values(e)}.")
    if k == 'router-warning':
        return f"⚠ Router: {e.get('text')}"
    if k == 'stalled':
        where = f"{task}: {title}" if e.get('title') else e.get('section') or 'the run'
        return f"⚠ No output for {e['minutes']} min on {where}. Checking the log."
    if k == 'restart':
        delay = f" in {e['delay_s']} s" if e.get('delay_s') else ''
        of = f" of {e['max_restarts']}" if e.get('max_restarts') else ''
        return (f"⚠ Attempt failed ({e.get('category')}): {e.get('error')}. "
                f"Restarting{delay} (restart {e.get('restart')}{of}); finished tasks are kept.")
    if k == 'failed':
        return f"✗ Run failed ({e.get('category')}): {e.get('error')}"
    if k == 'stopped':
        if e.get('after'):
            return f"■ Stopped after {e['after']}. Say \"resume\" to continue from the next one."
        return '■ Run stopped.'
    if k == 'stop-queued':
        where = f"{task} finishes ({title})" if e.get('title') else \
            f"the current step finishes ({e.get('section') or 'now'})"
        return f"⏸ Will stop after {where}."
    if k == 'stop-cancelled':
        return '▶ Stop cancelled; the run continues.'
    if k == 'done':
        return '✓ Run finished. Writing the final report.'
    if k == 'limit':
        wait = f", next try in {max(1, round(e['wait_s'] / 60))} min" if e.get('wait_s') else ''
        resets = f", resets {e['resets']}" if e.get('resets') else ''
        return (f"⏸ Usage limit ({e.get('tool') or 'model'}): {e.get('pattern')}{resets}{wait}. "
                'Finished tasks are kept.')
    if k == 'limit-cleared':
        return f"▶ Limit cleared after {e.get('minutes')} min; work continues."
    if k == 'auto-answered':
        return f"⏱ No reply: answered \"{e.get('summary')}\". {e.get('reason') or ''}".strip()
    if k == 'review-skipped':
        return f"• External review skipped ({e.get('how')})."
    if k == 'plan-review':
        text = e.get('text') or ''
        return f"{'⚠' if e.get('warning') else '•'} Plan review: {text[:1].upper()}{text[1:]}."
    if k == 'error':
        return f"⚠ {e.get('line')}"
    if k == 'heartbeat':
        where = f"{task}: {title}" if e.get('title') else \
            f"{(e.get('phase') or 'step')[:1].upper()}{(e.get('phase') or 'step')[1:]}: {e.get('section')}"
        active = '' if e.get('active_min') is None else \
            (' · agent active now' if e['active_min'] < 1 else f" · agent active {e['active_min']} min ago")
        return (f"… {where} still running ({e.get('minutes')} min) · last output "
                f"{e.get('output_min')} min ago{active}")
    if k == 'wake':
        return None  # for the chat that was asleep: the catch-up shows what happened
    return None


def answer_pending(sdir, st):
    """True while an answer to the current prompt sits unprocessed in the inbox."""
    waiting = st.get('waiting') or {}
    sent = read_json(sdir / 'answered.json', {}) or {}
    return st.get('status') == 'waiting' and bool(waiting) and \
        sent.get('prompt_id') == waiting.get('id')


def limit_view(limit):
    """Rate-limit episode with relative times, or None."""
    if not limit:
        return None
    view = {k: limit.get(k) for k in ('tool', 'pattern', 'resets', 'retries', 'handled_by',
                                      'wait_s', 'message')}
    view['waiting_min'] = round((now() - limit.get('since', now())) / 60)
    if limit.get('next_retry_at'):
        view['next_retry_in_s'] = max(0, int(limit['next_retry_at'] - now()))
    if limit.get('resets_at'):
        view['resets_in_s'] = max(0, int(limit['resets_at'] - now()))
    return view


WAIT_QUIET = {'section', 'started', 'answered', 'auto-answer', 'waiting', 'limit-retry',
              'transient', 'extended', 'wake'}


def watch(sdir, since, prompt_after, timeout, any_event=False):
    """Block until something needs the agent; return the report, or None when superseded.

    A prompt wakes the agent only when its id is above prompt_after (None: any open prompt),
    so one prompt wakes the chat once; after that the watch goes on until a reminder, an
    auto-answer or another event arrives.
    """
    end = now() + timeout
    # one watcher per run: a newer wait replaces an older one, which then exits quietly
    me = {'pid': os.getpid(), 'owner': host_session_id(), 'since': since, 'started': now()}
    write_json(sdir / 'watcher.json', me)
    meta = read_json(sdir / 'session.json', {}) or {}
    tab = own_tab()
    if meta and ((me['owner'] and meta.get('owner') != me['owner']) or (tab and meta.get('tab') != tab)):
        # the chat that watches now is the one in charge, in the tab it runs in
        meta['owner'] = me['owner'] or meta.get('owner')
        meta['tab'] = tab or meta.get('tab')
        write_json(sdir / 'session.json', meta)
    while True:
        st = read_json(sdir / 'state.json', {}) or {}
        fresh = [e for e in st.get('events', []) if e['seq'] > since]
        prompt = st.get('waiting') or {}
        new_prompt = st.get('status') == 'waiting' and not answer_pending(sdir, st) and (
            prompt_after is None or prompt.get('id', 0) > prompt_after)
        ended = st.get('status') in ('done', 'failed', 'stopped')
        if new_prompt or ended or not pid_alive(st.get('pid')) and st.get('status') != 'starting':
            break
        if any(e['kind'] not in WAIT_QUIET for e in fresh):
            break
        if any_event and fresh:
            break
        if now() >= end:
            break
        if (read_json(sdir / 'watcher.json', {}) or {}).get('pid') != os.getpid():
            return None
        time.sleep(1)
    if (read_json(sdir / 'watcher.json', {}) or {}).get('pid') == os.getpid():
        # keep when the watch ended: the chat starts the next one in a moment, and the hooks
        # must not call that normal gap "no chat is watching"
        write_json(sdir / 'watcher.json', {'pid': None, 'owner': me['owner'], 'ended': now()})
    out = report(sdir, st, since)
    if since <= out['seq']:  # it reported everything between since and seq
        write_json(sdir / 'seen.json', {'seq': out['seq'], 't': now()})
    out['heartbeat'] = not [e for e in out['new_events'] if e['kind'] not in WAIT_QUIET] \
        and not new_prompt and out['status'] not in ('done', 'failed', 'stopped', 'lost')
    return out


def cmd_wait(args):
    """Block until something needs the agent: a new prompt, a notable event, the end, or timeout."""
    sdir = session_dir(args.session)
    since = args.since
    if args.catch_up:
        since = (read_json(sdir / 'seen.json', {}) or {}).get('seq', 0)
    if since is None:
        since = (read_json(sdir / 'state.json', {}) or {}).get('seq', 0)
    out = watch(sdir, since, args.since, args.timeout, args.any)
    if out is None:
        emit({'session': sdir.name, 'superseded': True,
              'note': 'a newer wait watches this run; nothing to report'})
        return
    emit(out)


def clock(seconds_from_now):
    """'17:15 (in 30 min)' for a deadline seconds_from_now away."""
    at = time.strftime('%H:%M', time.localtime(now() + seconds_from_now))
    return f'{at} (in {max(1, round(seconds_from_now / 60))} min)'


def next_text(out, sdir):
    """The `next` report as short plain text: one line per fact, the open prompt last."""
    sid = out['session']
    ap = 'AP'
    head = [f"session {sid} · {out.get('kind')} · {out.get('status')}"]
    if out.get('section'):
        head.append(out['section'])
    tasks = out.get('tasks') or {}
    if tasks.get('total'):
        head.append(f"tasks {tasks.get('done')}/{tasks.get('total')}")
    model = out.get('model') or {}
    if model.get('model'):
        head.append(model_label(model))
    lines = [' · '.join(head)]
    lines += out.get('updates') or []
    limit = out.get('limit')
    if limit:
        lines.append(f"LIMIT {limit.get('tool') or ''} {limit.get('message') or ''}".strip()
                     + (f" · resets {clock(limit['resets_in_s'])}" if limit.get('resets_in_s') else ''))
    status = out.get('status')
    w = out.get('waiting')
    if out.get('heartbeat'):
        open_id = f" · prompt {w.get('id')} still open" if status == 'waiting' and w else ''
        lines += [f'NO NEWS{open_id} · run {ap} next {sid} again', f"seq {out.get('seq')}"]
        return '\n'.join(lines)
    if status == 'waiting' and w:
        rec = (read_json(sdir / 'recommend.json', {}) or {})
        rec = rec if rec.get('prompt_id') == w.get('id') else {}
        left = out.get('auto_answer_in_s')
        when = (f"auto-answer at {clock(left)}" if left is not None else 'no auto-answer: waits for the user')
        review = w.get('review') or {}
        step = {'pick': ' · plan review: which reviewer',
                'apply': ' · plan review: which findings'}.get(review.get('step'), '')
        lines.append(f"PROMPT {w['kind']} (id {w.get('id')}){step} · {when}")
        lines.append('recommended: ' + (f"{rec['summary']} ({rec.get('reason') or ''})" if rec
                                        else f'none registered; run {ap} recommend first'))
        if review.get('default') and not rec:
            lines.append(f"default on no reply: {review['default']['answer']} "
                         f"({review['default']['reason']})")
        if review:
            lines.append(f"plan: {review.get('plan_file') or 'unknown'} · round {review.get('round')}"
                         + (f" · reviewer: {review['reviewer']}" if review.get('reviewer') else ''))
        if w.get('question') and w['kind'] != 'draft':
            lines.append('question: ' + one_line(w['question']))
        if review.get('step') == 'apply':
            lines.append('findings:')
            lines += [f"  #{f['n']} {f['severity']}: {one_line(f['text'])}"
                      for f in review.get('findings') or []] or ['  (none read from the output)']
        if w.get('prompt'):
            lines.append('prompt: ' + one_line(w['prompt']))
        for i, opt in enumerate(w.get('options') or [], 1):
            lines.append(f'  {i}. {one_line(opt)}')
        if w['kind'] == 'draft':
            lines.append(f"draft: {w.get('draft_file')}")
            lines.append(f"edit copy: {w.get('edited_file')}")
            lines.append(f'answer: {ap} answer {sid} --action accept | --action revise --text "..." '
                         '| --action edited | --action reject')
        elif review.get('step') == 'apply':
            lines.append(f'answer: {ap} answer {sid} --text "1,3" | --text "2-4" | '
                         f'--text "{REVIEW_ALL}" | --text "{REVIEW_NONE}"')
        elif w['kind'] in ('question', 'choice'):
            lines.append(f'answer: {ap} answer {sid} --choice N'
                         + (' | --text "..."' if w.get('free_text') or w['kind'] == 'question' else ''))
        elif w['kind'] == 'yesno':
            lines.append(f'answer: {ap} answer {sid} --yes | --no')
        else:
            lines.append(f'answer: {ap} answer {sid} --text "..."')
    elif status == 'done':
        lines.append('DONE' + (f" · plan file: {out['plan_file']}" if out.get('plan_file') else ''))
    elif status in ('failed', 'lost', 'stopped'):
        lines.append(f'{status.upper()}' + (f": {out['last_error']}" if out.get('last_error') else ''))
        lines += [f'  error: {e}' for e in out.get('recent_errors') or []]
        lines.append(f"log: {out.get('log')}")
    lines.append(f"seq {out.get('seq')}")
    return '\n'.join(lines)


def cmd_next(args):
    """The one call the chat makes: block until the runner needs it, then print short text.

    It continues after the last event any watch reported (seen.json), so the chat never
    tracks --since. It returns on a prompt the chat has not seen, a notable event, the end,
    or after --timeout (default 115 min, under the 2 h limit of background commands).
    """
    sdir = session_dir(args.session)
    since = (read_json(sdir / 'seen.json', {}) or {}).get('seq', 0)
    # reattaching (a new chat, after a limit): an open prompt that an earlier chat saw counts too
    out = watch(sdir, since, None if args.catch_up else since, args.timeout)
    if out is None:
        print(f'session {sdir.name}: a newer watch took over; nothing to report')
        return
    if args.json:
        emit(out)
        return
    print(next_text(out, sdir))


def one_line(text):
    return ' '.join((text or '').split())


def open_prompt(sdir):
    st = read_json(sdir / 'state.json', {}) or {}
    w = st.get('waiting')
    if not w or st.get('status') != 'waiting':
        die('the session is not waiting for input')
    if answer_pending(sdir, st):
        die('this prompt is already answered; run wait for the next one')
    return w


def build_answer(w, args):
    """Turn answer flags into (stdin text, one-line summary) for prompt w."""
    kind = w['kind']
    if getattr(args, 'text_file', None):
        args.text = Path(args.text_file).read_text(encoding='utf-8')
    text = one_line(args.text) if args.text is not None else None
    if kind == 'question':
        if args.choice:
            if not 1 <= args.choice <= len(w['options']):
                die(f"choice must be 1-{len(w['options'])}")
            text = w['options'][args.choice - 1]
        if not text:
            die('give --choice N or --text')
        return text + '\n', text
    if kind == 'choice':
        opts = w['options']
        if args.choice:
            if not 1 <= args.choice <= len(opts):
                die(f'choice must be 1-{len(opts)}')
            return f'{args.choice}\n', opts[args.choice - 1]
        if text and text in opts:
            return f'{opts.index(text) + 1}\n', text
        if text and OTHER in opts:
            return f'{opts.index(OTHER) + 1}\n{text}\n', text
        die('give --choice N, or --text matching an option')
    if kind == 'draft':
        action = args.action
        if action == 'accept':
            send = '1\n'
        elif action == 'revise':
            if not text:
                die('revise needs --text with the feedback')
            send = f'2\n{text}\n'
        elif action == 'edited':
            edited, draft = Path(w['edited_file']), Path(w['draft_file'] or '')
            if not edited.exists() or (draft.exists() and
                                       edited.read_text() == draft.read_text()):
                die(f'edit {edited} first: it still matches the draft')
            send = '3\n'
        elif action == 'reject':
            send = '4\n'
        else:
            die('draft needs --action accept|revise|edited|reject')
        return send, action + (f': {text}' if text else '')
    if kind == 'yesno':
        if args.yes == args.no:
            die('give --yes or --no')
        return ('y\n', 'yes') if args.yes else ('n\n', 'no')
    if not text:
        die('give --text')
    return text + '\n', text


def cmd_answer(args):
    sdir = session_dir(args.session)
    w = open_prompt(sdir)
    send, summary = build_answer(w, args)
    inbox = sdir / 'inbox'
    tmp = inbox / f'.{time.time_ns()}.tmp'
    tmp.write_text(json.dumps({'send': send, 'summary': summary[:200], 'by': args.by,
                               'reason': args.reason, 'prompt_id': w.get('id')}),
                   encoding='utf-8')
    os.replace(tmp, inbox / f'{time.time_ns()}.json')
    write_json(sdir / 'answered.json', {'prompt_id': w.get('id')})
    emit({'ok': True, 'answered': summary[:200], 'by': args.by})


def cmd_recommend(args):
    """Register the answer to take when nobody replies before the deadline."""
    sdir = session_dir(args.session)
    w = open_prompt(sdir)
    send, summary = build_answer(w, args)
    write_json(sdir / 'recommend.json', {'prompt_id': w.get('id'), 'send': send,
                                         'summary': summary[:200], 'reason': args.reason})
    left = int(w['deadline'] - now()) if w.get('deadline') else None
    emit({'ok': True, 'recommended': summary[:200],
          'auto_answer_in_s': max(0, left) if left is not None else None})


def cmd_skip_review(args):
    """Skip the external code review of a run: now if it runs, otherwise when it starts."""
    sdir = session_dir(args.session)
    st = read_json(sdir / 'state.json', {}) or {}
    if not pid_alive(st.get('pid')):
        die('the session is not running')
    inbox = sdir / 'inbox'
    (inbox / f'{time.time_ns()}.json').write_text(
        json.dumps({'skip_external': True, 'by': args.by, 'reason': args.reason}), encoding='utf-8')
    emit({'ok': True, 'when': 'now' if st.get('phase') == 'external review'
          else 'when the external review starts'})


def cmd_extend(args):
    """Give the user more time on the open prompt (or switch its auto-answer off)."""
    sdir = session_dir(args.session)
    w = open_prompt(sdir)
    seconds = None if args.never else parse_duration(args.by)
    inbox = sdir / 'inbox'
    (inbox / f'{time.time_ns()}.json').write_text(
        json.dumps({'extend': seconds, 'prompt_id': w.get('id')}), encoding='utf-8')
    emit({'ok': True, 'auto_answer_in_s': seconds})


def timer_path(tid):
    if not re.fullmatch(r'[\w.-]+', tid):
        die('timer id: letters, digits, dot, dash, underscore')
    path = state_root() / 'timers'
    path.mkdir(parents=True, exist_ok=True)
    return path / f'{tid}.json'


def cmd_ask_timer(args):
    """Timer for agent-level questions: start, wait (in chunks), cancel, extend."""
    path = timer_path(args.id)
    t = read_json(path, {}) or {}
    if args.action == 'start':
        rule = load_policy().get(args.decision)
        if not rule:
            die(f'unknown decision: {args.decision}; see the policy command')
        timeout = parse_duration(args.seconds) if args.seconds else rule['timeout']
        t = {'decision': args.decision, 'started': now(), 'state': 'pending',
             'deadline': now() + timeout if timeout is not None else None,
             'remind_before': rule['remind_before'], 'reminded': False}
    elif not t:
        die(f'no timer {args.id}')
    elif args.action == 'cancel':
        t['state'] = 'cancelled'
    elif args.action == 'extend':
        seconds = None if args.never else parse_duration(args.by)
        t.update({'deadline': now() + seconds if seconds is not None else None,
                  'reminded': False, 'state': 'pending'})
    elif args.action == 'wait':
        end = now() + args.max
        while t.get('state') == 'pending':
            deadline = t.get('deadline')
            if deadline is None:
                t['state_now'] = 'never'
                break
            remind_at = deadline - t['remind_before'] if t.get('remind_before') else None
            if remind_at and not t.get('reminded') and now() >= remind_at:
                t['reminded'] = True
                write_json(path, t)
                emit({'state': 'reminder', 'left_s': int(deadline - now()),
                      'decision': t['decision']})
                return
            if now() >= deadline:
                t['state'] = 'expired'
                break
            if now() >= end:
                write_json(path, t)
                emit({'state': 'pending', 'left_s': int(deadline - now()),
                      'decision': t['decision']})
                return
            time.sleep(1)
            t = read_json(path, t) or t
    write_json(path, t)
    left = int(t['deadline'] - now()) if t.get('deadline') else None
    emit({'state': t.pop('state_now', None) or t['state'], 'decision': t['decision'],
          'left_s': max(0, left) if left is not None else None,
          'waited_min': round((now() - t['started']) / 60, 1)})


def cmd_log(args):
    """Record an agent decision (recovery, fix, choice) in the session's decision log."""
    sdir = session_dir(args.session)
    extra = {}
    if args.question:
        extra['question'] = one_line(args.question)
    if args.waited is not None:
        extra['waited_min'] = args.waited
    if args.by == 'auto':
        extra['recommended'] = True
    log_decision(sdir, kind=args.kind, by=args.by, text=one_line(args.text),
                 reason=one_line(args.reason) if args.reason else None, **extra)
    emit({'ok': True})


def cmd_timer(args):
    """Sleep up to S seconds (for timed agent-level questions); prints elapsed time."""
    start = now()
    time.sleep(max(0, args.seconds))
    emit({'ok': True, 'elapsed_s': round(now() - start)})


def default_branch(repo):
    code, out = run_quiet(['git', 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], cwd=repo)
    if code == 0 and out:
        return out
    for name in ('main', 'master'):
        if run_quiet(['git', 'rev-parse', '--verify', '-q', name], cwd=repo)[0] == 0:
            return name
    return None


GIT_OPERATIONS = {'rebase-merge': 'rebase', 'rebase-apply': 'rebase', 'MERGE_HEAD': 'merge',
                  'CHERRY_PICK_HEAD': 'cherry-pick', 'REVERT_HEAD': 'revert', 'BISECT_LOG': 'bisect'}


def porcelain_paths(repo):
    """Changed and untracked paths as the runner counts them (git status -uall), unquoted."""
    try:
        out = subprocess.run(['git', 'status', '--porcelain', '-z', '--untracked-files=all'],
                             cwd=repo, capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=15, stdin=subprocess.DEVNULL,
                             **NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    entries, paths = out.split('\0'), []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in 'RC':
            i += 1  # -z puts a rename's source path in the next entry
    return paths


def file_state(path):
    """A short content hash of a file (a symlink: of its target); None when it does not exist."""
    try:
        if path.is_symlink():
            data = b'link:' + os.readlink(path).encode()
        elif path.is_file():
            data = path.read_bytes()
        else:
            return None
    except OSError:
        return None
    return hashlib.sha1(data).hexdigest()[:16]


def dirty_snapshot(repo):
    """{path: content hash, None when deleted} for every changed, added or untracked file.

    Renames count as a deletion plus a new file, so each side can be moved on its own.
    """
    try:
        out = subprocess.run(['git', 'status', '--porcelain', '-z', '--untracked-files=all',
                              '--no-renames'], cwd=repo, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=15,
                             stdin=subprocess.DEVNULL,
                             **NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    snap = {}
    for entry in out.split('\0'):
        if len(entry) >= 4:
            snap[entry[3:]] = file_state(Path(repo) / entry[3:])
    return snap


def flow_baseline(flow):
    """The dirty files when the flow started (its first session), or None when unknown."""
    root = state_root() / 'sessions'
    if not flow or not root.exists():
        return None
    for d in sorted(root.iterdir()):
        meta = read_json(d / 'session.json', {}) or {}
        if meta.get('flow') == flow and isinstance(meta.get('baseline'), dict):
            return meta['baseline']
    return None


def session_changes(repo, baseline):
    """What a move to a new worktree takes: {'move': [...], 'keep': [...]}.

    move: files changed or created since the flow started; they go to the worktree and this
    checkout gets them back as in HEAD. keep: files that had changes before the flow and changed
    again; they are copied, but stay here too, because the earlier changes are not the flow's.
    Files that only had changes before the flow are not touched.
    """
    now_ = dirty_snapshot(repo)
    move = sorted(p for p in now_ if p not in baseline)
    keep = sorted(p for p in now_ if p in baseline and baseline[p] != now_[p])
    return {'move': move, 'keep': keep, 'state': now_}


def implementation_place(repo, cfg=None, plan=None, baseline=None, ticket=None, summary=None):
    """Where the implementation run should work, and the recommended choice for this checkout.

    The runner can make a worktree only from the default branch, and a run on a feature branch
    works on that branch in place. So: on the default branch in the main checkout, recommend a
    new worktree (the main checkout stays free for more planning); inside a linked worktree or on
    a feature branch, recommend staying here. use_worktree = true has no per-run off switch, so it
    blocks every run that must stay here.
    """
    cfg = cfg if cfg is not None else merged(cli_config_paths(repo)).get('', {})

    def git(*argv):
        code, out = run_quiet(['git', *argv], cwd=repo)
        return out.strip() if code == 0 else None

    git_dir = Path(git('rev-parse', '--absolute-git-dir') or '.')
    common = git('rev-parse', '--path-format=absolute', '--git-common-dir')
    linked = bool(common) and git_dir.resolve() != Path(common).resolve()
    branch = git('branch', '--show-current') or ''
    default = (cfg.get('default_branch') or default_branch(repo) or '').removeprefix('origin/')
    on_default = branch == default if default else branch in ('main', 'master')
    operation = next((name for marker, name in GIT_OPERATIONS.items()
                      if (git_dir / marker).exists()), None)
    plan_rel = None
    if plan:
        try:
            plan_rel = Path(plan).resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            plan_rel = None
    dirty = [path for path in porcelain_paths(repo) if path != plan_rel]
    use_worktree = str(cfg.get('use_worktree', '')).lower() == 'true'
    checkout = 'worktree' if linked else 'main checkout'
    here_text = (f'this worktree, branch {branch}' if linked and not on_default
                 else f'this checkout, branch {branch}' if not on_default
                 else f'a new branch in this {"worktree" if linked else "checkout"}')
    names = run_names(repo, plan, ticket, summary) if plan else None
    new_path = names['path'] if names else str(sibling_path(repo, '<plan branch>'))
    name_args = ['--branch', names['branch']] if names and names['branch'] != names['slug'] else []
    worktree = {'option': 'worktree', 'usable': True, 'reason': None, 'path': new_path,
                'run_args': ['--new-worktree'] + name_args
                + (['--worktree-path', new_path] if names and names['custom'] else []),
                'text': f'a new worktree in {new_path} from {default or branch}'}
    if baseline is not None:
        changes = session_changes(repo, baseline)
        worktree.update(moves=changes['move'], keeps=changes['keep'])
    here = {'option': 'here', 'run_args': name_args if on_default else [], 'usable': True,
            'reason': None, 'text': here_text}
    if not branch:
        worktree.update(usable=False, reason='HEAD is detached')
        here.update(usable=False, reason='HEAD is detached: check out a branch first')
    elif not on_default:
        worktree.update(usable=False,
                        reason=f'the runner makes worktrees only from {default or "main/master"}; '
                               f'this {checkout} is on {branch}')
    if operation:
        worktree.update(usable=False, reason=f'a {operation} is in progress: finish or abort it')
        here.update(usable=False, reason=f'a {operation} is in progress: finish or abort it')
    if on_default and dirty and here['usable']:
        here.update(usable=False, reason='the runner refuses to branch with uncommitted files: '
                                         + ', '.join(dirty[:5]))
    if use_worktree:
        # the runner would make its own worktree inside .ralphex/worktrees/, and fails on a branch
        reason = ('use_worktree = true makes the runner make its own worktree inside '
                  '.ralphex/worktrees/ (or fail on a feature branch): set use_worktree = false')
        worktree.update(usable=False, reason=reason)
        if not on_default and here['usable']:
            here.update(usable=False, reason=reason)
    if worktree['usable'] and (not linked or not here['usable']):
        recommended = 'worktree'
        moved = worktree.get('moves') or []
        others = [d for d in dirty if d not in moved]
        why = ('planned on the default branch: the run gets its own worktree and branch in '
               'workspaces/, and this checkout stays on ' + (default or branch)
               + (' with your other uncommitted files' if others and moved
                  else ' with your uncommitted files' if others else '')
               + (f'; the {len(moved)} file(s) changed during this flow move there, and this '
                  'checkout gets them back as committed' if moved else ''))
    elif here['usable']:
        recommended = 'here'
        why = (f'already in a worktree on {branch}: work stays in it' if linked and not on_default
               else f'already on the feature branch {branch}: work stays on it' if not on_default
               else 'this is already a separate worktree: a new branch here keeps it isolated')
    else:
        recommended = None
        why = here['reason'] or worktree['reason']
    return {'checkout': checkout, 'branch': branch or None, 'default_branch': default or None,
            'on_default': on_default, 'dirty': dirty, 'operation': operation,
            'use_worktree': use_worktree, 'recommended': recommended, 'why': why,
            'names': names,
            'options': [o for o in (worktree, here) if o['option'] == recommended]
            + [o for o in (worktree, here) if o['option'] != recommended]}


REPO_CONFIGS = ('.ralphex/config', '.agrouter/config', '.autopilot/config')


WORKTREE_PATH = '{parent}/workspaces/{repo}/{Name}'


def sibling_path(repo, branch, names=None):
    """Folder for a new worktree, in the workspaces layout: <parent>/workspaces/<repo>/<Name>.

    <parent> is the folder that holds the main checkout (also when repo is a linked worktree),
    <Name> is the branch with a capital first letter ("add-cache" -> "Add-cache"). The
    [worktree] path key in the autopilot config changes the pattern; it knows {parent},
    {repo}, {branch} and {Name}.
    """
    code, common = run_quiet(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                             cwd=repo)
    main = Path(common).parent if code == 0 and common and Path(common).name == '.git' \
        else Path(repo)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '-', branch).strip('-') or 'run'
    pattern = autopilot_config('worktree').get('path') or WORKTREE_PATH
    extra = {k: re.sub(r'[^A-Za-z0-9._-]+', '-', str(v)).strip('-')
             for k, v in (names or {}).items() if k in ('slug', 'ticket', 'summary')}
    extra.setdefault('slug', safe)
    extra.setdefault('ticket', '')
    extra.setdefault('summary', '')
    try:
        path = pattern.format(parent=main.parent, repo=main.name, branch=safe,
                              Name=safe[:1].upper() + safe[1:], **extra)
    except (KeyError, IndexError, ValueError):
        path = WORKTREE_PATH.format(parent=main.parent, repo=main.name, branch=safe,
                                    Name=safe[:1].upper() + safe[1:])
    path = re.sub(r'-{2,}', '-', path).replace('/-', '/').replace('-/', '/')
    path = Path(path).expanduser()
    return path if path.is_absolute() else main / path


TICKET_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,9}-\d+)\b')


def plan_heading(plan):
    """The plan's first heading without a "Plan:" prefix, or None."""
    try:
        for line in Path(plan).read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('# '):
                return re.sub(r'(?i)^(implementation\s+)?plan:\s*', '', line[2:]).strip() or None
    except OSError:
        pass
    return None


def run_names(repo, plan, ticket=None, summary=None):
    """The branch and worktree folder for a run, after the repository's naming convention.

    [worktree] branch (default {slug}) and path (default the workspaces layout) know {slug}
    (the plan file name without its date), {ticket} and {summary}. A ticket is taken from the
    argument, else found in the current branch, the plan file name or the plan title
    (ABC-123). The summary is the argument, else the plan title. A pattern that needs a ticket
    nobody gave falls back to {slug} and lists it in `needs`.
    """
    cfg = autopilot_config('worktree')
    slug = plan_branch({'argv': ['ralphex'], 'target': str(plan)})
    title = plan_heading(plan) if plan else None
    if not ticket:
        _, current = run_quiet(['git', 'branch', '--show-current'], cwd=repo)
        for text in (current, Path(str(plan)).name, title):
            m = TICKET_RE.search(text or '')
            if m:
                ticket = m.group(1)
                break
    summary = summary or title or slug
    pattern = cfg.get('branch') or '{slug}'
    path_pattern = cfg.get('path') or WORKTREE_PATH
    needs = [k for k, v in (('ticket', ticket),)
             if '{' + k + '}' in pattern + path_pattern and not v]
    values = {'slug': slug, 'ticket': ticket or '',
              'summary': re.sub(r'[^A-Za-z0-9._-]+', '-', summary).strip('-')}
    branch = slug
    if not needs:
        try:
            branch = pattern.format(**values)
        except (KeyError, IndexError, ValueError):
            branch = slug
        branch = re.sub(r'[^A-Za-z0-9._/-]+', '-', branch)
        branch = re.sub(r'-{2,}', '-', re.sub(r'/{2,}', '/', branch)).strip('-/') or slug
    path = sibling_path(repo, branch, values if not needs else None)
    return {'branch': branch, 'path': str(path), 'slug': slug, 'ticket': ticket,
            'summary': summary, 'pattern': pattern, 'needs': needs,
            'custom': branch != slug or bool(cfg.get('path'))}


def worktree_of(repo, branch):
    """Path of an existing worktree that has branch checked out, or None."""
    _, listing = run_quiet(['git', 'worktree', 'list', '--porcelain'], cwd=repo)
    path = None
    for line in (listing or '').splitlines():
        if line.startswith('worktree '):
            path = line[9:]
        elif line == f'branch refs/heads/{branch}' and path:
            return path
    return None


def restore_paths(repo, paths):
    """Give paths back their HEAD state in repo: tracked ones checked out, new ones removed."""
    repo = Path(repo)
    tracked = [p for p in paths if run_quiet(['git', 'cat-file', '-e', f'HEAD:{p}'], cwd=repo)[0] == 0]
    new = [p for p in paths if p not in tracked]
    problems = []
    for i in range(0, len(tracked), 100):
        code, out = run_quiet(['git', 'checkout', 'HEAD', '--', *tracked[i:i + 100]], cwd=repo,
                              timeout=60)
        if code != 0:
            problems.append(out[-200:])
    if new:
        run_quiet(['git', 'rm', '-q', '--cached', '--ignore-unmatch', '--', *new], cwd=repo)
    for p in new:
        f = repo / p
        try:
            if f.is_symlink() or f.is_file():
                f.unlink()
            parent = f.parent
            while parent != repo and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError as exc:
            problems.append(f'{p}: {exc}')
    return problems


def prepare_worktree(repo, plan, cli_args, path=None, baseline=None):
    """Make (or reuse) the run's worktree in workspaces/, with the plan and repo configs in it.

    The runner's own --worktree puts it in .ralphex/worktrees/ inside the checkout; autopilot
    makes it in the workspaces layout instead (sibling_path) and runs the runner there on the
    new branch, which the runner then uses in place. An uncommitted plan and uncommitted
    repository configs are copied and committed on the new branch, so the run sees them.

    With the flow's baseline (the dirty files when the flow started), every file changed or
    created during the flow moves: it is copied (a deletion repeated) and committed in the new
    worktree, and this checkout gets it back as in HEAD. A file that had changes before the
    flow is copied but kept here. Without a baseline the source files stay.
    """
    repo = Path(repo)
    branch = plan_branch({'argv': ['ralphex', *cli_args], 'target': str(plan)})
    _, base = run_quiet(['git', 'branch', '--show-current'], cwd=repo)
    rel = Path(plan).resolve().relative_to(repo.resolve())
    existing = worktree_of(repo, branch)
    if existing and Path(existing).resolve() != repo.resolve():
        return {'path': existing, 'branch': branch, 'base': base, 'plan': str(Path(existing) / rel),
                'reused': True, 'copied': [], 'removed': [], 'moved': [], 'kept_here': [],
                'restore_problems': []}
    if existing:
        die(f'{branch} is checked out in this folder: run here instead of in a new worktree')
    target = Path(path) if path else sibling_path(repo, branch)
    n = 2
    while target.exists() and not path:
        target = Path(f'{sibling_path(repo, branch)}-{n}')
        n += 1
    if target.exists():
        die(f'{target} exists and is not a worktree of {branch}')
    has_branch = run_quiet(['git', 'rev-parse', '--verify', '-q', f'refs/heads/{branch}'],
                           cwd=repo)[0] == 0
    argv = ['git', 'worktree', 'add', str(target), branch] if has_branch else \
        ['git', 'worktree', 'add', '-b', branch, str(target), base]
    code, out = run_quiet(argv, cwd=repo, timeout=120)
    if code != 0:
        die(f'git worktree add failed: {out[-300:]}')
    plan_rel = rel.as_posix()
    move, keep = [], []
    if baseline is not None and not has_branch:
        changes = session_changes(repo, baseline)
        state, move, keep = changes['state'], changes['move'], changes['keep']
    else:
        state = dirty_snapshot(repo)
    dirty = set(state)
    wanted = [plan_rel] + ([] if has_branch else [
        c for c in dirty if c in REPO_CONFIGS or c.startswith('.ralphex/scripts/')])
    wanted = list(dict.fromkeys(wanted + move + keep))
    copied, removed = [], []
    for name in wanted:
        src, dst = repo / name, target / name
        if name in dirty and state.get(name) is None and not src.exists():
            # deleted during the flow: delete it on the new branch too
            if run_quiet(['git', 'rm', '-q', '--ignore-unmatch', '--', name], cwd=target)[0] == 0:
                removed.append(name)
        elif (src.is_file() or src.is_symlink()) and (name in dirty or not dst.exists()):
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            shutil.copy2(src, dst, follow_symlinks=False)
            copied.append(name)
    if copied or removed:
        if copied:
            code, out = run_quiet(['git', 'add', '--', *copied], cwd=target)
            if code != 0:
                die(f'could not stage {", ".join(copied[:5])} in {target}: {out[-300:]}')
        extra = [p for p in copied + removed if p != plan_rel and p not in REPO_CONFIGS
                 and not p.startswith('.ralphex/scripts/')]
        msg = f'add plan and flow changes: {branch}' if extra else f'add plan: {branch}'
        code, out = run_quiet(['git', 'commit', '-m', msg], cwd=target, timeout=60)
        if code != 0:
            die(f'could not commit the plan in {target}: {out[-300:]}')
    # only after the commit holds them: give this checkout its files back as in HEAD
    moved = [p for p in move if p in copied or p in removed]
    problems = restore_paths(repo, moved) if moved else []
    return {'path': str(target), 'branch': branch, 'base': base, 'plan': str(target / rel),
            'reused': False, 'copied': copied, 'removed': removed, 'moved': moved,
            'kept_here': [p for p in keep if p in copied], 'restore_problems': problems}


def cmd_place(args):
    repo = git_root()
    if not repo:
        die('not inside a git repository')
    emit(implementation_place(repo, plan=args.plan, baseline=flow_baseline(args.flow),
                              ticket=args.ticket, summary=args.summary))


def remote_host(url):
    url = url or ''
    if 'github' in url:
        return 'github'
    if 'gitlab' in url:
        return 'gitlab'
    return None


def plan_title(plan):
    try:
        for line in Path(plan).read_text(encoding='utf-8').splitlines():
            if line.startswith('# '):
                return line[2:].strip()
    except OSError:
        pass
    return None


def cmd_publish(args):
    """Push the run's branch and open (or reuse) a pull request against its base branch."""
    sdir = session_dir(args.session)
    meta = read_json(sdir / 'session.json', {}) or {}
    st = read_json(sdir / 'state.json', {}) or {}
    if meta.get('kind') != 'run':
        die('publish takes a run session')
    mode = args.mode or 'pr'
    branch, folder, base, base_why = run_work(meta)
    if args.base:
        base, base_why = args.base, 'chosen with --base'
    out = {'branch': branch, 'folder': folder, 'base': base, 'base_why': base_why,
           'mode': mode, 'pushed': False, 'pr': None, 'skipped': None}

    def finish(skipped=None, **extra):
        out.update(extra, skipped=skipped)
        text = (f"Opened pull request {out['pr']['url']} ({branch} into {base})"
                if out['pr'] and out['pr'].get('created')
                else f"Updated pull request {out['pr']['url']}" if out['pr']
                else f'Pushed {branch} to {out.get("remote")}' if out['pushed']
                else f'Did not publish {branch}')
        log_decision(sdir, kind='decision', by='autopilot', text=text,
                     reason=skipped or (f'publish mode {mode}; base: {base_why}'))
        emit(out)

    def git(*argv, timeout=60):
        return run_quiet(['git', *argv], cwd=folder, timeout=timeout)

    if mode == 'off':
        return finish('publish --mode off: the user asked to keep the branch local')
    if run_quiet(['git', 'rev-parse', '--verify', '-q', f'refs/heads/{branch}'],
                 cwd=meta['repo'])[0] != 0:
        return finish(f'the run made no branch {branch}')
    if branch == base:
        return finish(f'the run worked on {base} itself: nothing to compare')
    _, ahead = git('rev-list', '--count', f'{base}..{branch}')
    out['commits'] = int(ahead) if (ahead or '').isdigit() else None
    if not out['commits']:
        return finish(f'{branch} has no commits ahead of {base}')
    out['uncommitted'] = porcelain_paths(folder)
    _, remotes = git('remote')
    remotes = (remotes or '').split()
    remote = 'origin' if 'origin' in remotes else (remotes[0] if remotes else None)
    out['remote'] = remote
    if not remote:
        return finish('the repository has no git remote')
    code, msg = git('push', '-u', remote, f'{branch}:{branch}', timeout=300)
    if code != 0:
        return finish('git push failed (never forced): ' + (msg or '').splitlines()[-1][:300])
    out['pushed'] = True
    if mode != 'pr':
        return finish()
    code, heads = git('ls-remote', '--heads', remote, base, timeout=60)
    if code == 0 and not (heads or '').strip():
        default = (default_branch(meta['repo']) or '').removeprefix('origin/')
        base, base_why = default, f'{out["base"]} is not on {remote}: the default branch'
        out.update(base=base, base_why=base_why)
    _, url = git('config', '--get', f'remote.{remote}.url')
    host = remote_host(url)
    title = args.title or plan_title(st.get('plan_file') or meta['target']) or branch
    draft = args.draft or st.get('status') != 'done'
    body = Path(args.body_file).read_text(encoding='utf-8') if args.body_file else \
        f'Implements `{Path(meta["target"]).name}` (autopilot run {sdir.name}).\n'
    if host == 'github' and shutil.which('gh'):
        code, view = run_quiet(['gh', 'pr', 'view', branch, '--json', 'url,state,baseRefName'],
                               cwd=folder, timeout=60)
        try:
            existing = json.loads(view) if code == 0 else None
        except ValueError:
            existing = None
        if existing and existing.get('state') == 'OPEN':
            return finish(pr={'url': existing['url'], 'created': False,
                              'base': existing.get('baseRefName')})
        argv = ['gh', 'pr', 'create', '--base', base, '--head', branch, '--title', title,
                '--body', body] + (['--draft'] if draft else [])
    elif host == 'gitlab' and shutil.which('glab'):
        argv = ['glab', 'mr', 'create', '--source-branch', branch, '--target-branch', base,
                '--title', title, '--description', body, '--yes'] + (['--draft'] if draft else [])
    else:
        tool = {'github': 'gh', 'gitlab': 'glab'}.get(host)
        return finish(f'pushed only: install and log in to {tool} to open the pull request'
                      if tool else f'pushed only: no pull request support for {url}')
    code, msg = run_quiet(argv, cwd=folder, timeout=120)
    link = re.findall(r'https?://\S+', msg or '')
    if code != 0 or not link:
        return finish('pull request not created: ' + ((msg or '').splitlines() or ['?'])[-1][:300])
    return finish(pr={'url': link[-1], 'created': True, 'draft': draft, 'base': base})


def cmd_report(args):
    """Everything that happened in a flow (or one session), for the final report."""
    root = state_root() / 'sessions'
    if args.session:
        sdir = session_dir(args.session)
        flow = read_json(sdir / 'session.json', {}).get('flow')
    else:
        flow = args.flow
    dirs = sorted(d for d in root.iterdir() if (d / 'session.json').exists()
                  and read_json(d / 'session.json', {}).get('flow') == flow) \
        if flow and root.exists() else []
    if args.session and not dirs:
        dirs = [session_dir(args.session)]
    if not dirs:
        die(f'no sessions for flow {flow}')
    sessions, decisions, repo = [], [], None
    for d in dirs:
        meta, st = read_json(d / 'session.json', {}), read_json(d / 'state.json', {}) or {}
        repo = meta.get('repo')
        sessions.append({
            'session': d.name, 'kind': meta.get('kind'), 'target': meta.get('target'),
            'status': st.get('status'), 'plan_file': st.get('plan_file'),
            'started': meta.get('created_iso'),
            'minutes': round(((st.get('ended') or now()) - meta.get('created', now())) / 60, 1),
            'attempts': st.get('attempt'), 'tasks': st.get('tasks'),
            'executor': meta.get('executor'), 'models_used': st.get('models_used', {}),
            'task_log': st.get('task_log', {}), 'steps': st.get('steps', []),
            'router_fallbacks': st.get('router_fallbacks', []),
            'restarts': st.get('restarts', []), 'last_error': st.get('last_error'),
            'errors': st.get('errors', []), 'phases': [
                e['section'] for e in st.get('events', []) if e['kind'] == 'section'],
            'log': str(d / 'output.log')})
        path = d / 'decisions.jsonl'
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    decisions.append({'session': d.name, **json.loads(line)})
                except ValueError:
                    pass
    decisions.sort(key=lambda e: e.get('t', ''))
    # every choice taken on a timeout, by the supervisor or by the agent, for its own section
    timeouts = [{'t': d.get('t'), 'session': d['session'], 'kind': d.get('prompt') or d.get('kind'),
                 'question': d.get('question'), 'chosen': d.get('answer') or d.get('text'),
                 'waited_min': d.get('waited_min'), 'recommended': d.get('recommended'),
                 'reason': d.get('reason')}
                for d in decisions if d.get('by') == 'auto']
    # every choice the user did not make: timeouts, agent and autopilot choices, recoveries
    without_user = [{'t': d.get('t'), 'session': d['session'], 'kind': d.get('prompt') or d.get('kind'),
                     'by': d.get('by'), 'question': d.get('question'),
                     'chosen': d.get('answer') or d.get('text'), 'reason': d.get('reason'),
                     'waited_min': d.get('waited_min'), 'recommended': d.get('recommended')}
                    for d in decisions if d.get('by', 'user') != 'user' and d.get('kind') != 'note']
    git = {}
    runs = [read_json(d / 'session.json', {}) for d in dirs]
    runs = [m for m in runs if m.get('kind') == 'run' and m.get('repo')]
    if repo:
        if runs:
            branch, folder, base, _ = run_work(runs[-1])
        else:
            base = default_branch(repo)
            folder = repo
            _, branch = run_quiet(['git', 'branch', '--show-current'], cwd=repo)
        git = {'branch': branch, 'base': base, 'folder': folder}
        published = [d for d in decisions if d.get('by') == 'autopilot'
                     and re.match(r'(Opened|Updated) pull request|Pushed |Did not publish',
                                  d.get('text') or '')]
        if published:
            git['published'] = published[-1]['text']
        if base and branch and branch != base.split('/')[-1]:
            _, commits = run_quiet(['git', 'log', '--oneline', '--no-decorate',
                                    f'{base}..{branch}'], cwd=folder)
            _, stat = run_quiet(['git', 'diff', '--stat', f'{base}...{branch}'], cwd=folder)
            git['commits'] = commits.splitlines()[:60]
            git['diffstat'] = stat.splitlines()[-25:]
    emit({'flow': flow, 'sessions': sessions, 'decisions': decisions, 'timeouts': timeouts,
          'without_user': without_user, 'git': git})


def cmd_stop(args):
    sdir = session_dir(args.session)
    st = read_json(sdir / 'state.json', {}) or {}
    if args.after_task or args.cancel:
        if not session_active(st):
            die('the session is not running')
        (sdir / 'inbox' / f'{time.time_ns()}.json').write_text(json.dumps(
            {'stop_after_task': bool(args.after_task), 'by': args.by, 'reason': args.reason}))
        emit({'ok': True, 'session': sdir.name,
              'status': 'stop queued' if args.after_task else 'stop cancelled'})
        return
    if pid_alive(st.get('pid')):
        inbox = sdir / 'inbox'
        (inbox / f'{time.time_ns()}.json').write_text(json.dumps({'stop': True}))
        for _ in range(150):
            if not pid_alive(st.get('pid')):
                break
            time.sleep(0.2)
        kill_tree(st.get('pid'), graceful=False)
    if pid_alive(st.get('child_pid')):
        kill_tree(st.get('child_pid'))
    emit({'ok': True, 'session': sdir.name, 'status': 'stopped'})


def cmd_resume(args):
    """Start a stopped or failed run again: same plan, flags and flow, from the first open task."""
    sdir = session_dir(args.session)
    meta = read_json(sdir / 'session.json', {}) or {}
    st = read_json(sdir / 'state.json', {}) or {}
    if meta.get('kind') != 'run':
        die('only run sessions can be resumed; start planning again with plan')
    if session_active(st):
        die('the session is still running')
    os.chdir(meta['repo'])
    write_json(sdir / 'reported.json', {'t': now(), 'by': 'resume'})  # the new session carries on
    start_session('run', meta['target'], meta.get('cli_args', []), meta.get('max_restarts', 5),
                  meta.get('flow'))


def cmd_shim(args):
    sdir = Path(os.environ.get('AUTOPILOT_SESSION', '.'))
    if args.name == 'fzf':
        prompt = ''
        rest = list(args.rest)
        if '--prompt' in rest:
            i = rest.index('--prompt')
            prompt = rest[i + 1] if i + 1 < len(rest) else ''
        options = sys.stdin.read()
        (sdir / 'fzf-options.txt').write_text(options, encoding='utf-8')
        (sdir / 'fzf-question.txt').write_text(prompt, encoding='utf-8')
        sys.exit(1)  # "no match": the CLI then reads a typed answer from stdin
    if args.name == 'autopilot-editor':
        edited = sdir / 'draft-edited.md'
        if not edited.exists() or not args.rest:
            sys.exit(1)
        shutil.copyfile(edited, args.rest[-1])
        sys.exit(0)
    sys.exit(2)


# ---------------------------------------------------------------- argparse

def main(argv=None):
    if WINDOWS:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8')
    argv = list(sys.argv[1:] if argv is None else argv)
    cli_args = []
    if '--' in argv:
        i = argv.index('--')
        argv, cli_args = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog='autopilot.py', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('doctor')
    s.add_argument('--json', action='store_true')
    s.add_argument('--latest', action='store_true', help='look up latest releases on GitHub')
    s.add_argument('--updates', action='store_true',
                   help='report newer releases of the runner and router (looked up once a day)')
    s.add_argument('--skip-updates', action='store_true',
                   help='stop offering the releases --updates reports now')
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser('plan')
    s.add_argument('--desc', required=True)
    s.add_argument('--max-restarts', type=int, default=2)
    s.add_argument('--flow', help='flow id shared by the sessions of one full run')
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser('plan-review')
    s.add_argument('plan')
    s.add_argument('--max-restarts', type=int, default=2)
    s.add_argument('--flow', help='flow id shared by the sessions of one full run')
    s.set_defaults(fn=cmd_plan_review)
    s = sub.add_parser('run')
    s.add_argument('--plan', required=True)
    s.add_argument('--max-restarts', type=int, default=5)
    s.add_argument('--force', action='store_true', help='run even when check-plan says redesign')
    s.add_argument('--flow', help='flow id shared by the sessions of one full run')
    s.add_argument('--new-worktree', action='store_true',
                   help='make a worktree in <parent>/workspaces/<repo>/<Name> and run there')
    s.add_argument('--worktree-path', help='folder for the new worktree (implies --new-worktree)')
    s.add_argument('--branch', help='name of the run branch (passed to the runner as --branch)')
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser('wait')
    s.add_argument('session')
    s.add_argument('--timeout', type=int, default=240)
    s.add_argument('--since', type=int)
    s.add_argument('--any', action='store_true', help='also return on phase changes')
    s.add_argument('--catch-up', action='store_true',
                   help='start after the last event a wait reported (reattaching)')
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser('next')
    s.add_argument('session')
    s.add_argument('--timeout', type=int, default=6900)
    s.add_argument('--json', action='store_true', help='the full wait report instead of text')
    s.add_argument('--catch-up', action='store_true',
                   help='reattach: also return an open prompt an earlier watch reported')
    s.set_defaults(fn=cmd_next)
    s = sub.add_parser('watch-status')
    s.add_argument('session', nargs='?')
    s.set_defaults(fn=cmd_watch_status)
    s = sub.add_parser('mark-reported')
    s.add_argument('session')
    s.set_defaults(fn=cmd_mark_reported)
    s = sub.add_parser('resume')
    s.add_argument('session')
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser('hook')
    s.add_argument('event', choices=['session-start', 'prompt', 'stop'])
    s.set_defaults(fn=cmd_hook)
    s = sub.add_parser('answer')
    s.add_argument('session')
    s.add_argument('--choice', type=int)
    s.add_argument('--text')
    s.add_argument('--text-file', help='read --text from a file (no shell quoting)')
    s.add_argument('--action', choices=['accept', 'revise', 'edited', 'reject'])
    s.add_argument('--yes', action='store_true')
    s.add_argument('--no', action='store_true')
    s.add_argument('--by', default='user', choices=['user', 'agent'])
    s.add_argument('--reason')
    s.set_defaults(fn=cmd_answer)
    s = sub.add_parser('recommend')
    s.add_argument('session')
    s.add_argument('--choice', type=int)
    s.add_argument('--text')
    s.add_argument('--text-file', help='read --text from a file (no shell quoting)')
    s.add_argument('--action', choices=['accept', 'revise', 'edited', 'reject'])
    s.add_argument('--yes', action='store_true')
    s.add_argument('--no', action='store_true')
    s.add_argument('--reason', required=True)
    s.set_defaults(fn=cmd_recommend)
    s = sub.add_parser('log')
    s.add_argument('session')
    s.add_argument('--kind', required=True,
                   choices=['decision', 'recovery', 'fix', 'answer', 'note'])
    s.add_argument('--text', required=True)
    s.add_argument('--reason')
    s.add_argument('--by', default='agent',
                   help='user, agent, or auto (taken on a timeout without a reply)')
    s.add_argument('--question', help='what was asked (for timeout decisions)')
    s.add_argument('--waited', type=float, help='minutes without a reply before a timeout decision')
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser('skip-review')
    s.add_argument('session')
    s.add_argument('--by', default='user', choices=['user', 'agent'])
    s.add_argument('--reason')
    s.set_defaults(fn=cmd_skip_review)
    s = sub.add_parser('extend')
    s.add_argument('session')
    s.add_argument('--by', default='30m', help='more time from now: 10m, 1h, ...')
    s.add_argument('--never', action='store_true', help='switch auto-answer off')
    s.set_defaults(fn=cmd_extend)
    s = sub.add_parser('ask-timer')
    s.add_argument('action', choices=['start', 'wait', 'cancel', 'extend'])
    s.add_argument('--id', required=True)
    s.add_argument('--decision', help='policy key, for start')
    s.add_argument('--seconds', help='override the policy timeout, for start (10m, 1h, never)')
    s.add_argument('--max', type=int, default=1500, help='longest single wait call (s)')
    s.add_argument('--by', default='30m')
    s.add_argument('--never', action='store_true')
    s.set_defaults(fn=cmd_ask_timer)
    s = sub.add_parser('policy')
    s.set_defaults(fn=cmd_policy)
    s = sub.add_parser('timer')
    s.add_argument('seconds', type=int)
    s.set_defaults(fn=cmd_timer)
    s = sub.add_parser('report')
    s.add_argument('session', nargs='?')
    s.add_argument('--flow')
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser('stop')
    s.add_argument('session')
    s.add_argument('--after-task', action='store_true',
                   help='stop once the current task (or review step) is finished and committed')
    s.add_argument('--cancel', action='store_true', help='cancel a queued --after-task stop')
    s.add_argument('--by', default='user')
    s.add_argument('--reason')
    s.set_defaults(fn=cmd_stop)
    s = sub.add_parser('publish')
    s.add_argument('session', help='the run session')
    s.add_argument('--mode', choices=['pr', 'push', 'off'],
                   help='default pr; push or off only when the user asks for it in the chat')
    s.add_argument('--base', help='target branch (default: the branch the run started from)')
    s.add_argument('--title')
    s.add_argument('--body-file', help='pull request description (Markdown)')
    s.add_argument('--draft', action='store_true',
                   help='open as a draft (always when the run did not finish)')
    s.set_defaults(fn=cmd_publish)
    s = sub.add_parser('place')
    s.add_argument('--plan', help='the plan file (an uncommitted plan does not count as dirty)')
    s.add_argument('--flow', help='the flow: lists the files a new worktree takes from here')
    s.add_argument('--ticket', help='ticket id for {ticket} in [worktree] branch/path patterns')
    s.add_argument('--summary', help='text for {summary} (default: the plan title)')
    s.set_defaults(fn=cmd_place)
    s = sub.add_parser('list')
    s.add_argument('--all', action='store_true')
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser('config-set')
    s.add_argument('file')
    s.add_argument('key')
    s.add_argument('--section')
    s.add_argument('--value')
    s.add_argument('--value-stdin', action='store_true')
    s.add_argument('--private', action='store_true', help='0600 file, 0700 directory')
    s.add_argument('--comment', help='full-line comment(s) written above the key')
    s.set_defaults(fn=cmd_config_set)
    s = sub.add_parser('install-wrapper')
    s.add_argument('--docs', nargs='*', default=[], help='doc files, relative to the repo root')
    s.add_argument('--doc-dirs', nargs='*', default=[], help='directories whose *.md are docs')
    s.add_argument('--windows', action='store_true', help='install the .cmd + .py pair')
    s.add_argument('--force', action='store_true')
    s.set_defaults(fn=cmd_install_wrapper)
    s = sub.add_parser('config-init')
    s.add_argument('dest', nargs='?', help='default: the tool\'s repository config path')
    s.add_argument('--tool', choices=sorted(CONFIG_DEST), default='ralphex')
    s.add_argument('--merge', action='store_true',
                   help='upgrade a partial file: full template plus its current values (backup in the state dir)')
    s.add_argument('--force', action='store_true')
    s.set_defaults(fn=cmd_config_init)
    s = sub.add_parser('plan-rev')
    s.add_argument('action', choices=['snapshot', 'diff', 'supersede'])
    s.add_argument('plan')
    s.add_argument('--by', help='new plan file, for supersede')
    s.set_defaults(fn=cmd_plan_rev)
    s = sub.add_parser('check-plan')
    s.add_argument('plan')
    s.set_defaults(fn=cmd_check_plan)
    s = sub.add_parser('config-summary')
    s.set_defaults(fn=cmd_config_summary)
    s = sub.add_parser('config-unset')
    s.add_argument('file')
    s.add_argument('key')
    s.add_argument('--section')
    s.set_defaults(fn=cmd_config_unset)
    s = sub.add_parser('set-docs')
    s.add_argument('--docs', nargs='*', default=[])
    s.add_argument('--doc-dirs', nargs='*', default=[])
    s.add_argument('--replace-wrapper', action='store_true',
                   help='replace a hand-written wrapper with the template')
    s.set_defaults(fn=cmd_set_docs)
    s = sub.add_parser('config-show')
    s.add_argument('file')
    s.set_defaults(fn=cmd_config_show)
    s = sub.add_parser('catalog')
    s.set_defaults(fn=cmd_catalog)
    s = sub.add_parser('compact')
    s.add_argument('--flow', required=True)
    s.add_argument('--handoff', required=True, help='file the chat reads after the compaction')
    s.add_argument('--focus', help='the /compact instructions (a default names the flow)')
    s.set_defaults(fn=cmd_compact)
    s = sub.add_parser('_compact')
    s.add_argument('request')
    s.set_defaults(fn=cmd_compact_helper)
    s = sub.add_parser('_supervise')
    s.add_argument('sdir')
    s.set_defaults(fn=cmd_supervise)
    s = sub.add_parser('_shim')
    s.add_argument('name')
    s.add_argument('rest', nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_shim)
    args = p.parse_args(argv)
    args.cli_args = cli_args
    if args.cmd == '_shim':
        args.rest = args.rest + cli_args
    args.fn(args)


if __name__ == '__main__':
    main()
