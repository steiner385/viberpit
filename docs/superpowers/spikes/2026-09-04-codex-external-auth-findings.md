# Codex external-auth two-runner spike findings

**Date:** 2026-09-04

**Status:** release gate pending — the safe harness is implemented and unit-tested, and rocky now
has the pinned CLI. A credential-free topology preflight exposed and corrected an SSH command
transport defect, but the credential-bearing dobby/rocky gate has not been rerun.

## Boundary inspected

The local installed binary is `codex-cli 0.153.2`. Its experimental app-server JSON schemas were
generated into a disposable directory and reduced canonically to the files governing
initialization, external login, account reads, rate-limit reads, model listing, and external-token
refresh. The SHA-256 of that relevant schema subset is:

```text
3d867fa0083c54be185b02a445153f0c5b7bfd1495d75ba6cf103d4a3f6c9be7
```

The generated contract contains:

- `initialize`, with `capabilities.experimentalApi`;
- `account/login/start`, including the unstable `chatgptAuthTokens` variant;
- `account/read`, including its managed-auth proactive-refresh option;
- `model/list`;
- `account/rateLimits/read`;
- the server request `account/chatgptAuthTokens/refresh` and its response shape.

The external-token login schema remains explicitly marked unstable/internal by this CLI version.
That confirms the accepted design's version pin, startup probe, kill switch, and fail-closed
requirements remain necessary.

## Harness implemented

[`scripts/spike_codex_external_auth.py`](../../../scripts/spike_codex_external_auth.py) is an
opt-in, no-model-turn harness. Its intended live run does the following:

1. Regenerates and hashes the exact installed contract subset.
2. Reads `/etc/machine-id` locally and remotely through a one-way domain-separated SHA-256. The
   raw stable identifiers are never emitted. It requires exactly one runner to be the local
   authority machine and requires the two runner digests to differ before loading credentials.
   Thus the default `local,rocky` pair refuses to run when invoked on rocky or when the SSH alias
   resolves back to the local machine. Final evidence contains only two topology booleans and a
   second-level topology SHA-256, not either machine digest.
3. Starts a local managed-auth authority app-server against the explicitly selected authority
   home.
4. Chooses each dedicated `/tmp` path locally with 128 bits of randomness, validates it, and
   registers its process/cleanup record before asking either host to create the mode-0700
   directory. The remote command never chooses an unknown path, so cancellation after creation
   still leaves the controller with the exact cleanup target. An absent/uncreated target is treated
   as already cleaned. It then starts each app-server over stdio.
5. Sends the access-token envelope only in JSON-RPC stdin. Tokens and the native ChatGPT account id
   are never placed in process arguments, environment variables, shell history, or temporary
   files by the harness. The refresh token is never transmitted to either runner.
6. Initializes runners with `experimentalApi=true` and concurrently validates meaningful content,
   not merely JSON-RPC success: a present ChatGPT account and plan, at least one well-formed model,
   and at least one percentage-bearing rate-limit window with no contradictory account binding.
7. Corrupts only the in-memory JWT signature, waits for both runners' unauthorized refresh
   callbacks, and coalesces them through one managed authority `account/read` request with proactive
   refresh enabled. Generation 1 is recorded only if the authority cache contains a different
   access token for the same account. Both callbacks receive that advanced token; unchanged or
   corrupted tokens fail the gate. The original rate-limit operations and a second full set of
   validated read-only calls must then succeed.
8. Closes every child, scans each runner home for `auth.json`, symlinks, and exact received secret
   bytes, scans captured child stderr and final evidence for every known sensitive value, then
   deletes the dedicated temporary homes. Any exception from subprocess communication—including
   timeout, cancellation, or another `BaseException`—first terminates the child, kills it after a
   bounded grace period if needed, and reaps it before re-raising the original exception. Cleanup
   of every registered home still runs if any app-server close fails. If both the protocol body
   and finalization fail, the body exception remains primary and receives only a static,
   secret-free cleanup note; if only finalization fails, its failure remains primary. Failure JSON
   recognizes only that exact note and emits `cleanup_also_failed: true`; arbitrary exception notes
   are never serialized.

Raw app-server protocol stdout is consumed by private pipes and is never forwarded to the terminal.
Child stderr is captured only for the exact-value leak check and is never replayed. The final output
is one allow-listed JSON evidence object containing only version/schema hash, method names,
topology proof, booleans, counts, generation, and gate state.

## SSH preflight defect and correction

The first credential-free dobby/rocky topology preflight failed before credentials were loaded.
The harness passed `python3`, `-c`, and a multiline program as separate local arguments after the
SSH destination. OpenSSH reconstructs those arguments into one remote-shell command string without
preserving the original argument boundaries. Rocky therefore received only `import` as the
`python3 -c` program and the remaining line as a second shell command, producing Python and shell
syntax errors.

