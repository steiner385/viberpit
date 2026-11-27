#!/usr/bin/env bash
# Manual drill (spec 002 Task 17, Principle III): does the REAL claude binary resume a
# conversation by id from a different cwd, out of a config dir that merely contains the
# transcript? Findings: specs/002-session-continuity/research.md. Not run by CI.
#
# No model is called and no real account dir is touched: ANTHROPIC_BASE_URL points at a
# local stub that records each /v1/messages request body and answers 400, so "resumed"
# is proven by the stub seeing the prior conversation (more than one message) in the
# first request of the resume run.
#
# Usage:
#   scripts/verify-cross-host-resume.sh                 # both halves on this host
#   scripts/verify-cross-host-resume.sh export          # host A: create + one turn; prints a tarball path
#   scripts/verify-cross-host-resume.sh import <tar>    # host B: unpack into a fresh dir, resume from another cwd
#
# Requires: claude on PATH, python3, git. Set CLAUDE_BIN to test a specific binary.
set -euo pipefail

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
PORT="${STUB_PORT:-18791}"
WORK="$(mktemp -d /tmp/xhost-resume.XXXXXX)"
STUB_LOG="$WORK/stub.jsonl"

stub_start() {
  python3 - "$PORT" "$STUB_LOG" <<'PY' &
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, log = int(sys.argv[1]), sys.argv[2]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            msgs = json.loads(body).get("messages", [])
        except Exception:
            msgs = []
        with open(log, "a") as f:
            f.write(json.dumps({"path": self.path, "n": len(msgs)}) + "\n")
        self.send_response(400); self.end_headers(); self.wfile.write(b'{"error":"stub"}')
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
  STUB_PID=$!
  sleep 0.5
}

stub_stop() { kill "${STUB_PID:-0}" 2>/dev/null || true; }
trap stub_stop EXIT

run_claude() {  # run_claude <cfg_dir> <cwd> <args…>
  local cfg="$1" cwd="$2"; shift 2
  ( cd "$cwd" && env -i PATH="$PATH" HOME="$WORK/home" CLAUDE_CONFIG_DIR="$cfg" \
      ANTHROPIC_API_KEY=sk-ant-stub ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" \
      "$CLAUDE_BIN" "$@" >/dev/null 2>&1 ) || true
}

export_half() {
  mkdir -p "$WORK/home" "$WORK/cfg-a" "$WORK/repo"
  git -C "$WORK/repo" init -q && git -C "$WORK/repo" commit -q --allow-empty -m init
  git -C "$WORK/repo" worktree add -q "$WORK/wt-a" -b drill
  echo '{"hasCompletedOnboarding":true}' > "$WORK/cfg-a/.claude.json"
  stub_start
  : > "$STUB_LOG"
  run_claude "$WORK/cfg-a" "$WORK/wt-a" -p "The codeword is pelican. Say OK." --output-format json
  stub_stop
  local sid
  sid="$(ls "$WORK/cfg-a/projects/"*/*.jsonl 2>/dev/null | head -1 | xargs -r basename | sed 's/\.jsonl$//')"
  [ -n "$sid" ] || { echo "FAIL: no transcript written under $WORK/cfg-a/projects" >&2; exit 1; }
  echo "$sid" > "$WORK/cfg-a/.drill-sid"
  tar -C "$WORK" -czf "$WORK/export.tgz" cfg-a repo
  echo "export: session $sid, bundle $WORK/export.tgz"
}

import_half() {  # import_half <tar>
  local tar="$1" dst
  dst="$(mktemp -d /tmp/xhost-resume-b.XXXXXX)"
  mkdir -p "$dst/home"
  tar -C "$dst" -xzf "$tar"
  local sid; sid="$(cat "$dst/cfg-a/.drill-sid")"
  # The worktree lives at a DIFFERENT path here, as a recreated tree would.
  git -C "$dst/repo" worktree prune
  git -C "$dst/repo" worktree add -q "$dst/wt-b-elsewhere" drill
  WORK="$dst"; STUB_LOG="$dst/stub.jsonl"
  stub_start
  : > "$STUB_LOG"
  run_claude "$dst/cfg-a" "$dst/wt-b-elsewhere" --resume "$sid" -p "What was the codeword?" --output-format json
  stub_stop
  local n; n="$(python3 -c 'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1])]; print(max([r["n"] for r in rows] or [0]))' "$STUB_LOG")"
  if [ "${n:-0}" -gt 1 ]; then
    echo "PASS: resume of $sid from a different cwd sent $n prior messages (transcript honoured)"
  else
    echo "FAIL: resume of $sid sent $n message(s) — the prior conversation was not loaded" >&2
    exit 1
  fi
  echo "artifacts: $dst (delete when done)"
}

case "${1:-both}" in
  export) export_half ;;
  import) import_half "${2:?tarball}" ;;
  both)   export_half; import_half "$WORK/export.tgz" ;;
  *) echo "usage: $0 [export | import <tar> | both]" >&2; exit 2 ;;
esac
