# Keyboard Answering for Permission & Question Cards — Design

**Date:** 2026-06-22
**Status:** Approved (brainstorming complete; user delegated completion)
**Feature area:** `web/` frontend — in-session permission/question cards
**Origin:** Part C audit P1-a (`docs/superpowers/specs/2026-06-22-keyboard-parity-audit.md`)

## Goal

Let a keyboard user answer the in-session permission and question cards as fast as in Claude
Code — without reaching for the mouse. These cards are the only place the agent *blocks on the
user*, so keyboard answering is core to a hands-on-keyboard flow.

User decisions (brainstorming):
- **Key scheme:** numbers + arrows + Enter.
- **Scope:** card-focused (keys active when the card has focus; the card auto-focuses when it
  appears — the pattern `PermissionCard` already uses).
- **Multi-select `Enter`:** Submit the current selection (not toggle).

## Current state (verified against `origin/master`)

- `PermissionCard.tsx` — interactive in `Turn.tsx:74`. Auto-focuses its primary button
  (`primaryRef`). Buttons in order: allow / deny / (always, when `mode !== 'interactive'`); plan
  variant: allow (`Approve & run`) / deny (`Keep planning`). No number/arrow keys today.
- `QuestionCard.tsx` — the interactive instance is rendered only in the pinned
  `PendingQuestionPanel`; transcript copies render `readOnly` (`Turn.tsx:79`). So exactly one
  interactive question exists at a time. Modes: `readOnly`, `armedForChat` (banner), single-select
  (radiogroup of buttons), multi-select (checkboxes + Submit), plus escape hatches (Write a
  response / Chat about this). No autofocus, no arrow/number handling; `role="radio"` buttons are
  static (`aria-checked={false}`).

## Architecture — one shared hook

All option-list key behavior lives in a single new hook so neither card grows a private copy
(constitution VI, single source of truth):

**`web/src/components/session/useCardKeyboard.ts`**

```ts
interface CardKeyboardOptions {
  count: number
  multiSelect?: boolean
  onActivate: (index: number) => void   // single-select: choose option `index`
  onToggle?: (index: number) => void    // multi-select: toggle option `index`
  onSubmit?: () => void                  // multi-select: submit current selection
}
interface CardKeyboardApi {
  activeIndex: number
  containerProps: { onKeyDown: (e: React.KeyboardEvent) => void }
  itemProps: (index: number) => { ref: (el: HTMLElement | null) => void; tabIndex: number }
}
export function useCardKeyboard(opts: CardKeyboardOptions): CardKeyboardApi
```

Behavior:
- Maintains a roving highlight `activeIndex` (starts at 0). `itemProps(i)` gives each option
  `tabIndex = i === activeIndex ? 0 : -1` and a ref the hook focuses when `activeIndex` changes.
