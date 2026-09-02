---
slug: command-line-interface
title: Command-Line Interface
state: live
links:
  - kind: powered_by
    project: ccswitch
    feature: config-dir-isolation
  - kind: powered_by
    project: ccswitch
    feature: credential-store-encryption
  - kind: integrates_with
    project: ccswitch
    feature: multi-account-management
---

# Command-Line Interface

**Problem** — ccswitch is defined as "a CLI core plus a web-hosted interface," but before the web dashboard existed (and still, for scripting and headless setup) the primary way to manage accounts and switch which account a `claude` invocation runs under is the terminal. Without a first-class CLI, every account operation would require the daemon + web stack to be up, and there would be no scriptable, composable entry point for shells, cron, or setup automation.

**Target users** — operators setting up ccswitch on a new host, power users who script account operations, and anyone who wants to launch or switch a `claude` account from a plain shell without the web UI or daemon.

**Desired outcome** — `ccswitch <cmd>` manages the full account lifecycle and produces a shell-consumable account switch (`ccswitch use <account>` prints an export line) with no dependency on the web/daemon stack.

**Success metrics** — every account operation (add/list/remove/link/use) is doable from the CLI alone; `ccswitch use` output can be `eval`'d to switch the active account in one line; the CLI runs on a fresh checkout before any daemon exists.

**Non-goals** — running or streaming live sessions (that is the session engine + web terminal); rotation logic (usage-aware rotation); serving the dashboard.

**Notes** — the CLI is the console entry point `ccswitch = "ccswitch.cli:main"` and is intentionally dependency-light (stdlib `argparse`) so it works on a bare host. Subcommands: `list`, `add <name>`, `remove <name>`, `link [name]`, `use` (compose a config dir for an account and print its export line), and `crypto {status,init,migrate}` (credential-store encryption management).

**Evidence** — `src/ccswitch/cli.py` (argparse `list`/`add`/`remove`/`link`/`use`/`crypto` subcommands); `pyproject.toml` console-script `ccswitch = "ccswitch.cli:main"`; delegates to `src/ccswitch/accounts.py`, `src/ccswitch/configdir.py`, `src/ccswitch/creds.py`, `src/ccswitch/crypto.py`. (Reconstructed in the feature-map curation pass — the CLI is a stable, low-churn foundation the churn-weighted archaeology under-represented; see #903-adjacent curation.)
