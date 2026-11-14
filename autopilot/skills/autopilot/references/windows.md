# Windows Scripts

Use native Windows Python 3.9+ and Git. The driver is the same Python file
on every platform; Git Bash and WSL are not required. Use PowerShell
commands below when the host shell is PowerShell. Replace `<driver>` with
the absolute path to this skill's `scripts/autopilot.py`.

```powershell
py -3 "<driver>" doctor --json
py -3 "<driver>" plan --desc "Add caching"
py -3 "<driver>" run --plan "docs/plans/cache.md"
```

When `py` is unavailable, use `python` after verifying `python --version`
reports Python 3.9 or later. Quote paths containing spaces. Set variables
with PowerShell syntax, for example `$env:AUTOPILOT_STATE_DIR = 'D:\autopilot'`.

## Codex Routing Wrapper

`AP install-wrapper` automatically installs the Windows pair:

| File | Purpose |
|---|---|
| `.ralphex/scripts/codex-agrouter.cmd` | Runner entrypoint; chooses Python and forwards arguments |
| `.ralphex/scripts/codex-agrouter.py` | Resolves project docs, removes the extra `exec`, calls agrouter and preserves stdin and the exit code |

Set `codex_command` to the returned `.cmd` path. Commit both files. Keep
forward slashes in the runner config; quote paths in shell commands.
`config-summary`, `set-docs` and plan review recognize the pair and use
the companion Python file's routing docs.

The launcher uses `AUTOPILOT_PYTHON` when set, then tries `py -3`, then
`python`. The driver supplies its own interpreter in `AUTOPILOT_PYTHON`
to its children. The committed launcher contains no machine-specific
Python path. Regenerate old wrappers with `AP install-wrapper --force`
after checking that they are generated templates.

The supervisor installs temporary `fzf.cmd` and `autopilot-editor.cmd`
shims. These use the current interpreter, quote paths and use CRLF line
endings. Python subprocesses use UTF-8 for prompts and logs. npm-installed
`claude.cmd` and `codex.cmd` are recognized as batch entrypoints; plan
review prompts are passed through stdin. The runner starts `fzf.cmd` by
its resolved path, the way it starts `claude.cmd`; Windows runs batch files
through `cmd.exe` itself. `cmd.exe` expands `%NAME%` in that command line,
so such text in a planner question can look changed in `waiting.question`.
Options and answers travel through stdin and files and stay exact. Batch invocation follows
[Python's subprocess guidance](https://docs.python.org/3/library/subprocess.html#security-considerations).

## Stop, Review Skip and Recovery

Windows stops a runner and its descendants with `taskkill /T /F`. There
is no SIGQUIT review break. A skip during external review stops that
attempt and restarts with `--external-review-tool=none`, replacing an
explicit external-review flag too. A skip requested during tasks waits
until external review starts. Completed plan steps remain checked.

Claude/Codex login checks, optional-review skips and guarded Codex
executor fallback work as described in `reviews.md` on both platforms.

## PowerShell Setup Commands

Use a PowerShell pipeline instead of `printf` when storing a key. Read
it without echoing it, then pass it to the global config helper:

```powershell
$key = [System.Net.NetworkCredential]::new('', (Read-Host 'Classifier key' -AsSecureString)).Password
$key | py -3 "<driver>" config-set "$HOME/.config/agrouter/config" api_key --section classifier.typesafe --value-stdin --private
Remove-Variable key
```

Use the correct provider section for the chosen classifier. Never place
the key inside the repository.

PowerShell does not support `< NUL` input redirection. For the optional
paid routing check, use:

```powershell
'' | agrouter --cli codex -p "rename one local variable"
```

Pick the usable CLI. If offering a user-level instructions link, prefer
`New-Item -ItemType SymbolicLink` when Windows Developer Mode or suitable
permissions allow it. Otherwise offer a copy and explain that later edits
will need syncing. Do not use Unix `ln -s` in PowerShell.

## Verification

Run `python -m unittest discover -s tests -p test_autopilot.py` on Windows.
The GitHub Actions workflow runs the driver suite on native Windows and
Linux with Python 3.11 and 3.13. It exercises planning, editor shims,
restarts, queued review skips, login checks, wrapper installation and
argument/stdin forwarding with fake model CLIs. Paid model calls still
require a separate integration run.
