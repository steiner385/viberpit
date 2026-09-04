# Server-side streaming dictation — design

**Date:** 2026-09-04
**Status:** approved for implementation (user asked to "consider options and implement")
**Supersedes:** nothing — extends `docs/superpowers/plans/2026-06-22-voice-dictation.md`

## Problem

Dictation on both prompt surfaces (`DispatchBar`, `Composer`) is "very delayed". The
current engine is the browser Web Speech API (`webkitSpeechRecognition`, wrapped by
`web/src/lib/useDictation.ts`). Both real clients are Chrome (desktop Linux 152, Android
151), where that API is a thin shim over Google's cloud recognizer: in `continuous` mode
it batches audio, interim results are sparse, and finals often arrive only after a pause
or after `stop()`. Nothing in our code adds latency — the delay lives inside a black box
we cannot tune (no chunk size, no endpointing, no model choice), and audio leaves the
tailnet on every use.

## Options considered

| Option | Latency | Privacy | Cost / risk | Verdict |
|---|---|---|---|---|
| 1. Tune Web Speech (restart loop, Chrome `processLocally`) | unchanged-ish | audio still leaves | cheap, Chrome-only, Linux on-device unreliable | fallback only |
| 2. Cloud streaming STT (Deepgram, AssemblyAI) | ~300 ms | audio to a 3rd party | API key, per-minute bill, violates security-first posture | rejected |
| 3. **Self-hosted streaming Whisper on the edge** | ~0.6–1.5 s interim, ~1.5–2 s final | audio never leaves the tailnet | +436 MB site-packages, CPU burst while dictating | **chosen** |
| 4. In-browser Whisper (transformers.js / WebGPU) | poor on phones | local | large model download per device, iOS/Android WebGPU gaps | rejected |

### Spike evidence (dobby, 16 vCPU, no GPU, `faster-whisper` 1.2.1, int8, 4 threads)

Re-transcribing a growing window every second — the streaming workload — costs:

| model | load | 8 s window per tick |
|---|---|---|
| `base.en` | 6.8 s | 350–470 ms |
| `small.en` | 24 s | ~1000 ms (one 4.3 s outlier) |

`base.en` sustains a 1 s cadence with headroom; `small.en` cannot keep up on CPU. Default
to `base.en`, keep the model configurable. PyPI and HuggingFace are reachable from the
edge host; `ffmpeg` is installed (not required — the client sends raw PCM).

## Design

Server-side engine as the primary path when enabled; Web Speech remains the fallback so
nothing changes on hosts that do not opt in.

### Data flow

```
mic ─getUserMedia─▶ AudioContext(16 kHz) ─AudioWorklet─▶ Int16 PCM chunks (~100 ms)
   ─WebSocket /ws/stt (binary)─▶ DictationSession buffer ─tick 1 s─▶ VAD + whisper
   ◀─{"type":"interim"|"final","text"}─ ◀─────────────────────────────────┘
```

### Backend (`server/stt/`)

- **`engine.py`** — `SttEngine` protocol with two blocking methods:
  `transcribe(pcm: bytes) -> str` and `speech_segments(pcm: bytes) -> list[tuple[float, float]]`
  (seconds, from Silero VAD). `WhisperEngine` implements it with `faster_whisper`
  (imported lazily inside the class so `server/` imports without the extra), `int8`,
  `beam_size=1`, `condition_on_previous_text=False`, `cpu_threads` from settings. The model
  loads once on first use and stays resident (base.en ≈ 150 MB RSS). `warm()` is called
  from the lifespan in a background thread when STT is enabled so the first dictation does
  not pay the ~7 s load. A module-level `threading.Semaphore(1)` serialises inference so
  N concurrent dictations cannot fan out across all cores (the daemon shares this host).
  `is_available()` reports whether the extra is importable — the config endpoint uses it.
- **`session.py`** — `DictationSession(engine, *, endpoint_silence_s=0.7, max_window_s=15,
  sample_rate=16000)`. Pure, synchronous, tested with a fake engine. `feed(bytes)` appends
  PCM. `tick() -> list[Event]` applies the policy:
  1. no new audio since the last tick → `[]`;
  2. `segments = engine.speech_segments(buf)`; if empty and `len(buf) > 2 s` → trim the
     buffer to its last 0.5 s (silence never accumulates) → `[]`;
  3. if the last segment ends ≥ `endpoint_silence_s` before the buffer end → transcribe
     `buf[:end]`, emit `final`, drop that prefix; then fall through to step 5 for the
     remainder if it holds speech;
  4. elif buffer ≥ `max_window_s` → transcribe everything, emit `final`, clear;
  5. else transcribe the buffer and emit `interim` unless the text equals the last interim.
  `flush() -> list[Event]` transcribes whatever remains and emits a `final` (if non-empty).
  Empty/whitespace transcripts are never emitted.
