# Voice Dictation (browser speech-to-text) — Design

**Date:** 2026-06-22
**Feature:** Voice/mic input for ccswitch prompt surfaces
**Status:** Approved, pending implementation plan

## Summary

Add a "tap to talk" dictation affordance to ccswitch's prompt-input surfaces.
Speech is transcribed in-browser via the Web Speech API and appended into the
existing text field; the user reviews and sends as normal. No spoken replies,
no wake word, no server-side transcription.

## Goals

- Let the user dictate prompts instead of typing them, on both the new-session
  launcher (`DispatchBar`) and the per-session prompt box (`Composer`).
- Zero new backend, zero new dependencies, no audio leaves the device.
- Degrade gracefully where the Web Speech API is unavailable (e.g. iOS Safari):
  the mic affordance simply does not appear.

## Non-Goals (YAGNI)

- Text-to-speech / spoken replies (full-duplex voice).
- Wake-word / always-on listening.
- Server-side or local Whisper transcription.
- Language picker (hardcode `en-US` for v1).
- Audio recording/storage of any kind.

## Decisions (from brainstorming)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Scope | Dictation only (input) | Smallest useful slice; output TTS is a separate, larger feature. |
| STT engine | Browser Web Speech API | No backend, free, audio stays on device. Accepts unreliability on iOS Safari, handled via graceful hiding. |
| Surfaces | Both `DispatchBar` and `Composer` | Consistent UX via one shared hook; no duplication. |
| Insertion | Append at cursor | Mix typing and talking; dictate additions to a half-written prompt. |
| Auto-send | No | Mis-transcription is common for technical prompts; user reviews then presses Enter. |
| Language | `en-US` hardcoded | YAGNI; promote to a setting later if needed. |

## Architecture

Three small, independently-testable units.

### 1. `web/src/lib/useDictation.ts`

React hook wrapping `window.SpeechRecognition ?? window.webkitSpeechRecognition`.
Isolates all browser-quirky speech code. Knows nothing about prompts, WebSocket,
or UI.

Interface:

```ts
interface UseDictationOptions {
  onResult: (text: string, isFinal: boolean) => void
  lang?: string // default 'en-US'
}

interface UseDictation {
  supported: boolean
  listening: boolean
  error: string | null
  start(): void
  stop(): void
  toggle(): void
}
```

Behavior:
- `supported` is `false` when neither global exists → consumers hide the button.
- Continuous recognition with interim results enabled; emits interim results
  (`isFinal:false`) live and the finalized text (`isFinal:true`).
- `start()`/`stop()`/`toggle()` manage the recognition lifecycle and `listening`.
- All recognition errors (permission-denied, no-speech, network) set `error`,
  stop listening, and never throw.

### 2. `web/src/components/session/MicButton.tsx`

Presentational button. Props: `{ listening, supported, error, onToggle }`.
- Renders **nothing** when `!supported`.
- Idle / listening (pulsing/red) / error visual states.
- Surfaces `error` via tooltip/title.

### 3. Integration in `DispatchBar.tsx` and `Composer.tsx`

Each surface:
- Calls `useDictation` with an `onResult` that appends transcript at the current
  cursor position in its existing textarea state (interim text replaced by final).
- Renders `<MicButton />` alongside its existing controls.

## Data Flow

```
mic tap → useDictation.start()
        → SpeechRecognition fires results
        → onResult(text, isFinal)
        → surface appends/updates text at cursor in existing textarea state
        → user reviews → Enter → existing prompt() WebSocket path (unchanged)
```

The session engine is untouched: voice merely produces the `text` string that
already flows into the existing `prompt()` call (`web/src/ws/client.ts:139`).

## Error Handling

- Unsupported browser: `supported:false`, button absent. No errors surfaced.
- Mic permission denied / no-speech / network: `error` set, `listening:false`,
  button shows error state with explanatory tooltip. Recoverable by tapping again.

## Testing

- **`useDictation`** — mock `SpeechRecognition` global: start/stop transitions,
  interim vs final results, error paths, unsupported → `supported:false`.
- **`MicButton`** — correct state rendering, hidden when unsupported, fires
  `onToggle`.
- **Integration** — in `DispatchBar`/`Composer` tests, mock the hook and assert a
  result appends to the textarea at the cursor and does not auto-send.

## Risks / Notes

- Web Speech API on Chrome routes audio to Google's servers for recognition;
  this is the browser's behavior, not ccswitch's, but worth noting in UX copy if
  privacy-sensitive. Audio never touches the ccswitch server.
- iOS Safari support is partial/absent; graceful hiding is the mitigation.

### Known v1 limitations (accepted)

- **Typing while dictating can desync the insertion anchor.** The caret anchor
  is captured when dictation starts and is not updated by user keystrokes or
  caret moves made while listening. The intended flow is tap → talk → tap →
  then edit. Editing mid-utterance may splice the next transcript at the wrong
  offset. A future hardening could re-anchor on textarea `input`/`select` while
  listening.
- **Stopping mid-interim can leave un-finalized text.** If recognition ends
  while an interim (not-yet-final) chunk is shown, that text stays in the box as
  ordinary editable text. Chrome usually emits a final result on stop, so this
  is rare; because nothing auto-sends, the worst case is a stray word the user
  edits before sending.
- **Error visibility on touch devices.** Recognition errors surface via the
  button's `title` (hover) plus an amber border; touch devices have no hover, so
  a denied-permission error is subtle. Acceptable for v1; a visible hint / live
  region is a future improvement.