The correction adds one `build_ssh_command` source of truth. It validates the destination with the
repository's canonical `SSH_HOST_RE`, rejects empty/NUL-bearing remote command vectors, serializes
the complete remote argv once with `shlex.join`, and gives OpenSSH that single command string.
Machine identity, version inspection, home provisioning and cleanup, app-server launch, and remote
home scanning all use the helper. Secret scan needles remain JSON on stdin and no credential was
added to an argument, environment variable, file, or shell history. A second credential-free
machine-identity probe against rocky succeeded after the correction; this is not a live auth-gate
pass.

Durable fix pattern:

- **Symptom:** a remote command works for simple tokens but loses quoting, splits multiline
  programs, or interprets argument metacharacters when invoked through SSH.
- **Anti-pattern:** append a local argv (`[ssh, host, *remote_argv]`) and assume OpenSSH transports
  the post-host elements as an exec-style argv.
- **Fix shape:** validate the SSH destination and remote inputs, shell-quote the entire remote argv
  once, and pass exactly one remote command string; keep secrets on stdin.
- **Where to look:** subprocess wrappers that append multiple arguments after an SSH destination,
  especially `python -c`, `sh -c`, paths containing whitespace, and environment-derived values.
  The repository audit found two out-of-scope candidates: `server/deploy/runners.py::_ssh` and the
  remote health probe in `scripts/ccswitch-health-check.sh`. Their current arguments are mostly
  constrained/simple, but both rely on remote-shell reconstruction and need a separately reviewed
  transport hardening change before accepting complex or less-constrained inputs.

The script refuses to start unless both of these are explicitly set:

```bash
CCSWITCH_TEST_CODEX_EXTERNAL_AUTH=1 \
CCSWITCH_TEST_CODEX_ACCOUNT=<non-secret-account-alias> \
python scripts/spike_codex_external_auth.py 2>&1 \
  | tee /tmp/codex-external-auth-spike.log
```

`CCSWITCH_TEST_CODEX_RUNNERS` may override the default `local,rocky` pair. An alternate
authority-only Codex home may be selected with `CCSWITCH_TEST_CODEX_AUTHORITY_HOME`; these values
are paths/aliases, never native account identifiers or credentials.

## Verification completed

- Focused redaction/security suite: 58 tests passed.
- Ruff: passed for the harness and focused suite.
- Mypy: passed for the harness.
- Opt-in refusal: passed; without both required values the script exits nonzero with sanitized JSON
  and starts no app-server.
- Local schema generation: passed for 0.153.2 with the subset hash above; its disposable directory
  was removed.
- Original Rocky preflight: SSH was reachable, but `codex --version` returned command-not-found.
- Post-install Rocky CLI/version preflight: `codex-cli 0.153.2` is now reachable
  non-interactively.
- Post-install credential-free topology preflight: failed because SSH split the multiline identity
  program; the corrected remote identity probe then passed without loading credentials or mutating
  remote state. The credential-bearing live gate was not retried.

The focused tests cover exact-value removal for access tokens, refresh tokens, native account ids,
invalid test tokens, and native thread ids; recursive diagnostic sanitization; secret-free process
metadata; runner-home detection; allow-listed topology/final evidence; same-machine and non-local
topology rejection; meaningful account/model/rate-limit response validation; refresh refusal when
the managed access token does not advance; concurrent refresh coalescing onto one advanced token;
subprocess timeout/cancellation/exception reaping with original-exception preservation; cleanup
despite close failure; remote-home registration before an interruptible create; absent-home
cleanup; body-versus-cleanup exception precedence with sanitized diagnostics; cleanup-only failure
propagation; allow-listed combined-failure JSON without arbitrary-note or cleanup-detail leakage;
ordinary failure JSON; and strict opt-in behavior.

The five new SSH construction cases cover multiline Python preservation; exact `sh -c` and
app-server positional arguments; spaces and shell metacharacters as inert data; unsafe destination
rejection; and empty/NUL-bearing command-vector rejection.

## Live release gate still pending

No managed authority app-server or runner app-server was started during this verification. No
credential was read, refreshed, copied, printed, or written. No thread or model turn was created.

The live gate needs all of the following before it can be run safely:

1. The supported Codex CLI installed and discoverable for rocky's non-interactive SSH user
   (**satisfied: 0.153.2**).
2. An owner-selected ChatGPT test-account alias and authority-only managed Codex home.
3. The explicit `CCSWITCH_TEST_CODEX_EXTERNAL_AUTH=1` opt-in.
4. A maintenance/test window in which a supported managed token refresh is acceptable.

Until that run proves distinct runner machines, meaningful authenticated responses, two refresh
callbacks, one authority refresh, actual access-token advancement, read-only recovery on that new
token, clean runner homes, zero stderr/evidence leaks, and complete cleanup, Phase 0.2 has not
passed. Runtime Codex implementation must not fall back to shared `auth.json` or a shared
`CODEX_HOME`.
