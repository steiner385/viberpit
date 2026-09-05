# Golden Codex notification transcripts

Hand-written `codex app-server` server-notification streams, one JSON object per
line as `{"method": …, "params": …}`. `server/tests/test_codex_translator.py`
feeds each line through `server/codex/schema.py:parse_server_notification` and
then `server/codex/translator.py:CodexEventTranslator.translate`, and asserts on
the whole normalized event list — so a fixture line that does not match the
pinned schema fails the test rather than being silently skipped.

The content is **invented**. Nothing here was captured from a real account, a
real repository, or a real model response, and nothing here may be: these files
are committed, and a transcript carries prompts, commands, paths and model
output by construction.

* `ordinary_turn.jsonl` — a normal turn: reasoning summary, streamed assistant
  text, a command execution with output deltas, and a file change.
* `interrupted_turn.jsonl` — a turn interrupted mid-command, with two unmodelled
  item types mixed in and an assistant message delivered only as a final item.
