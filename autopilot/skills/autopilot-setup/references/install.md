# Installing and Upgrading the Tools

Both tools come from the user's own forks:

| Tool | Repository | Command name | Release tags |
|---|---|---|---|
| Plan runner | `github.com/svtlv/ralphex` (branch `windows`) | `ralphex.windows` (`ralphex.windows.exe` on Windows) | `vX.Y.Z-windows.N` |
| Model router | `github.com/svtlv/agrouter` | `agrouter` | `vX.Y.Z` |

Both tools ship ready-made release binaries. Install them only the way the
operating system expects: Homebrew on macOS, a `.deb` or `.rpm` package on
Linux, and the PowerShell installer on Windows. Never use `go install`, and
never ask the user to install Go.

`doctor --json --latest` reports the installed and the newest versions;
`doctor --json --updates` adds the `updates` list the other skills offer at
their start. Upgrade when the installed version is older or missing. Show
the command and get a yes before running any installer.

## Choosing the Method

| `via` of the copy first on `PATH` | Action |
|---|---|
| `brew`, `package`, `script` | Upgrade the same way (tables below). |
| `go` (an old `go install` copy in `~/go/bin`) | Install the OS way for `doctor.os`, then offer to remove the Go copy ("Replacing a Go Copy"). |
| null (unknown place) | Ask the user how they installed it. Offer the OS way for a new install. |
| not installed | Install the OS way for `doctor.os`. |

The OS way for a new install:

| `doctor.os` | Method | Condition |
|---|---|---|
| `darwin` | Homebrew cask | `tools.brew` is true |
| `darwin` | Installer script (`~/.local/bin`) | no Homebrew; offer to install Homebrew first (https://brew.sh) |
| `linux` | `.deb` with `apt` | `tools.apt` is true |
| `linux` | `.rpm` with `dnf` | `tools.dnf` is true |
| `linux` | Installer script (`~/.local/bin`) | no `apt` or `dnf`, or no `sudo` |
| `win32` | PowerShell installer | always |

## Plan Runner (ralphex.windows)

| Platform | Install | Upgrade |
|---|---|---|
| macOS (Homebrew) | `brew tap svtlv/ralphex https://github.com/svtlv/ralphex.git` then `brew install --cask ralphex.windows` | `brew update && brew upgrade --cask ralphex.windows` |
| Linux (Debian, Ubuntu) | `.deb` from the release, then `sudo apt install ./<file>.deb` ("Linux Packages") | install the newer package the same way |
| Linux (Fedora, RHEL) | `.rpm` from the release, then `sudo dnf install ./<file>.rpm` | install the newer package the same way |
| Linux / macOS without a package manager | `curl -fsSL https://raw.githubusercontent.com/svtlv/ralphex/windows/scripts/install.sh \| sh` (installs to `~/.local/bin`; `RALPHEX_VERSION=<tag>` pins) | rerun the installer |
| Windows (PowerShell) | `irm https://raw.githubusercontent.com/svtlv/ralphex/windows/scripts/install-windows.ps1 \| iex` (installs to `%LOCALAPPDATA%\Programs\ralphex`) | rerun the installer |

Notes:

- The Linux package and the Linux installer script install the command as
  `ralphex`, not `ralphex.windows`. The driver accepts both names. When
  the upstream `ralphex` is also installed, the two conflict: name the
  conflict and ask which to keep.
- A Homebrew cask that installed a plain `ralphex` command needs
  `brew update && brew reinstall --cask ralphex.windows`.
- `fzf` is optional. The autopilot driver works without it.

## Model Router (agrouter)

| Platform | Install | Upgrade |
|---|---|---|
| macOS (Homebrew) | `brew tap svtlv/agrouter https://github.com/svtlv/agrouter.git` then `brew install --cask agrouter` | `brew update && brew upgrade --cask agrouter` |
| Linux (Debian, Ubuntu) | `.deb` from the release, then `sudo apt install ./<file>.deb` ("Linux Packages") | install the newer package the same way |
| Linux (Fedora, RHEL) | `.rpm` from the release, then `sudo dnf install ./<file>.rpm` | install the newer package the same way |
| Linux / macOS without a package manager | `curl -fsSL https://raw.githubusercontent.com/svtlv/agrouter/master/scripts/install.sh \| sh` (installs to `~/.local/bin`; `AGROUTER_VERSION=<tag>` pins) | rerun the installer |
| Windows (PowerShell) | `irm https://raw.githubusercontent.com/svtlv/agrouter/master/scripts/install-windows.ps1 \| iex` (installs to `%LOCALAPPDATA%\Programs\agrouter`) | rerun the installer |

## Linux Packages

Take the tag from `doctor.latest` and the architecture from
`dpkg --print-architecture` (Debian) or `uname -m` (`x86_64` = `amd64`,
`aarch64` = `arm64`). Download into the state directory, then install:

| Tool | Package file (`<v>` = tag without the leading `v`) |
|---|---|
| ralphex.windows | `ralphex_<v>_linux_<arch>.deb` / `.rpm` |
| agrouter | `agrouter_<v>_linux_<arch>.deb` / `.rpm` |

```sh
# example: agrouter v1.0.2 on Debian amd64
gh release download v1.0.2 -R svtlv/agrouter -p 'agrouter_1.0.2_linux_amd64.deb' -D "$dir"
sudo apt install "$dir/agrouter_1.0.2_linux_amd64.deb"
```

Without `gh`, use `curl -fsSLO https://github.com/svtlv/<repo>/releases/download/<tag>/<file>`.
The packages install `/usr/bin/<command>` (`via` = `package`). `sudo` asks
for a password: run the install in the user's terminal when the host cannot
answer it, then rerun `doctor`.

## Replacing a Go Copy

A copy from an old `go install` stays in `~/go/bin` (or `GOBIN`). When
that folder comes first on `PATH`, it hides the new install. After the
OS install:

1. Rerun `doctor --json --updates` and name the Go copy (`cli.path`,
   `router.path`, or an entry in `other_copies`).
2. Offer to remove it: `rm ~/go/bin/ralphex.windows ~/go/bin/agrouter`
   (Windows: `Remove-Item "$env:USERPROFILE\go\bin\agrouter.exe"`).
   Remove only the two tool files, never the folder or Go itself.
3. Rerun `doctor` and confirm that `via` is no longer `go`.

## PATH

- `~/.local/bin` (installer script) must be on `PATH`. When `doctor` still
  cannot find the command after install, show the line to add to the shell
  profile (`export PATH="$HOME/.local/bin:$PATH"`), then a new terminal.
- The Windows installers add their folder to the user `Path`. Open a new
  terminal before the check.
- Running a remote install script is outward-facing: say what it does
  (downloads a release, verifies SHA-256, installs one binary) before asking.

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