- On mount, focuses item 0 (replaces `PermissionCard`'s ad-hoc `primaryRef` autofocus).
- `containerProps.onKeyDown`:
  - **`↑`/`↓`** — move `activeIndex` with wraparound, focus the new item. `preventDefault` +
    `stopPropagation`.
  - **`1`–`9`** — let `n = digit - 1`; if `n < count`, `multiSelect ? onToggle(n) : onActivate(n)`.
    Out-of-range digits are ignored. `preventDefault` + `stopPropagation`.
  - **`Enter`** — `multiSelect ? onSubmit() : onActivate(activeIndex)`. `preventDefault` +
    `stopPropagation`.
  - **`Space`** — `multiSelect ? onToggle(activeIndex) : onActivate(activeIndex)`.
  - **Bail** (return without handling, so the event behaves natively) when
    `e.target` is an `INPUT` or `TEXTAREA` — preserves typing in the multi-select checkboxes and
    the freeform "Write a response" textarea.

`stopPropagation` on handled keys keeps them from bubbling to SessionView's window-level
`Esc`/`ArrowLeft`/`Shift+Tab` handlers.

## Wiring

### PermissionCard
- Build an ordered `actions` array matching the rendered buttons: plan variant `['allow','deny']`;
  normal `['allow','deny']` plus `'always'` when `mode !== 'interactive'`.
- `const kb = useCardKeyboard({ count: actions.length, onActivate: i => onDecision(block.permId, actions[i]) })`.
- Spread `kb.containerProps` on the button-row container; spread `kb.itemProps(i)` on each button
  (replacing the `primaryRef`/`useEffect` autofocus, which the hook now owns).

### QuestionCard
- **single-select:** `useCardKeyboard({ count: options.length, onActivate: i => onAnswer(block.questionId, [optionLabel(options[i])]) })`; spread container/item props onto the radiogroup and each option button.
- **multi-select:** convert each option from a `<label><input type="checkbox">` to a focusable
  `<button role="checkbox" aria-checked={checked.has(label)}>` with a presentational check glyph
  (mirrors the single-select `role="radio"` button pattern), so the option list contains **no
  native `<input>`** and every key flows through the hook.
  `useCardKeyboard({ count: options.length, multiSelect: true, onToggle: i => toggleChecked(optionLabel(options[i])), onSubmit: () => onAnswer(block.questionId, Array.from(checked)) })`;
  `itemProps(i)` spreads onto each option button. A separate Submit button remains (Enter also
  submits). The freeform "Write a response" `<textarea>` is then the only INPUT/TEXTAREA in the
  card, which the hook's bail guard protects.
- **`readOnly` and `armedForChat`:** unchanged — the hook is **not** called, so no key handling and
  no focus stealing.
- Escape hatches (Write a response / Chat about this) stay Tab-reachable but are **not** part of the
  number/arrow scheme (they are not answer options).

## Key behavior summary

| Key | Single-select (Permission, Question radio) | Multi-select (Question checkboxes) |
|---|---|---|
| `↑`/`↓` | move highlight (wraps) | move highlight (wraps) |
| `1`–`9` | choose option N | toggle option N |
| `Enter` | activate highlighted option | Submit current selection |
| `Space` | activate highlighted option | toggle highlighted option |

PermissionCard digit map: `1`=allow, `2`=deny, `3`=always (when shown).

## Testing (vitest + @testing-library/react + user-event)

**`useCardKeyboard` unit tests:** roving index wraps at both ends; digit > count ignored; digit
within range calls the right callback; multi-select Enter calls `onSubmit`, single-select Enter
calls `onActivate(activeIndex)`; INPUT/TEXTAREA target bails.

**PermissionCard:** mounts with allow focused; `1`→`onDecision(allow)`, `2`→`onDecision(deny)`,
`3`→`onDecision(always)` only when the always button is shown (and `3` ignored when it isn't);
`↓` then `Enter` activates deny; plan variant maps `1`/`2` to allow/deny.

**QuestionCard single-select:** mounts with first option focused; `2`→`onAnswer([secondLabel])`;
`↓`/`↑` move; `Enter` answers the highlighted option.

**QuestionCard multi-select:** options render as `role="checkbox"` buttons with `aria-checked`
reflecting state; `1` then `3` toggle options 0 and 2 (`aria-checked` flips); `Space` toggles
highlighted; `Enter`→`onAnswer(checkedArray)`; opening "Write a response" and typing a digit into
the freeform textarea inserts text and does not toggle.

**Modes:** `readOnly` and `armedForChat` render no roving tabindex and do not move focus.

All of `vitest run`, `tsc --noEmit`, `eslint` green.

## Out of scope

- Global-while-pending key routing (card-focused only).
- Letter mnemonics (`y`/`n`/`a`).
- Keyboarding the escape-hatch buttons (Write / Chat).
- Other audit items (`@` mentions, Esc-to-interrupt, double-Esc rewind) — separate cycles.

## Risks

- **Focus stealing:** auto-focusing the question card when it appears mirrors `PermissionCard`'s
  existing behavior; consistent, and the pinned panel appearing is a deliberate "answer me" moment.
- **Event bubbling:** handled keys must `stopPropagation` so arrows/Enter don't reach SessionView's
  detach/mode handlers — covered by tests asserting the callbacks fire and (where feasible) that no
  detach occurs.
- **Multi-select option model change:** options become `role="checkbox"` buttons (no native
  `<input>`), so Space/digit toggles run only through the hook — no native/hook double-toggle. The
  visual checked state comes from `aria-checked` + a glyph. Tests pin toggle + submit behavior and
  the freeform-textarea bail.
