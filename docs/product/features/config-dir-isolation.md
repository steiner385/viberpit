---
slug: config-dir-isolation
parent: security-and-isolation
title: Per-Account Config-Dir Isolation
state: live
links:
  - kind: powered_by
    project: viberpit
    feature: multi-account-management
  - kind: integrates_with
    project: viberpit
    feature: session-engine-terminal
---

# Per-Account Config-Dir Isolation

Run accounts concurrently without sharing configuration or credentials across their session processes.

**Problem** — The whole product is "cc-account-**switcher**," and the defining primitive is: run the *real* `claude` binary as a chosen account without accounts bleeding into each other. Claude stores auth + state under a config directory; if two accounts shared one, credentials, usage, and session state would collide. Something must give each account its own isolated `CLAUDE_CONFIG_DIR` and let a caller *switch* which one a given `claude` process uses — per-process, never globally.

**Target users** — every other capability in ViberPit (it's the substrate) and any user/session that needs to act as a specific account.

**Desired outcome** — each account maps to a composed, isolated `CLAUDE_CONFIG_DIR`; switching an account for a shell or a spawned session is a single, side-effect-free operation (`viberpit use <account>` → export line) with strict per-process isolation and no mutation of the shared/global environment.

**Success metrics** — two accounts never read or write each other's config/credentials/usage; a session bound to account A is unaffected by a switch of account B; switching is per-process (concurrent sessions on different accounts coexist on one host).

**Non-goals** — deciding *which* account to use (rotation), managing the account list/metadata (multi-account-management), or encrypting the credentials at rest (credential-store-encryption). This feature is purely the isolation + switch mechanism.

**Notes** — this is Constitution Principle II ("account = `CLAUDE_CONFIG_DIR`, strict per-process isolation") and underpins Principle I (never fork/patch the binary — ViberPit composes a config dir and runs the stock `claude`). `viberpit use` composes the dir and prints the export; the session engine sets the same env per spawned PTY so many accounts run concurrently on one host.

**Evidence** — `src/viberpit/configdir.py` (composes/points `CLAUDE_CONFIG_DIR` per account), `src/viberpit/cli.py` `use` subcommand (compose + print export line), `src/viberpit/launcher.py` / `src/viberpit/runner.py` (per-process env for spawned sessions); `CLAUDE_CONFIG_DIR` referenced across `src/viberpit/{configdir,launcher,runner,paths,auth_login,copilot}.py`; `.specify/memory/constitution.md` Principle II. (Reconstructed in curation — the eponymous switch/isolation primitive was buried inside multi-account-management; surfaced here as the foundational capability.)
