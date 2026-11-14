# Installing and Upgrading the Tools

Both tools come from the user's own forks:

| Tool | Repository | Command name | Release tags |
|---|---|---|---|
| Plan runner | `github.com/svtlv/ralphex` (branch `windows`) | `ralphex.windows` (`ralphex.windows.exe` on Windows) | `vX.Y.Z-windows.N` |
| Model router | `github.com/svtlv/agrouter` | `agrouter` | `vX.Y.Z` |

`doctor --json --latest` reports the installed and the newest versions.
Upgrade when the installed version is older or missing. Show the command and
get a yes before running any installer.

## Plan Runner (ralphex.windows)

| Platform | Install | Upgrade |
|---|---|---|
| macOS (Homebrew) | `brew tap svtlv/ralphex https://github.com/svtlv/ralphex.git` then `brew install --cask ralphex.windows` | `brew update && brew upgrade --cask ralphex.windows` |
| Linux / macOS without Homebrew | `curl -fsSL https://raw.githubusercontent.com/svtlv/ralphex/windows/scripts/install.sh \| sh` (installs to `~/.local/bin`; `RALPHEX_VERSION=<tag>` pins) | rerun the installer |
| Windows (PowerShell) | `irm https://raw.githubusercontent.com/svtlv/ralphex/windows/scripts/install-windows.ps1 \| iex` | rerun the installer |
| Any, with Go | `go install github.com/svtlv/ralphex/cmd/ralphex.windows@windows` (newest commit; `@vX.Y.Z-windows.N` pins a release) | same command |

Notes:

- A Homebrew cask that installed a plain `ralphex` command needs
  `brew update && brew reinstall --cask ralphex.windows`.
- `~/.local/bin` (installer) or `~/go/bin` (Go) must be on `PATH`. When
  `doctor` still cannot find the command after install, show the line to add
  to the shell profile (`export PATH="$HOME/.local/bin:$PATH"`); on Windows,
  the directory goes into the user `Path`, then a new terminal.
- Running a remote install script is outward-facing: say what it does
  (downloads a release, verifies SHA-256, installs one binary) before asking.
- `fzf` is optional. The autopilot driver works without it.

## Model Router (agrouter)

Requires Go 1.27 or later (`go version`). When Go is missing or too old,
offer `brew install go` (macOS), the distribution package or
https://go.dev/dl/.

```sh
go install github.com/svtlv/agrouter/cmd/agrouter@latest
agrouter --version
```

Upgrade with the same command. `@vX.Y.Z` pins a release.

## Coding Agent CLIs

The router runs `claude` and/or `codex`. Check `doctor.tools`:

| CLI | Needed when | Login check |
|---|---|---|
| `claude` | Claude executor, or routing may pick Claude | `claude auth status --json`; log in with `claude auth login` |
| `codex` | Codex executor, external Codex review, or routing may pick Codex | `codex login status` |

When one CLI is not installed or not wanted, disable it for routing in the
local router config (`[cli.codex] enabled = false`), so the router never
picks it. Installing the CLIs themselves is outside this skill; give the
official install link and continue.

## Verifying

| Check | Command | Expect |
|---|---|---|
| Runner | `ralphex.windows --version` | `ralphex vX.Y.Z-windows.N` |
| Router | `agrouter --version` | `vX.Y.Z` |
| Router key and routing (Unix) | `agrouter --cli claude -p "rename one local variable" < /dev/null` | one JSON line with `cli`, `model`, `effort` |
| Router key and routing (PowerShell) | `'' \| agrouter --cli codex -p "rename one local variable"` (pick the usable CLI) | one JSON line with `cli`, `model`, `effort` |

The routing check makes one paid classifier request; say so before running
it. Exit `2` with a config message means a config error; an authentication
error means a wrong key.
