## Windows VM (this session targets Windows)

A Windows 11 VM is reachable as `ssh win11`. User `viber` is logged on to the desktop; the default ssh shell is PowerShell. Python 3.12 (`python`) and Git for Windows are installed.

- Run `winsync [--dest WINPATH] [path]` first, every time you change code: it mirrors the worktree (tracked plus untracked, not-ignored files) into the VM and prints the Windows path, `C:\work\<worktree dir>` by default.
- Run commands with `winrun [--cwd WINPATH] [--timeout S] -- <command...>`, for example `winrun --cwd C:\work\<dir> -- python -m pytest -q`. It runs inside the logged-on interactive desktop (session 2), relays stdout/stderr, and exits with the command's exit code. `--timeout` defaults to 3600 s; on expiry the command is killed and winrun exits 124.
- The command is a program plus arguments. For PowerShell (pipes, cmdlets), add `--ps`: `winrun --ps -- 'Get-Process | Select-Object -First 3'`.
- UI automation works ONLY through `winrun`. A plain `ssh win11 <cmd>` runs in a non-interactive session where GetForegroundWindow is 0.
- `winrun` serializes desktop use with a host lock. "desktop busy" (exit 75) means another session held it past `--lock-wait` (default 900 s): wait and retry, do not fight it. Exit 255 means the VM is unreachable.
- Leave the desktop clean: close every window and app you opened.
- Never read `~/.config/viberpit/win11-vm.secret` or `~/.ssh/win11_ed25519`.
- The PR's Windows CI (GitHub Actions `windows-latest`) stays the authority. Use the VM for fast iteration and for what CI cannot show (a real desktop).
