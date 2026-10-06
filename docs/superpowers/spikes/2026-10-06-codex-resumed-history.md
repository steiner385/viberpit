# Codex resumed-history recovery (#2721)

## Observed boundary

On 2026-10-06, session `5d3be633-e77a-4c0b-b497-54c330fb9643` on rocky
(native thread `01a10c98-de41-7f12-b806-b2809667d1ab`, codex-cli 0.153.2)
continued after the 18:05 UTC daemon restart. The daemon's supported `ring_tail`
RPC retained 1,344 events, including six terminal turns through 18:42 UTC.
The public transcript endpoint returned only 31 turns, ending before restart.

An unmodified native app-server reading an isolated, credential-free snapshot
reproduced the missing tail through **both** `thread/read(includeTurns=true)` and
`thread/turns/list(itemsView=full)`. The thread reported `historyMode=paginated`.
The snapshot retained the original absolute runtime-home path inside a private
mount namespace; a relocated snapshot does not exercise the same native history
lookup. Native storage was copied opaquely, never queried for its schema/content,
parsed, patched, or modified in the live home. No paid model call was needed.

Small disposable native threads survived start, two interrupted turns, process
restart, resume, and two more interrupted turns correctly (1/2/3/4 turns). Thus
neither an unconditional resume failure nor changing pagination alone explains
or repairs this incident. The underlying native indexing defect remains unproven.

## Repair and acceptance

The driver's existing coalesced normalized turn buffer is saved before terminal
delivery/reset in a private ViberPit-owned `.turns.jsonl` sidecar beside the item
timestamps. Replay fills missing native turns from those snapshots, keeping one
cursor space for attach, paging, exports, and search. Native complete turns win
when they catch up; saved terminals can settle native in-progress records.

The six recovered ring turns were decoded with the production `decode_event`
codec and written through `TurnHistoryStore.record`, without editing native
history. The repaired `CodexThreadReplay` against the same native snapshot returned
**37 turns**, with the final eight starting at ordinal 29. It included the user
reply `Continue with your recommendation` and terminal turn
`01a11285-6d63-7ef0-9e16-cdb4b37d6121`, both absent before the repair.

This protects completed/explicitly interrupted turns. A hard daemon kill before
a terminal snapshot can still leave only native persistence for its active turn;
it is not a claim of crash-proof streaming storage or repair of model context.

## Reusable failure pattern

- **Symptom:** live progress disappears when a conversation is reopened.
- **Anti-pattern:** assuming a native history read contains everything already
  delivered by its live stream, and then replacing the display with that read.
- **Fix shape:** preserve completed normalized live turns durably and reconcile
  by stable native turn identity in the canonical replay adapter; never invent
  completion, duplicate caught-up turns, or parse a vendor's private storage.
- **Where to look:** driver buffer reset, terminal delivery, attach replay,
  transcript pagination, export/search, and permanent-delete sidecar cleanup.
