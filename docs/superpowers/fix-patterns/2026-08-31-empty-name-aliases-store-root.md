# Fix pattern: an empty identifier joined into a path aliases the store ROOT

**Date:** 2026-08-31
**Origin:** RC of the "session spins forever and never replies" investigation (production session
`8220c5ac`, 2026-08-31 08:10 EDT). One instance was fixed; this is the sibling sweep required by
`CLAUDE.md`'s "audit recurring fix patterns" rule.

## The bug signature

Account state lives in per-account paths built by joining a **name** onto a shared root:

```python
def slot_path(name):            return store_dir() / f"{name}.cred.json"
def account_cfg_dir(name):      return cfg_root() / name
def copilot_token_dir(name):    return copilot_store_dir() / name
```

`Path(root) / ""` is `Path(root)` — joining an empty segment is a silent no-op, not an error. So an
**empty account name makes a per-account path collapse onto shared state**, and two things follow:

1. **Writes go to the shared root.** `sync_back("")` wrote `store/.cred.json`, and the
   `f"{name}..."` variants wrote `store/.refresh.lock` — files that look like account slots.
2. **Existence probes return a false positive.** `provider_of(name)` answers
   "is this a Copilot account?" with `copilot_token_dir(name).exists()`. For `name=""` that probes
   the *Copilot store root*, which exists as soon as any Copilot account is configured — so a
   nameless account was reported as a Copilot account.

The two halves then fed each other. `_anthropic_account_names()` enumerates the store with
`d.glob("*.cred.json")`; unlike a shell glob, **`pathlib.Path.glob` does not hide dotfiles**, so it
matched the stray `.cred.json` and yielded the stem `""`. `all_accounts()` published a phantom
account `("", "anthropic")`, and the rotation picker treated it as a rotation candidate.

## What it cost

A rate-limited turn triggered `_rotate_away()`. `select_account()` returned the phantom `""`.
`_reassign()` — which, unlike `switch_account_manual()`, did **not** validate its target — closed
the session's working driver, then asked `provider_of("")`, got `"copilot"`, and tried to start a
Copilot proxy for a nameless account:

```
reassign.error session=8220c5ac-…
server.sessions.copilot_proxy.ProxyStartError: copilot proxy for  never became ready
```

Note the double space: the account name in that message is empty. `_reassign`'s `except Exception:`
logged and swallowed, so the session was left with **no driver, no status change and no event**. The
UI kept showing a live-looking session; every prompt the user sent spun forever with no reply. The
transcript's last write stayed frozen for over 40 minutes while sibling sessions turned normally.

## How it was detected

Not by any test or health check — the daemon was healthy and every other session worked. The signal
was the daemon journal, after filtering out the RPC access-log noise that dominates it:

```bash
journalctl -u ccswitch-daemon.service --since "07:20" --no-pager \
  | grep -v '"POST /rpc/' | grep -v '"GET /' \
  | sed -E 's/^[A-Za-z]{3} [0-9]{2} [0-9:]+ dobby python\[[0-9]+\]: //' \
  | sort | uniq -c | sort -rn
```

Collapsing the log to distinct lines by frequency surfaced `reassign.error session=8220c5ac` and
`copilot proxy for  never became ready` immediately. The empty interpolation in that message is what
named the bug. It was then confirmed directly against the live install:

```python
>>> paths.copilot_token_dir('')     # → /home/tony/.claude-accounts/store/copilot  (the ROOT)
>>> accounts.provider_of('')        # → 'copilot'
>>> accounts.all_accounts()         # → [('', 'anthropic'), ('ibaset-personal', 'anthropic'), …]
```

## Why the existing tests missed it

`tests/test_account_names.py` already had a validator (`validate_account_name`) that correctly
rejects `""`, `"."` and `".."` — the invariant was *known*. But it was only enforced at the
**boundaries where a human types a name** (the add-account API, `switch_account_manual`), never on
names the system generated for itself by enumerating the filesystem. Every test constructed accounts
through helpers that write a well-named slot, so no test ever asked what happens when the store
contains a file that is not an account. **A validator that exists but is not on the path the value
actually travels is not a guard.**

## Method for this sweep

1. `grep -rn "slot_path(\|account_cfg_dir(\|copilot_token_dir(" src/ server/ --include=*.py` —
   every consumer of a name→path helper (30 sites).
2. `grep -rn "\.glob(\|iterdir()" src/ccswitch/ server/ --include=*.py` — every place a name is
   *produced* by enumerating the filesystem, since that is how an invalid name gets minted.
3. For each hit, ask both questions: can this receive an empty/dot name, and does it *probe or
   enumerate* a path that would then alias a shared root?

## Full results

### Name → path helpers (`src/ccswitch/paths.py`)

