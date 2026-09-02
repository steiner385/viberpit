---
slug: credential-store-encryption
title: Credential Store & Encryption
state: live
links:
  - kind: powered_by
    project: ccswitch
    feature: config-dir-isolation
  - kind: integrates_with
    project: ccswitch
    feature: account-reauthentication
---

# Credential Store & Encryption

**Problem** — ccswitch holds the credentials for every managed Claude (and Copilot) account. On a self-hosted, network-reachable server, storing those secrets as plaintext on disk is an unacceptable exposure: anyone with filesystem read (a stray backup, a misconfigured mount, a compromised process) would harvest every account's auth. The credential store needs encryption at rest with a key that is not itself sitting next to the ciphertext.

**Target users** — the operator running ccswitch (their accounts' secrets), and every capability that reads credentials to launch or re-auth a session.

**Desired outcome** — account credentials are encrypted at rest via envelope encryption; the master key lives in the OS keyring (not in the store); migrating an existing plaintext store to encrypted is a safe, idempotent operation; day-to-day reads/writes transparently decrypt/encrypt.

**Success metrics** — no plaintext credential material on disk after `crypto migrate`; master key sourced from the keyring, not the store; `crypto migrate` is idempotent (re-runnable with no double-encryption); encryption status is inspectable (`crypto status`).

**Non-goals** — the initial/again login flow that *obtains* credentials (account re-authentication), account list/metadata (multi-account-management), or transport security to the browser (tailnet identity auth).

**Notes** — exposed through the CLI `crypto` subcommand: `status` (key source + slot counts), `init` (generate a master key, store in keyring), `migrate` (encrypt all plaintext slots, idempotent). Envelope encryption = per-slot data keys wrapped by a keyring-held master key, so rotating/backing up is tractable.

**Evidence** — `src/ccswitch/crypto.py` (envelope encryption + keyring master key), `src/ccswitch/creds.py` (credential store read/write), `src/ccswitch/cli.py` `crypto {status,init,migrate}` subcommands (`_cmd_crypto`). (Reconstructed in curation — a stable security foundation with low recent churn that the churn-weighted archaeology omitted; see #903-adjacent.)
