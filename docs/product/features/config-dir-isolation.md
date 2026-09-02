---
slug: config-dir-isolation
title: Per-Account Config-Dir Isolation
state: live
links:
  - kind: powered_by
    project: ccswitch
    feature: multi-account-management
  - kind: integrates_with
    project: ccswitch
    feature: session-engine-terminal
---

# Per-Account Config-Dir Isolation

**Problem** — The whole product is "cc-account-**switcher**," and the defining primitive is: run the *real* `claude` binary as a chosen account without accounts bleeding into each other. Claude stores auth + state under a config directory; if two accounts shared one, credentials, usage, and session state would collide. Something must give each account its own isolated `CLAUDE_CONFIG_DIR` and let a caller *switch* which one a given `claude` process uses — per-process, never globally.

**Target users** — every other capability in ccswitch (it's the substrate) and any user/session that needs to act as a specific account.

**Desired outcome** — each account maps to a composed, isolated `CLAUDE_CONFIG_DIR`; switching an account for a shell or a spawned session is a single, side-effect-free operation (`ccswitch use <account>` → export line) with strict per-process isolation and no mutation of the shared/global environment.

**Success metrics** — two accounts never read or write each other's config/credentials/usage; a session bound to account A is unaffected by a switch of account B; switching is per-process (concurrent sessions on different accounts coexist on one host).

**Non-goals** — deciding *which* account to use (rotation), managing the account list/metadata (multi-account-management), or encrypting the credentials at rest (credential-store-encryption). This feature is purely the isolation + switch mechanism.

**Notes** — this is Constitution Principle II ("account = `CLAUDE_CONFIG_DIR`, strict per-process isolation") and underpins Principle I (never fork/patch the binary — ccswitch composes a config dir and runs the stock `claude`). `ccswitch use` composes the dir and prints the export; the session engine sets the same env per spawned PTY so many accounts run concurrently on one host.

**Evidence** — `src/ccswitch/configdir.py` (composes/points `CLAUDE_CONFIG_DIR` per account), `src/ccswitch/cli.py` `use` subcommand (compose + print export line), `src/ccswitch/launcher.py` / `src/ccswitch/runner.py` (per-process env for spawned sessions); `CLAUDE_CONFIG_DIR` referenced across `src/ccswitch/{configdir,launcher,runner,paths,auth_login,copilot}.py`; `.specify/memory/constitution.md` Principle II. (Reconstructed in curation — the eponymous switch/isolation primitive was buried inside multi-account-management; surfaced here as the foundational capability.)
