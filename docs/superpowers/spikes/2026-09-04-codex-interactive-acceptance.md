# Codex interactive-v1 acceptance record

**Date opened:** 2026-09-05

**Status:** **Automated gates recorded. Real-boundary acceptance NOT YET RUN — the
v1 flags stay false.**

This is the record Task 6.2 requires. It has two halves and they are deliberately
not merged: the part a CI-equivalent run can establish, and the part that can only
be established against a real ChatGPT account on the real fleet. The second half
is the launch bar. Until every one of its rows is filled in with a real result,
`CCSWITCH_CODEX_ENABLED` and `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED` remain
`false` in the private deployment, and `CCSWITCH_CODEX_AUTOMATIONS_ENABLED`
remains `false` regardless (that is Phase 7).

**Redaction contract for this document.** It may name binary/schema versions and
runner hosts. It must never contain an account id, a token, a prompt, a command, a
patch, or a raw native thread id. Every row below is a verdict plus a version or a
host — nothing else.

---

## Boundary under test

| | |
|---|---|
| Pinned CLI | `codex-cli 0.153.2` (`server/codex/schema.py:PINNED_CLI_VERSION`) |
| Pinned contract | `cb145bc3…35c3` (`PINNED_CONTRACT_SHA256`) |
| Contract fixtures | `server/tests/fixtures/codex_app_server/0.153.2/` |
| Runner hosts | dobby (primary), rocky (worker) |
| Authority | `ccswitch-codex-auth.service` on dobby — no failover in v1 |
| Prior real-boundary evidence | `2026-09-04-codex-external-auth-findings.md` (Phase 0.2 two-runner gate) |

---

## Part 1 — Automated gates

Recorded on 2026-09-05 against this branch. These establish application behaviour;
per the plan's own framing they are **not** app-server truth, which is what Part 2
is for.

| Gate | Command | Result |
|---|---|---|
| Lint | `ruff check src/ server/` | **PASS** — all checks passed |
| Types | `mypy server/ --ignore-missing-imports` | **PASS** — 1091 files, no issues |
| Core suite | `PYTHONPATH=$PWD/src pytest tests/` | see the PR's verification section |
| Server suite | `pytest server/tests/ -n auto --dist loadfile --timeout=300` | see the PR's verification section |
| Codex + metrics + quota subset | `pytest server/tests/ -k "codex or metrics or assistant or quota"` | **PASS** — 1239 passed, 3 skipped |
| Redaction/audit subset | `pytest server/tests/ -k "redact or audit or codex or log"` | **PASS** |
| Doctor | `pytest server/tests/test_codex_doctor.py` | **PASS** — 51 passed |
| Secret scanning | `pytest server/tests/test_codex_secret_scanning.py` | **PASS** — 19 passed |
| Install script | `bash -n` + `shellcheck` + all five paths driven by hand | **PASS** (see below) |
| Unit files | `systemd-analyze verify deploy/ccswitch-daemon{,-remote}.service` | **PASS** |
| Frontend gate | `npm run typecheck && lint && test && build` | **N/A** — this change touches no `web/` file |
| Playwright | `npm run test:e2e` | **N/A** — no UI change in this PR (Phase 5 covered the Codex UI) |
| Postgres suite | `CCSWITCH_TEST_PG_URL=… pytest server/tests/test_postgres_compat.py` | **N/A** — no model or column added |
| Prompt evals | `python scripts/run_evals.py` | **N/A** — no prompt or governance rule changed |

`fetch-codex.sh` was exercised through all five branches with a stand-in binary:
no recorded digest → refused; `--record` → digest written; install → succeeded;
wrong `--version` → refused; tampered bytes → refused.

### Two findings from the secret-scanning sweep, both fixed here

The sweep was not a formality — it found two live gaps, both of the same shape
(*a credential pattern owned by the Codex boundary, on a surface no Codex module
sits on*):

1. **Bare `sk-` OpenAI keys survived `audit.redact_detail` and `RedactingFilter`.**
   `server/audit.py` covered `sk-ant-` only; the bare shape lived in
   `server/codex/redaction.py`. Audit rows outlive logs and no Codex module is on
   that path, so the pattern could never have protected them. Moved upstream.
2. **Short JWTs had the same problem** — the shared itsdangerous rule needs
   16/6/20-character segments, and an OpenAI access token can be shorter. Moved
   upstream too.

Consequently `server/codex/redaction.py` now owns **no** shape patterns and
delegates entirely; `test_the_shared_redactor_owns_every_openai_shape` pins both
as regressions.

A third, narrower finding: `/info` carried `cliVersion` — raw `codex --version`
stdout — verbatim, and `/info` is polled every 30 s per host and persisted on the
host row. It is now shape-scrubbed and length-capped at the probe.

---

## Part 2 — Real-boundary acceptance (the launch bar)