- **`ws.py`** — `build_stt_ws_router(get_engine)` mounting `@router.websocket("/ws/stt")`,
  identity via `require_ws_identity` (same as `/ws` and `/ws/vnc`). Protocol:
  - client → `{"type":"start","sampleRate":16000}` then binary Int16 LE mono frames, then
    `{"type":"stop"}`;
  - server → `{"type":"ready"}` once the engine is loaded, `{"type":"interim","text"}`,
    `{"type":"final","text"}`, `{"type":"error","message"}`, and `{"type":"done"}` after
    the flush on `stop`, then close.
  A tick task runs every `stt_tick_s` (default 1.0) and awaits `asyncio.to_thread(session.tick)`;
  ticks are skipped while one is in flight. Guards: `sampleRate` must be 16000; engine
  disabled/unavailable → accept then `close(4004, "stt disabled")` (mirrors `/ws/vnc`);
  more than `stt_max_concurrent` (default 3) open dictations → `close(4005, "stt busy")`;
  a socket is closed after `stt_max_session_s` (default 600). Audio is never persisted.
- **Config** (`server/config.py`, both Settings variants): `enable_stt`
  (`CCSWITCH_ENABLE_STT`, default false), `stt_model` (`CCSWITCH_STT_MODEL`, `base.en`),
  `stt_cpu_threads` (`CCSWITCH_STT_CPU_THREADS`, 4), `stt_tick_s` (`CCSWITCH_STT_TICK_S`, 1.0),
  `stt_max_concurrent` (`CCSWITCH_STT_MAX_CONCURRENT`, 3), `stt_max_session_s`
  (`CCSWITCH_STT_MAX_SESSION_S`, 600).
- **`GET /api/config`** gains `sttEnabled: bool` = `enable_stt and WhisperEngine.is_available()`.
- **App wiring** (`server/app.py`): `app.state.stt_engine` created in `create_app` (None when
  disabled), router included next to the VNC bridge, lifespan warms it in a thread.
- **Dependencies**: new optional group `stt = ["faster-whisper>=1.2,<2"]` in `pyproject.toml`.
  The edge deploy step (`server/deploy/steps.py::sync_deps_if_changed`) installs
  `.[server,stt]`; runner sync and CI keep `[server]` (tests use a fake engine; a real-engine
  integration test is `importorskip`-gated and additionally requires `CCSWITCH_TEST_STT=1`).

### Frontend (`web/src/lib/`)

- **`pcmCapture.ts`** — `startPcmCapture(onChunk: (chunk: Int16Array) => void): Promise<PcmCapture>`.
  `getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, channelCount: 1}})`,
  `new AudioContext({sampleRate: 16000})`, an `AudioWorkletProcessor` loaded from a Blob URL
  (no build-config changes) that converts Float32 → Int16 and posts ~1600-sample chunks.
  `stop()` disconnects, closes the context, and stops every track. Throws if
  `AudioWorklet`/`getUserMedia` are missing.
- **`useServerDictation.ts`** — same `UseDictation` shape as the Web Speech hook. `start()`
  opens `new WebSocket(wsUrl('/ws/stt'))`, sends `start`, starts capture, forwards chunks as
  binary; `interim`/`final` messages → `onResult(text, isFinal)`; `stop()` stops capture,
  sends `stop`, and closes after `done` or a 3 s timeout; socket/mic errors → `error` and
  `listening=false`. `supported` = `!!navigator.mediaDevices?.getUserMedia && 'AudioWorklet' in window`.
- **`useDictationField.ts`** — calls both hooks (hooks are unconditional) and drives the one
  selected by `useConfig().data?.sttEnabled`: server when enabled and supported, else Web
  Speech. `supported` is the selected engine's. The splice/anchor logic is unchanged.
- **`ConfigInfo`** gains `sttEnabled?: boolean`. `MicButton` is unchanged.

### Security

Same identity gate as every other WebSocket; raw audio stays in process memory for at most
`max_window_s` and is never written to disk or logged. Bounded concurrency and session
length cap CPU exposure from a misbehaving client. Feature is off by default.

### Testing

- `server/tests/test_stt_session.py` — policy with a fake engine: no-op without new audio,
  interim on speech, duplicate interim suppressed, endpoint commit + buffer drop, forced
  commit at `max_window_s`, silence trimming, `flush`.
- `server/tests/test_stt_ws.py` — TestClient websocket: disabled → 4004; happy path
  start/binary/stop → `ready`, `interim`, `final`, `done`; bad `sampleRate` → error; busy → 4005.
- `server/tests/test_config_api.py` — `sttEnabled` false by default, true when flag set and
  engine importable (patched).
- `server/tests/test_stt_whisper_integration.py` — real `faster-whisper` on the JFK clip
  through `DictationSession`; skipped unless the extra is installed and `CCSWITCH_TEST_STT=1`.
- `web`: `pcmCapture.test.ts` (float→int16 conversion, stop releases tracks),
  `useServerDictation.test.tsx` (mock WebSocket: start frame, binary forwarding, message
  mapping, stop → done → close, error path), `useDictationField.test.tsx` (engine selection
  by config).

### Operations

Enable on the edge with `CCSWITCH_ENABLE_STT=true` in `/etc/ccswitch/ccswitch.env`; the first
start downloads `Systran/faster-whisper-base.en` (~150 MB) into the HuggingFace cache. Document
in `CLAUDE.md` and `deploy/README.md`.

## Out of scope

Automatic fallback from a failing server engine to Web Speech mid-session, language
selection, punctuation commands, GPU support, and idle unloading of the model.
