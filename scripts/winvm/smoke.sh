#!/usr/bin/env bash
# Live smoke for winsync/winrun against the real Windows VM. Run by hand on the runner
# (rocky); never in CI. Uses the copies in this checkout, not the installed ones.
#   bash scripts/winvm/smoke.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
winrun=("python3" "$here/winrun.py")
winsync=("python3" "$here/winsync.py")
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

dest="$("${winsync[@]}" "$here" | tail -n1)"
echo "synced to $dest"

out="$("${winrun[@]}" --cwd "$dest" --timeout 120 -- python -c "import ctypes, os; print('fg', ctypes.windll.user32.GetForegroundWindow() != 0, os.path.isfile(r'scripts\winvm\winrun.py'))")"
[[ "$out" == *"fg True True"* ]] || fail "expected an interactive desktop and the synced tree, got: $out"

set +e
"${winrun[@]}" --timeout 60 -- python -c "import sys; print('to stderr', file=sys.stderr); sys.exit(7)" 2>/dev/null
rc=$?
"${winrun[@]}" --timeout 3 -- python -c "import time; time.sleep(60)"
rc_timeout=$?
set -e
[[ $rc -eq 7 ]] || fail "exit code not relayed (got $rc)"
[[ $rc_timeout -eq 124 ]] || fail "timeout did not exit 124 (got $rc_timeout)"

# shellcheck disable=SC2016  # $_ is PowerShell's, not bash's
ps_out="$("${winrun[@]}" --ps --timeout 60 -- '"a b" | ForEach-Object { $_.ToUpper() }')"
[[ "$ps_out" == *"A B"* ]] || fail "--ps mode: got $ps_out"

left="$(ssh -o BatchMode=yes "${VIBERPIT_WINDOWS_VM_SSH_HOST:-win11}" "(Get-ScheduledTask -TaskPath '\\viberpit\\' -ErrorAction SilentlyContinue | Measure-Object).Count")"
[[ "${left//[$'\r\n ']/}" == "0" ]] || fail "winrun left $left scheduled task(s) behind"
echo "SMOKE OK"
