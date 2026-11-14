#!/usr/bin/env python3
"""Drive the plan-and-execute CLI for the autopilot skills.

The CLI (ralphex) asks questions through a terminal picker and runs for hours.
Agents call tools without a terminal and cannot hold a session open, so this
helper starts the CLI under a detached supervisor, keeps its stdin open,
detects prompts, accepts answers through an inbox, restarts failed runs and
reports progress as compact JSON.

Commands:
  doctor [--json] [--latest]          tools, versions, configs, repo state
  plan  --desc TEXT [-- ARGS]         start interactive plan creation
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
ERROR_RE = re.compile(r"(?i)^(ERROR:|Failed:|panic:|fatal:|error:)|^agrouter: (?!selection:)|"
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
    (re.compile(r'finali[sz]e'), 'finalizing'),
]


# ---------------------------------------------------------------- utilities

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=None))


def die(msg, code=1):
    emit({'error': msg})
    sys.exit(code)


def now():
    return time.time()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
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


def run_quiet(argv, timeout=15, cwd=None):
    try:
        res = subprocess.run(**command_kwargs(argv), capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=timeout,
                             cwd=cwd, stdin=subprocess.DEVNULL)
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


def pid_alive(pid):
    if not pid:
        return False
    if WINDOWS:
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


def autopilot_config(section):
    """Merged [section] of ~/.config/autopilot/config (AUTOPILOT_CONFIG) and <repo>/.autopilot/config."""
    files = [Path(os.environ.get('AUTOPILOT_CONFIG') or Path.home() / '.config/autopilot/config')]
    repo = git_root()
    if repo:
        files.append(Path(repo) / '.autopilot/config')
    values = {}
    for cfg in files:
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
          'source': [s for s in (os.environ.get('AUTOPILOT_CONFIG') or
                                 str(Path.home() / '.config/autopilot/config'),)
                     if Path(s).exists()] + (['AUTOPILOT_TIMEOUTS']
                                             if os.environ.get('AUTOPILOT_TIMEOUTS') else [])})


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


def cmd_config_init(args):
    """Copy the runner's commented default config (only the config file) to DEST."""
    dest = Path(args.dest)
    if dest.exists() and not args.force:
        emit({'ok': True, 'file': str(dest), 'created': False})
        return
    cli = find_cli()
    if not cli:
        die('not installed: ralphex.windows; run the autopilot-setup skill first')
    tmp = state_root() / f'dump-{secrets.token_hex(4)}'
    try:
        code, out = run_quiet([cli, '--dump-defaults', str(tmp)], timeout=60)
        src = tmp / 'config'
        if code != 0 or not src.exists():
            die(f'--dump-defaults failed: {out[-300:]}')
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(comment_out_values(src.read_text(encoding='utf-8')),
                        encoding='utf-8')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    emit({'ok': True, 'file': str(dest), 'created': True})


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


