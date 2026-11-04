## Windows VM (this session targets Windows)

A Windows 11 VM is reachable as `ssh win11`. User `viber` is logged on to the desktop; the default ssh shell is PowerShell. Python 3.12 (`python`) and Git for Windows are installed.

- Run `winsync [path]` first, every time you change code: it mirrors the worktree into the VM and prints the Windows path.
- Run commands with `winrun [--cwd WINPATH] [--timeout S] -- <command...>`. It runs inside the logged-on interactive desktop (session 2) and returns the exit code and output.
- UI automation works ONLY through `winrun`. A plain `ssh win11 <cmd>` runs in a non-interactive session where GetForegroundWindow is 0.
- `winrun` serializes desktop use with a host lock. "desktop busy" (exit 75) means another session holds it: wait and retry, do not fight it.
- Leave the desktop clean: close every window and app you opened.
- Never read `~/.config/viberpit/win11-vm.secret` or `~/.ssh/win11_ed25519`.
- The PR's Windows CI (GitHub Actions `windows-latest`) stays the authority. Use the VM for fast iteration and for what CI cannot show (a real desktop).