**None of these rows has been run.** Fill each in with a date and a verdict.

Run the scripted portion on **each** runner host:

```bash
CCSWITCH_TEST_CODEX_INTERACTIVE=1 \
python scripts/smoke_codex_interactive.py \
    --account <smoke-account> --repo <owner/name> [--approvals]
```

It refuses to start without that variable *and* an explicit `--account`/`--repo`:
it spends real quota, so there is no default. Prompts are trivial and tool-free so
a pass costs a handful of tokens and a failure is attributable to the transport
rather than to model behaviour.

### Scripted

| # | Check | dobby | rocky |
|---|---|---|---|
| 1 | `python -m server.codex.doctor` reports no failing check | | |
| 2 | No `auth.json` under any runner `CODEX_HOME` | | |
| 3 | Account reads back as `openai` with a quota snapshot | | |
| 4 | Model list returned from the account's own app-server | | |
| 5 | Two sessions on ONE account (simultaneous managers → one app-server) | | |
| 6 | Model + reasoning effort applied at creation | | |
| 7 | One minimal tool-free turn reaches `turnEnd` | | |
| 8 | Steer mid-turn takes effect | | |
| 9 | Interrupt ends an in-flight turn | | |
| 10 | Browser reconnect replays via `thread/read` | | |
| 11 | Quota refresh advances the snapshot | | |
| 12 | Archive both sessions | | |
| 13 | No secret-shaped value in anything the run gathered | | |

### Manual

Deliberately not scripted — a script that kills production processes is worse
than a documented manual step, and the daemon owns every live `claude` session.

| # | Check | How | Result |
|---|---|---|---|
| 14 | Command-approval **decline** | `--approvals`, or drive it from the browser | |
| 15 | Manager crash/restart recovery | `pkill -f 'codex app-server'` on the runner; expect a `RunnerNotice` CRASHED then RESTARTED, turns reported INDETERMINATE (never success), and `codex_runner_restarts_total{outcome=restarted}` to advance | |
| 16 | Daemon drain / rehydrate | `systemctl restart ccswitch-daemon.service` with an idle Codex session attached; expect the drain to block, then the session to rehydrate | |
| 17 | 401-storm refresh coalescing | revoke at the authority with both runners live; expect `codex_auth_generation{account}` to advance **once**, not once per runner | |
| 18 | Claude and Copilot stay usable during a Codex crash / auth failure / version mismatch | run one of each concurrently while forcing each Codex failure mode | |
| 19 | Runner homes still hold no `auth.json` after all of the above | `find <base> -name auth.json` | |
| 20 | No secret in gathered journal, audit rows, or metric labels | `journalctl -u ccswitch-daemon -u ccswitch-codex-auth` since the run | |

### Fleet and mixed-version

| # | Check | Result |
|---|---|---|
| 21 | Explicit Codex dispatch reaches a compatible runner and is refused on an incompatible one | |
| 22 | A runner ahead of the edge reports `contract_hash_mismatch` / `dispatchable: false` rather than being dispatched to | |

---

## Enablement — do not do this before Part 2 is complete

Once, and only once, every Part 2 row is a pass:

1. Set `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED=true` then `CCSWITCH_CODEX_ENABLED=true`
   in the private deployment, **runner by runner**, checking the doctor between each.
2. Leave `CCSWITCH_CODEX_AUTOMATIONS_ENABLED=false`. Automation parity is Phase 7
   (#1154) and has its own origin-specific acceptance tests; an unaudited origin
   must not be able to choose Codex.
3. Record the enablement date and the flag state per host at the bottom of this
   document.
4. Flip `docs/product/features/openai-codex-integration.md` to `state: shipped`.

**Enablement log**

| Date | Host | `CODEX_ENABLED` | `EXTERNAL_AUTH_ENABLED` | `AUTOMATIONS_ENABLED` | By |
|---|---|---|---|---|---|
| — | — | false | false | false | (not yet enabled) |

---

## Deferred to the post-launch cleanup

`server/sessions/host_engines.py` still applies the plan's one-window
compatibility default: a daemon whose `/info` has no `engines` key is read as
Claude-only. The plan schedules its removal for this task, and it is **not**
removed here on purpose — Phase 5 landed on 2026-09-05 and the fleet has not
finished taking it, so removing the default now would make a legacy runner
advertise *no* engines and drop it out of **Claude** dispatch, which is a
production outage rather than a cleanup.

Remove it in a follow-up once `/info.engines` is confirmed present on every host
in the fleet registry — that is a one-line check against
`GET /api/fleet/hosts` — and after the enablement log above has entries.

---

## Related

* `docs/superpowers/plans/2026-09-04-native-openai-codex-integration.md` — Task 6.2
* `docs/runbooks/codex-credential-authority.md` — the procedures behind every row
* `2026-09-04-codex-external-auth-findings.md` — the Phase 0.2 two-runner gate
