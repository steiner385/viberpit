---
slug: session-engine-terminal
title: Session Engine & Terminal
state: live
links:
  code:
    - server/sessions/driver.py
    - server/sessions/facade.py
    - server/sessions/events.py
    - server/sessions/status.py
    - server/sessions/completion.py
    - server/sessions/failure.py
    - server/sessions/dispatch.py
    - server/ipc/daemon.py
    - server/ipc/serve.py
    - server/ipc/drain_on_signal.py
    - server/bridge/ws.py
    - server/bridge/vnc_ws.py
    - src/ccswitch/launcher.py
    - src/ccswitch/runner.py
    - web/src/ws/client.ts
    - web/src/components/session/TranscriptView.tsx
  prs: [853, 681, 517, 363, 365, 140, 118, 737]
---

# Session Engine & Terminal

**Problem** — A native `claude` process is a local, terminal-bound thing: to run one you need a
shell on the host, and if you close that shell the session is gone. Running several accounts'
worth of sessions at once, watching them, answering their prompts, and steering them from a phone
is impossible with raw terminals. The engine has to own long-lived `claude` processes on the
server, stream each session's activity to any number of browsers, let a user dispatch prompts,
interrupt turns, and answer permission/question cards — and it must survive daemon restarts and
reconnects without losing or corrupting the live transcript.

**Target users** — Claude Max subscribers running long autonomous or coding sessions who want to
launch, watch, and steer them from a browser (including mobile) instead of a pinned terminal;
multi-account operators who keep many sessions live across accounts at once and monitor them from
one dashboard.

**Desired outcome** — Any session can be opened from any device on the tailnet and shows a
complete, correctly-ordered transcript that continues to stream live; the user can send a prompt,
interrupt a turn, answer a permission or question, and switch permission mode, with those actions
never silently dropped across a reconnect. The daemon owns every live `claude` process, so an app
deploy or `systemctl restart` drains in-flight turns rather than killing them mid-work.

**Success metrics** — (unknown — needs owner input) for user-facing SLOs. Observable in-code
signals the team already tracks: correct replay with no duplicated/blanked/truncated transcripts
across attach and reconnect (regression-tested); no interrupts/answers/permissions dropped on
reconnect (#508); in-flight turns survive a daemon SIGTERM (drain, not kill — #853); event-loop
never starves under many concurrent sessions (#737, #738).

**Non-goals** — Not a raw PTY byte relay to the browser: the web transcript is a stream of
*normalized* session events (turns, text deltas, tool calls, permission requests, status)
rendered by React, not xterm.js terminal emulation over raw bytes. Raw framebuffer streaming
exists only for the separate X11/VNC (RFB) path used by GUI-tool sessions
(`server/bridge/vnc_ws.py`), not for the main session terminal. The engine also does not fork or
patch the `claude` binary — it drives the real binary through the Claude Agent SDK (constitution
Principle I).

**Notes**
- **Daemon ownership.** In the deployed topology the IPC daemon (`server/ipc/daemon.py`,
  `serve.py`) owns every live `claude` process; the edge/web tier talks to it over a Unix-socket
  RPC surface plus a multiplexed event WebSocket. On `SIGTERM` the daemon drains in-flight turns
  before shutting down (`server/ipc/drain_on_signal.py`, up to ~3 min / ~10 min mid-compaction),
  so a bare `systemctl restart` is not a kill for live sessions (#853).
- **Driver.** `SessionDriver` (`server/sessions/driver.py`) drives one session's SDK client,
  normalizes SDK output into the events in `server/sessions/events.py`, fans them out to all
  attached subscribers, and derives dashboard status.
- **Status derivation.** `server/sessions/status.py` derives a session's dashboard status purely
  from live driver signals, by precedence (roughly): plan-approval pending → question pending →
  permission pending → condensing (auto-compaction) → retrying → turn active → complete/done →
  idle. The orchestrator overlays account-level states such as `limited` and lifecycle states
  such as `archived`/`ended`. `completion.py` owns the `<!-- ccswitch:complete -->` /
  `<!-- ccswitch:no-pr -->` completion markers; `failure.py` classifies turn failures
  (rate-limit, auth-401, crash, dropped, overloaded) to drive recovery.
- **Replay / multiplexing.** The daemon keeps a per-stream bounded replay ring (default 8192
  events) with monotonic `seq`; on attach the edge (`server/bridge/ws.py`) replays the transcript
  from disk (trimming an incomplete in-progress turn), then streams live events, healing seq gaps
  by re-syncing from disk and resetting on daemon epoch change (#363, #365, #140, #514). Multiple
  browsers can watch the same session concurrently via the subscriber fan-out (#363, #681). See
  `transcript-replay-archive` for the replay/transcript detail.
- **CLI launchers.** `src/ccswitch/launcher.py` (`ccmax`) and `runner.py` (`ccrun`) are the
  standalone single-session CLI paths that auto-rotate one session on a limit; the web engine is
  the multi-session, daemon-owned path.

**Evidence** — Daemon-drain-on-SIGTERM so a restart no longer kills live sessions (#853);
dropped-interrupt/answer/permission fix on reconnect (#508); driver interrupt crash on
disconnected client after restart (#517); live-transcript freeze after driver recompose and the
multi-subscriber fan-out / daemon-epoch recovery (#363, #365); IPC subscriber-queue auto-scaling
and control-event preservation (#681); event-replay across the attach/reconnect gap (#140);
RemoteOrchestrator/RemoteDriver shim for the daemon topology (#118); event-loop-starvation fix on
the session daemon (#737). Code: `server/sessions/driver.py`, `facade.py`, `events.py`,
`status.py`, `completion.py`, `failure.py`; `server/ipc/daemon.py`, `serve.py`,
`drain_on_signal.py`; `server/bridge/ws.py`, `vnc_ws.py`; `web/src/ws/client.ts`,
`web/src/components/session/TranscriptView.tsx`.