| Site | Shape with `name=""` | Verdict |
|---|---|---|
| `copilot_token_dir(name)` = `copilot_store_dir() / name` | the Copilot store **ROOT**; `.exists()` always True | **root cause — fixed.** `provider_of` now short-circuits on `aliases_store_root(name)`. |
| `slot_path(name)` = `store_dir() / f"{name}.cred.json"` | `.cred.json` — a hidden file that `Path.glob("*.cred.json")` still matches | **root cause — fixed.** `_anthropic_account_names()` now filters these stems out. |
| `account_cfg_dir(name)` = `cfg_root() / name` | the cfg **ROOT** | **reachable, closed indirectly.** This is what produced the stray `cfg/.credentials.json` that `sync_back("")` then copied into `store/.cred.json`. No name that aliases the root can reach it now, since enumeration no longer mints one. |
| `_refresh_guard`'s `store / f"{name}.refresh.lock"` (`accounts.py`) | `.refresh.lock` | Same family; produced the second stray file. Closed by the same enumeration filter. |
| `store_claude_json_path(name)` = `store_dir() / f"{name}.claude.json"` | `.claude.json` | **latent.** Nothing enumerates `*.claude.json`, so no phantom account can be minted from it — but it is the same shape and would become live the moment such an enumeration is added. |

### Name-producing enumerations

| Site | What it does | Verdict |
|---|---|---|
| `accounts._anthropic_account_names()` | `store.glob("*.cred.json")`, strip suffix | **root cause — fixed.** Filters `aliases_store_root` stems. |
| `cli.py:54` (`ccswitch crypto` status) | a **second** `store.glob("*.cred.json")` | **fixed** — routed through the new `accounts.anthropic_slot_paths()`. Counted the phantom slot as a plaintext account. |
| `cli.py:97` (`ccswitch crypto migrate`) | a **third** `store.glob("*.cred.json")`, then writes each slot back | **fixed** — same routing. This one was the worst: it would have "migrated" the stray `{}` slot and **written it back**, resurrecting the phantom account after any cleanup. |
| `copilot.copilot_account_names()` | `d.iterdir()` filtered on `is_dir()` + `credentials.json` | **not reachable** — `iterdir()` never yields an empty name, and the `credentials.json` check excludes stray files. |
| `server/library/scan.py:272` (`_needs_auth_by_server`) | `cfg_root.iterdir()`, reading `mcp-needs-auth-cache.json` from each child | **latent, not fixed** — not the empty-name alias (`iterdir` can't produce it), but it treats *every* child of `cfg_root` as an account, including the `copilot` directory and the `commands`/`plugins` symlinks. Today it just reads a missing file and moves on, so the only effect is that MCP-auth state could be attributed to a non-account name. Flagged below rather than fixed, to keep this change scoped. |

## The fix

Three layers, each at the place that actually owns the invariant:

1. **`account_names.aliases_store_root(name)`** — one named definition of the hazard ("this name
   would collapse a per-account path onto the store root"). `validate_account_name` now routes its
   own `"."`/`".."` check through it rather than repeating the set inline.
2. **Enumeration and classification** (`_anthropic_account_names`, `provider_of`) consult it, so a
   stray file can neither become an account nor be misclassified — no matter what is on disk.
3. **`SessionOrchestrator._reassign`** validates its target **before** tearing down the working
   driver, and a failed reassign now calls `_pause_session()` instead of only logging. The guard
   belongs in the shared primitive: `switch_account_manual` had it, `_rotate_away` did not, and the
   unguarded caller is the one that reached production.

## Generalised lesson

> **A name the system derives from the filesystem deserves the same validation as a name a user
> typed.** Validators tend to get installed at the human-input boundary, because that is where
> hostile input is expected. But a filesystem is an input too, and a directory listing is
> user-influenced. Here the validator already existed and already rejected `""` — it just was not on
> the path the bad value travelled.
>
> The corollary for path helpers: **`Path(root) / untrusted` is only a per-item path if `untrusted`
> is non-empty.** An existence probe over an aliased path answers a question about the *root*, not
> about the item — which is exactly how a nonexistent account tested as a real Copilot one.
>
> And for shared primitives: **when a caller validates and the primitive does not, the guard is in
> the wrong place.** The safe caller proves the invariant matters; the unsafe one proves where it
> has to live.

## Flagged, not fixed

**`server/library/scan.py:_needs_auth_by_server` iterates `cfg_root` as if every child were an
account.** It reads `mcp-needs-auth-cache.json` out of `cfg/copilot`, `cfg/commands` and
`cfg/plugins` (the latter two symlinks into `~/.claude`) alongside the real account dirs. Harmless
today — the file is absent, so it contributes nothing — but it means MCP "needs auth" state is keyed
on a set that is not the account set. It should enumerate accounts via `accounts._account_names()`
rather than by listing a directory.