def latest_release(repo, contains=None):
    url = f'https://api.github.com/repos/{repo}/releases?per_page=10'
    req = urllib.request.Request(url, headers={'User-Agent': 'autopilot-skill',
                                               'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
        'issues': spec_problems(executor, eff, router_catalog(repo)[0]) + ([f'doc drift: {doc_drift(repo, eff.get("claude_args", ""), eff.get("codex_command"))}']
                                                  if ROUTER in eff.get('claude_command', '')
                                                  and doc_drift(repo, eff.get('claude_args', ''),
                                                                eff.get('codex_command')) else []),
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
    if os.environ.get('AUTOPILOT_CODEX_LOGIN_OK') != '1':
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
    pref = autopilot_config('review').get('plan', 'off')
    other = 'codex' if executor == 'claude' else 'claude'
    reviewer = other if pref == 'auto' else (None if pref == 'off' else pref)
    usable = {'codex': (codex_ok, codex_why), 'claude': (claude_ok, claude_why)}
    plan = {'setting': pref, 'reviewer': reviewer,
            'usable': bool(reviewer) and usable.get(reviewer, (False,))[0],
            'reason': (usable.get(reviewer, (False, 'unknown reviewer'))[1] if reviewer
                       else 'plan review is off'),
            'cross_model': other, 'cross_model_usable': usable[other][0]}
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


PLAN_REVIEW_PROMPT = """Review this implementation plan before it is executed. Do not change any
files: only read the plan and the repository and report.

Check: does it fully cover the task; are the files and steps right for this codebase; does
every task have a test step; are the validation commands correct; are there risky steps,
missing migrations, wrong order or tasks too large for one session.

Answer with a short list of findings, most important first, each as
"- [high|medium|low] <finding> -> <suggested change>". When the plan is fine, answer
"No findings."

Task: {task}

Plan ({path}):
{plan}
"""


def cmd_review_plan(args):
    """Have the other model review a plan read-only, through the router when configured."""
    repo = git_root()
    plan = Path(args.plan)
    if not plan.is_file():
        die(f'plan file not found: {plan}')
    cfg = merged(cli_config_paths(repo)).get('', {})
    if args.executor:
        cfg['executor'] = args.executor
    cap = review_capability(repo, cfg)
    reviewer = args.reviewer or cap['plan_review']['reviewer'] or cap['plan_review']['cross_model']
    ok = cap['codex']['usable'] if reviewer == 'codex' else cap['claude']['usable']
    if not ok:
        emit({'ok': False, 'skipped': True, 'reviewer': reviewer,
              'reason': cap[reviewer]['reason'], 'findings': [], 'no_findings': False})
        return
    prompt = PLAN_REVIEW_PROMPT.format(task=args.task or '(see the plan overview)', path=plan,
                                       plan=plan.read_text(encoding='utf-8', errors='replace'))
    wrapper = parse_wrapper(repo, cfg.get('codex_command'))
    if reviewer == 'codex':
        command = cfg.get('codex_command') if wrapper and wrapper['routes'] else 'codex'
        argv = [command, 'exec', '--sandbox', 'read-only']
    elif ROUTER in cfg.get('claude_command', ''):
        docs = [f'--doc={d}' for d in claude_docs(cfg.get('claude_args')) if (Path(repo) / d).is_file()]
        argv = [ROUTER, 'exec', 'claude', *docs, '--', '--permission-mode', 'plan']
    else:
        argv = ['claude', '-p', '--permission-mode', 'plan']
    env = {k: v for k, v in os.environ.items() if k not in HOST_SESSION_VARS}
    started = now()
    try:
        env['PYTHONUTF8'] = '1'
        env['AUTOPILOT_PYTHON'] = sys.executable
        res = subprocess.run(**command_kwargs(argv), input=prompt, capture_output=True,
                             text=True, encoding='utf-8', errors='replace', cwd=repo, env=env,
                             timeout=args.timeout)
        code, output = res.returncode, (res.stdout or '') + (res.stderr or '')
    except subprocess.TimeoutExpired:
        code, output = None, f'review timed out after {args.timeout} s'
    except OSError as exc:
        code, output = None, str(exc)
    out = plan_store(plan) / f'review-{time.strftime("%Y%m%d-%H%M%S")}-{reviewer}.md'
    out.write_text(output, encoding='utf-8')
    findings = [l.strip() for l in output.splitlines() if re.match(r'\s*-\s*\[(high|medium|low)\]', l, re.I)]
    emit({'ok': code == 0, 'reviewer': reviewer, 'command': ' '.join(argv[:3]), 'exit_code': code,
          'seconds': round(now() - started), 'file': str(out), 'findings': findings[:20],
          'no_findings': 'no findings' in output.lower() and not findings,
          'tail': output.splitlines()[-15:] if code != 0 else []})


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
    if args.latest:
        info['latest'] = {'cli': latest_release('svtlv/ralphex', '-windows'),
                          'router': latest_release('svtlv/agrouter')}
    missing = []
    if not cli:
        missing.append('plan runner (ralphex.windows) is not installed')
    if not info['router']['path']:
        missing.append('model router (agrouter) is not installed')
    incomplete = []
    if info['router']['path'] and not info['router_config']['key_set']:
        incomplete.append('classifier API key is not set')
    if repo and not info['cli_config']['local_exists']:
        incomplete.append('this repository has no runner config (.ralphex/config)')
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

def list_sessions(repo=None, all_repos=False):
    root = state_root() / 'sessions'
    out = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        meta = read_json(d / 'session.json')
        if not meta or (not all_repos and repo and meta.get('repo') != repo):
            continue
        st = read_json(d / 'state.json', {})
        out.append({'session': d.name, 'kind': meta['kind'], 'target': meta.get('target'),
                    'status': st.get('status'), 'alive': pid_alive(st.get('pid')),
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


def start_session(kind, target, cli_args, max_restarts, flow=None):
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
    argv = [cli, '--no-color']
    argv += ['--plan', target] if kind == 'plan' else [target]
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
    write_json(sdir / 'session.json', {
        'kind': kind, 'target': target, 'repo': repo, 'argv': argv,
        'max_restarts': max_restarts, 'created': now(),
        'flow': flow or f'flow-{stamp}-{secrets.token_hex(2)}', 'executor': executor,
        'policy': load_policy(),
        'created_iso': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'repo_hash': hashlib.sha1(repo.encode()).hexdigest()[:8]})
    write_json(sdir / 'state.json', {'status': 'starting', 'seq': 0, 'events': []})
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
          'executor': executor, 'executor_fallback_reason': executor_note})


def cmd_plan(args):
    start_session('plan', args.desc, args.cli_args, args.max_restarts, args.flow)


def cmd_run(args):
    require_tools()
    plan = Path(args.plan)
    if not plan.exists():
        die(f'plan file not found: {plan}')
    verdict = check_plan(plan)
    if verdict['verdict'] == 'redesign' and not args.force:
        die('the runner cannot use this plan: ' + '; '.join(verdict['problems']) +
            '. Redesign it with the autopilot-plan skill (--redesign), or pass --force')
    start_session('run', str(plan), args.cli_args, args.max_restarts, args.flow)


# ---------------------------------------------------------------- supervisor

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

    # -- state
    def save(self):
        with self.state_lock:
            self.state['updated'] = now()
            write_json(self.sdir / 'state.json', self.state)

    def event(self, kind, **data):
        if kind in ('done', 'failed', 'stopped'):
            self.state['ended'] = now()
        with self.state_lock:
            self.state['seq'] += 1
            ev = {'seq': self.state['seq'], 't': time.strftime('%H:%M:%S'),
                  'kind': kind, **data}
            self.state['events'] = (self.state['events'] + [ev])[-40:]
            self.save()

    # -- worktree runs
    def worktree_mode(self):
        argv = self.meta['argv']
        if '--worktree' in argv:
            return 'flag'
        cfg = merged(cli_config_paths(self.meta['repo'])).get('', {})
        return 'config' if cfg.get('use_worktree', '').lower() == 'true' else None

    def run_branch(self):
        """The branch the runner uses for this plan (--branch, or the plan name sans date)."""
        argv = self.meta['argv']
        for i, arg in enumerate(argv):
            if arg == '--branch' and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith('--branch='):
                return arg.split('=', 1)[1]
        name = Path(self.meta['target']).name
        name = name[:-3] if name.endswith('.md') else name
        return re.sub(r'^[\d-]+', '', name).lstrip('-') or name

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
                self.scan_line(clean_line(line))
        if partial:
            self.scan_line(clean_line(partial))

    def scan_line(self, line):
        m = SECTION_RE.match(line)
        if m:
            label = m.group(1)
            phase = next((p for rx, p in PHASES if rx.search(label)), label)
            self.state.update({'section': label, 'phase': phase})
            self.event('section', section=label, phase=phase)
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
        if line and ERROR_RE.search(line):
            errs = self.state.setdefault('errors', [])
            errs.append({'t': time.strftime('%H:%M:%S'), 'line': line[:300]})
            self.state['errors'] = errs[-8:]
            self.event('error', line=line[:300])

    def scan_model(self, line):
        """Record which CLI/model/effort runs now; True when the line was a selection."""
        m = SELECTION_RE.search(line)
        if m:
            try:
                sel = json.loads(m.group(1))
            except ValueError:
                return True
            self.state['model'] = {k: sel.get(k) for k in ('cli', 'model', 'effort')}
            self.count_model(self.state['model'])
            return True
        m = BANNER_RE.match(line)
        if m:
            model = self.state.setdefault('model', {}) or {}
            if m.group(1) == 'model':
                model = {'cli': 'codex', 'model': m.group(2)}
            else:
                model['effort'] = m.group(2)
            self.state['model'] = model
            if m.group(1) != 'model':
                self.count_model(model)
            self.save()
            return True
        return False

    def count_model(self, model):
        """Tally calls per cli/model/effort for the final report."""
        key = '/'.join(str(model.get(k) or '?') for k in ('cli', 'model', 'effort'))
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
        for field in ('id', 'asked_at', 'deadline', 'remind_at', 'reminded', 'policy_key'):
            current.pop(field, None)
        if current != prompt:
            prompt['id'] = self.state['seq'] + 1
            prompt['asked_at'] = now()
            key = prompt['kind'] if self.meta['kind'] == 'plan' or prompt['kind'] == 'text' \
                else 'runtime'
            rule = (self.meta.get('policy') or load_policy()).get(key) or {}
            prompt['policy_key'] = key
            self.set_deadline(prompt, rule.get('timeout'), rule.get('remind_before'))
            self.state['waiting'] = prompt
            self.state['status'] = 'waiting'
            self.event('waiting', prompt=prompt)

    def process_inbox(self):
        for path in sorted((self.sdir / 'inbox').glob('*.json')):
            msg = read_json(path, {})
            path.unlink(missing_ok=True)
            if msg.get('stop'):
                self.stop_requested = True
                kill_tree(self.proc.pid if self.proc else None)
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

    def deliver(self, waiting, send, summary, by, reason=None):
        self.send(send)
        self.state['answered_at'] = now()
        self.state['waiting'] = None
        self.state['status'] = 'running'
        log_decision(self.sdir, kind='answer', by=by, prompt=waiting['kind'],
                     question=describe_prompt(waiting), options=waiting.get('options'),
                     answer=summary, reason=reason)
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
        else:
            choice = fallback_answer(waiting)
        if not choice:
            if not waiting.get('needs_input'):
                waiting['needs_input'] = True
                self.event('needs-input', question=describe_prompt(waiting))
            return
        self.deliver(waiting, choice[0], choice[1], 'auto', choice[2].strip())

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
        done = total = 0
        current = None
        task = None
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            hm = re.match(r'^###\s+(.*)', line)
            if hm:
                task = hm.group(1).strip()
            cm = re.match(r'^\s*- \[([ xX])\]', line)
            if cm:
                total += 1
                if cm.group(1) != ' ':
                    done += 1
                elif current is None:
                    current = task
        return {'done': done, 'total': total, 'current_task': current}

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
        tick = 0
        while self.proc.poll() is None:
            self.process_inbox()
            prompt = self.detect_prompt()
            if prompt:
                self.handle_prompt(prompt)
            self.check_deadline()
            tick += 1
            if tick % 25 == 0:
                self.state['tasks'] = self.plan_progress()
                self.save()
            time.sleep(0.2)
        time.sleep(0.3)  # let the reader drain
        self.state['tasks'] = self.plan_progress()
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
                self.event('stopped')
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
    if answer_pending(sdir, st):
        status, waiting = 'running', None
    if status in ('running', 'waiting', 'restarting', 'starting') and not alive:
        status = 'lost'
    return {
        'session': sdir.name, 'kind': meta.get('kind'), 'target': meta.get('target'),
        'status': status, 'seq': st.get('seq', 0),
        'new_events': [e for e in st.get('events', []) if e['seq'] > since],
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


def cmd_wait(args):
    """Block until something needs the agent: a new prompt, a notable event, the end, or timeout.

    An open prompt wakes the agent only once (when it is newer than --since); after that
    the wait continues until a reminder, an auto-answer or another event arrives.
    """
    sdir = session_dir(args.session)
    since = args.since
    if since is None:
        since = (read_json(sdir / 'state.json', {}) or {}).get('seq', 0)
    end = now() + args.timeout
    quiet = {'section', 'started', 'answered', 'auto-answer', 'waiting', 'limit-retry',
             'transient', 'extended'}
    while True:
        st = read_json(sdir / 'state.json', {}) or {}
        fresh = [e for e in st.get('events', []) if e['seq'] > since]
        prompt = st.get('waiting') or {}
        new_prompt = st.get('status') == 'waiting' and not answer_pending(sdir, st) and (
            args.since is None or prompt.get('id', 0) > args.since)
        ended = st.get('status') in ('done', 'failed', 'stopped')
        if new_prompt or ended or not pid_alive(st.get('pid')) and st.get('status') != 'starting':
            break
        if any(e['kind'] not in quiet for e in fresh):
            break
        if args.any and fresh:
            break
        if now() >= end:
            break
        time.sleep(1)
    out = report(sdir, st, since)
    out['heartbeat'] = not [e for e in out['new_events'] if e['kind'] not in quiet] \
        and not new_prompt and out['status'] not in ('done', 'failed', 'stopped', 'lost')
    emit(out)


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
    log_decision(sdir, kind=args.kind, by=args.by, text=one_line(args.text),
                 reason=one_line(args.reason) if args.reason else None)
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
    git = {}
    if repo:
        base = default_branch(repo)
        _, branch = run_quiet(['git', 'branch', '--show-current'], cwd=repo)
        git = {'branch': branch, 'base': base}
        if base and branch and branch != base.split('/')[-1]:
            _, commits = run_quiet(['git', 'log', '--oneline', '--no-decorate',
                                    f'{base}..HEAD'], cwd=repo)
            _, stat = run_quiet(['git', 'diff', '--stat', f'{base}...HEAD'], cwd=repo)
            git['commits'] = commits.splitlines()[:60]
            git['diffstat'] = stat.splitlines()[-25:]
    emit({'flow': flow, 'sessions': sessions, 'decisions': decisions, 'git': git})


def cmd_stop(args):
    sdir = session_dir(args.session)
    st = read_json(sdir / 'state.json', {}) or {}
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
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser('plan')
    s.add_argument('--desc', required=True)
    s.add_argument('--max-restarts', type=int, default=2)
    s.add_argument('--flow', help='flow id shared by the sessions of one full run')
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser('run')
    s.add_argument('--plan', required=True)
    s.add_argument('--max-restarts', type=int, default=5)
    s.add_argument('--force', action='store_true', help='run even when check-plan says redesign')
    s.add_argument('--flow', help='flow id shared by the sessions of one full run')
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser('wait')
    s.add_argument('session')
    s.add_argument('--timeout', type=int, default=240)
    s.add_argument('--since', type=int)
    s.add_argument('--any', action='store_true', help='also return on phase changes')
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser('answer')
    s.add_argument('session')
    s.add_argument('--choice', type=int)
    s.add_argument('--text')
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
    s.add_argument('--by', default='agent')
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser('skip-review')
    s.add_argument('session')
    s.add_argument('--by', default='user', choices=['user', 'agent'])
    s.add_argument('--reason')
    s.set_defaults(fn=cmd_skip_review)
    s = sub.add_parser('review-plan')
    s.add_argument('plan')
    s.add_argument('--task', help='the task the plan should cover')
    s.add_argument('--reviewer', choices=['codex', 'claude'])
    s.add_argument('--executor', choices=['codex', 'claude'],
                   help='actual planning executor, including a per-session fallback')
    s.add_argument('--timeout', type=int, default=900)
    s.set_defaults(fn=cmd_review_plan)
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
    s.set_defaults(fn=cmd_stop)
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
    s.add_argument('dest', nargs='?', default='.ralphex/config')
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
