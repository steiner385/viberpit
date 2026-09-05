# Codex external-auth two-runner spike findings

**Date:** 2026-09-04

**Status:** Phase 0.2 release gate passed. The no-model-turn `account/rateLimits/read` trigger did
not invoke the external-auth callback. The separately owner-approved model-turn trigger on
`codex-cli 0.153.2` completed one tools-disabled exact-`OK` turn per runner, exactly one successful
callback from each runner, one coalesced authority refresh and credential generation, successful
recovery reads, clean auth-only runner-home scans, clean redaction, and complete cleanup. No shared
`auth.json` or shared `CODEX_HOME` fallback is permitted.

## Boundary inspected

The local installed binary is `codex-cli 0.153.2`. Its experimental app-server JSON schemas were
generated into a disposable directory and reduced canonically to the files governing
initialization, external login, account reads, rate-limit reads, model listing, external-token
refresh, thread start, turn start, and the turn/item notifications needed to prove a model response
without tool activity. The SHA-256 of that relevant schema subset is:

```text
09d4456bb8687bbe5abe3750bcb2dd2b2a92c91478a46810399182f064b7d3a9
```

The generated contract contains:

- `initialize`, with `capabilities.experimentalApi`;
- `account/login/start`, including the unstable `chatgptAuthTokens` variant;
- `account/read`, including its managed-auth proactive-refresh option;
- `model/list`;
- `account/rateLimits/read`;
- the server request `account/chatgptAuthTokens/refresh` and its response shape;
- `thread/start`, `turn/start`, `turn/completed`, `item/started`, and `item/completed`.

The external-token login schema remains explicitly marked unstable/internal by this CLI version.
That confirms the accepted design's version pin, startup probe, kill switch, and fail-closed
requirements remain necessary.

## Harness implemented

[`scripts/spike_codex_external_auth.py`](../../../scripts/spike_codex_external_auth.py) is an
opt-in harness whose default credential-bearing path creates no model turn. A second exact opt-in
enables the bounded paid model-turn trigger used for the final proof. Its live run does the
following:

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
5. Sends the access-token envelope to each runner app-server only in JSON-RPC stdin. Tokens and the
   native ChatGPT account id are never placed in process arguments, environment variables, shell
   history, or temporary files by the harness. After shutdown, runner-visible access tokens and
   the account id also enter one short-lived authenticated SSH scanner through stdin solely for
   exact byte leak detection; the scanner emits aggregate counts only. The refresh token is never
   transmitted to either runner or scanner.
6. Initializes runners with `experimentalApi=true` and concurrently validates meaningful content,
   not merely JSON-RPC success: a present ChatGPT account and plan, at least one well-formed model,
   and at least one percentage-bearing rate-limit window with no contradictory account binding.
7. Corrupts only the in-memory JWT signature, waits for both runners' unauthorized refresh
   callbacks, and coalesces them through one managed authority `account/read` request with proactive
   refresh enabled. Generation 1 is recorded only if the authority cache contains a different
   access token for the same account. Both callbacks receive that advanced token; unchanged or
   corrupted tokens fail the gate. The paired trigger requests and all callback tasks are drained
   before evaluation; a recorded sanitized callback/authority failure takes precedence over the
   downstream rate-limit error it caused, in stable runner order. With no callback failure, the
   original downstream result is preserved. The original rate-limit operations and a second full
   set of validated read-only calls must then succeed.
8. Closes every child, scans each runner home for `auth.json`, symlinks, and exact received secret
   bytes, scans captured child stderr and final evidence for every known sensitive value, then
   deletes the dedicated temporary homes. Any exception from subprocess communication—including
   timeout, cancellation, or another `BaseException`—first terminates the child, kills it after a
   bounded grace period if needed, and reaps it before re-raising the original exception. Cleanup
   of every registered home still runs if any app-server close fails. If both the protocol body
   and finalization fail, the body exception remains primary and receives only a static,
   secret-free cleanup note; if only finalization fails, its failure remains primary. Failure JSON
   recognizes only that exact note and emits `cleanup_also_failed: true`; arbitrary exception notes
   are never serialized. App-server error responses contribute no native code, message, data, raw
   frame, path, host, account identifier, or stderr. Failure evidence may add only a request method,
   execution stage, and process role selected from fixed in-process allowlists. Broken pipes,
   connection resets, and other request/notification write failures use the same sanitized context;
   canceled requests preserve cancellation while unregistering their pending entry. A late
   response to an abandoned id is discarded without disrupting other pending requests. Response
   ids must have exact integer type, so JSON booleans cannot alias request ids `0` or `1` through
   Python's `bool` subclassing. Server callback tasks are retained until their exceptions are
   consumed; handler and reply-transport failures are recorded as generic contextual protocol
   errors, while cancellation retains its original identity. If no callback root or cancellation
   exists but the downstream trigger still fails, one of four static messages distinguishes zero
   callbacks, partial callbacks, all callbacks with generation advancement, or all callbacks with
   incomplete refresh/generation state. The three coalescer counters are checkpointed before the
   trigger so prior successful refreshes cannot satisfy a new attempt. Only exactly the expected
   new callbacks, one new refresh, and one new generation qualify as advanced; regressions,
   overcounts, and retries are anomalous. No raw counts are emitted.
   After both downstream responses, each runner observes callback-task registrations until 0.1
   seconds of quiet, with a 2-second maximum. Every registration resets the quiet interval and all
   registered tasks are drained; failure to quiesce becomes a static callback protocol error. This
   bounded fence closes local reader/task scheduling races without claiming callbacks cannot occur
   outside the tested app-server ordering window.

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

## First credential-bearing attempt after the SSH correction

The explicitly opted-in dobby/rocky run was retried after the SSH fix. It completed its cleanup and
exited nonzero with only this sanitized evidence:

```json
{"error":"app-server request returned an error","release_gate":"fail"}
```

A follow-up cleanup audit found zero matching temporary runner homes locally and remotely. No raw
protocol frame, native error response, child stderr, host/account identifier, path, or token was
printed, and the harness did not create a thread or model turn.

That evidence was too aggressively minimal: it did not identify whether the authority or a runner
returned the error, which request failed, or which test stage had been reached. Therefore this run
does not establish whether managed credentials had already been loaded or whether a refresh was
attempted, and it provides no external-auth compatibility result. The request tracker now retains
only three fixed-vocabulary fields—`method`, `stage`, and `role`—when a native error response is
discarded. Unknown values are omitted rather than serialized. No live credentials or app-server
were used to test this diagnostic change, and the live gate remains pending until another explicit
run succeeds.

Durable fix pattern:

- **Symptom:** a boundary failure is secret-safe but too generic to locate the failing operation.
- **Anti-pattern:** discard safe request metadata with the pending request, then expose either no
  context or the untrusted native error body.
- **Fix shape:** retain only compile-time, fixed-vocabulary method/stage/role values alongside the
  pending future; discard the complete native error object and omit any context outside those
  allowlists.
- **Where to look:** returned JSON-RPC errors, timeouts, reader shutdown, notification writes,
  background-task failures, server callbacks, and response validators. The spike audit applied the
  same context policy to each of those paths; it found no other in-scope protocol client.

## Diagnostic live retry

The next explicitly opted-in run used the reviewed method/stage/role diagnostics and exited with:

```json
{"error":"app-server request returned an error","method":"account/rateLimits/read","release_gate":"fail","role":"runner","stage":"refresh_trigger"}
```

This establishes that authority initialization and account validation, both runner initializations
and external-token logins, and both runners' initial authenticated account/model/rate-limit checks
completed before the deliberately invalid token reached the refresh trigger. Cleanup completed and
no native response body, host/account identifier, path, stderr, or credential was emitted. The run
did not establish whether the runner issued its host refresh callback or whether that callback or
the authority refresh failed: the downstream rate-limit response won the exception race even when
a callback root failure had already been recorded.

The trigger stage now checkpoints each runner's callback failures, collects both downstream results
without early cancellation, drains all tracked callback tasks, and selects a sanitized callback
failure first in stable runner order. If no callback failed, the original downstream error remains
primary. Before either protocol-error selection, it scans every runner's complete callback slice
and every downstream outcome for cancellation; any cancellation dominates and retains its original
object identity. This precedence change was verified without live credentials or app-server
processes; another explicit live run is required.

That explicit repeat produced the same sanitized downstream result shown above. This proves no
recorded callback failure displaced it, but it remained ambiguous among zero callbacks, only one
runner callback, both callbacks with a completed generation, and both callbacks with incomplete
refresh/generation state. The evaluator checkpoints `callback_count`, `refresh_calls`, and
`generation` before issuing either trigger, then uses only their post-drain deltas to select a fixed
message for those four states; it does not emit the values. Seeded history is ignored, and any
counter regression, callback overcount, repeated refresh, or impossible generation delta maps to
the incomplete/anomalous message rather than advanced. At that point, the live gate had not yet
been rerun with this classifier.

## Final classified live result

The explicitly opted-in dobby/rocky gate was rerun with delta-based classification and exited 1
with this sanitized evidence:

```json
{"error":"refresh trigger failed before any runner callback","method":"account/rateLimits/read","release_gate":"fail","role":"runner","stage":"refresh_trigger"}
```

This classified the tested no-model-turn boundary but did not yet close a local scheduling race.
Authority initialization and
account validation, both runner initializations and external-token logins, and both runners'
meaningful initial account/model/rate-limit reads completed. After the harness installed the
deliberately invalid in-memory access token, `account/rateLimits/read` failed without either runner
registering `account/chatgptAuthTokens/refresh` before the downstream responses. Review then found
that callback-task registration could theoretically land just after that drain, so a bounded
quiescence fence was added before treating zero callbacks as final.

Failure cleanup completed. A follow-up audit found zero matching temporary homes locally and on
rocky, and rocky still reports `codex-cli 0.153.2`. No thread or model turn was created, and no
native response body, host/account identifier, path, stderr, or credential was emitted.

## Final fenced live rerun

The gate was rerun after adding the protocol fence and again exited 1 with the same static
zero-callback evidence. Both downstream responses completed, then each runner observed callback
registrations until 0.1 seconds of quiet, subject to the 2-second maximum. No callback registered
within that bounded window.

Cleanup again left zero matching temporary homes locally and on rocky, and rocky still reported
`codex-cli 0.153.2`. No thread or model turn was created. This fails Phase 0.2 for the tested
no-model-turn `account/rateLimits/read` trigger on that exact CLI version. It is not evidence that a
model-turn path cannot invoke the callback; testing that surface requires explicit owner approval.
The implementation must not fall back to a shared `auth.json` or shared `CODEX_HOME`; the
alternative is an approved architecture revision based on the bounded surface verified here.

## Owner-approved model-turn follow-up

The owner approved exactly one minimal paid turn on each of the two runners. The additional path is
guarded by the separate exact opt-in `CCSWITCH_TEST_CODEX_MODEL_TURN=1`; the original no-model-turn
behavior remains the default. Before loading credentials, each target runs a local fake Responses
provider preflight against the same fixed `gpt-5.6-luna` model and thread configuration. Both
preflights must observe that the outgoing model request has no tool catalog (the field is absent or
an exact empty list), and both initial model catalogs must expose that exact model.

The live path creates one ephemeral, read-only, approval-never thread per runner, disables every
supported tool capability, supplies no dynamic tools or environments, and starts exactly one turn
whose response must be exactly `OK`. Its observer permits only user-message, reasoning, and
assistant-message items; any tool/activity item, additional assistant response, or non-`OK` text
fails the gate. Thread and turn identifiers are treated as sensitive for terminal output, stderr,
and final evidence, but are expected native runtime metadata inside the dedicated host-local runner
home.

## First model-turn run and scanner correction

The first approved two-turn run reached post-recovery scanning: both tools-disabled exact-`OK`
turns completed, both runners invoked the refresh callback, the callbacks coalesced to one authority
refresh and generation, the access token advanced, and recovery reads passed. The final home scan
then falsely failed because it searched host-local runtime databases for native thread/turn ids,
which Codex necessarily persists there. Cleanup still left zero temporary homes or processes on
either host, and the authority credential cache remained owner-only mode `0600`.

The scanner was corrected by defining sensitive-value classes per sink. Output, stderr, and final
evidence reject credentials, account identifiers, refresh history, and native thread/turn ids.
Runner-home scans reject every current or historical runner-visible access/invalid token and account
identifier, while excluding authority-only refresh tokens and expected local runtime ids. Regression
tests cover local runtime databases, remote scanner stdin, and multiple credential generations. A
sibling-pattern audit found no other in-scope scanner using the same mistaken union.

## Final model-turn release-gate result

The explicitly approved retry completed successfully and emitted this sanitized allow-listed
evidence:

```json
{"authority_on_local_runner":true,"cleanup_complete":true,"cli_version":"codex-cli 0.153.2","credential_generation":1,"distinct_runner_hosts":true,"empty_model_tool_catalog_preflight":true,"experimental_api_required":true,"healthy_read_only_calls":true,"model_turn_trigger_used":true,"protocol_methods":["initialize","account/login/start","account/read","account/rateLimits/read","model/list","account/chatgptAuthTokens/refresh","thread/start","turn/start","turn/completed","item/started","item/completed"],"redaction_clean":true,"refresh_callback_observed":true,"refresh_calls":1,"release_gate":"pass","runner_count":2,"runner_homes_clean":true,"schema_sha256":"09d4456bb8687bbe5abe3750bcb2dd2b2a92c91478a46810399182f064b7d3a9","tool_free_exact_ok_turns_completed":true,"topology_sha256":"07e48cc4370c149ee5c5ea308dfabdf4e9ecdefbb8f6af4ba68f93f4139ad90d"}
```

An independent allowlist check confirmed the pass state, two distinct runners, local authority,
empty-tool preflights, two exact-`OK` turns, two observed callbacks coalesced to one refresh and one
credential generation, clean homes/redaction/cleanup, and the absence of access-token and account-id
fields. Independent cleanup checks found zero temporary homes and zero matching app-server processes
on both hosts. The authority `auth.json` remained owned by `tony:tony` with mode `0600`.

## Post-run callback-attribution correction

Final review found that the successful run's `refresh_callback_observed` boolean was derived from
an aggregate callback count of two. Because both runner processes shared the same untagged handler,
two successful callbacks from one process and none from the other could theoretically satisfy that
assertion. Both invalid-token turns and all recovery reads completed, so the observed behavior is
consistent with one callback per runner, but the evidence did not directly prove that distribution.

Each runner now receives a separately registered handler closure bound to an opaque, stable,
in-process identity. The release gate requires exactly one successfully completed callback for every
registered runner; duplicate callbacks from one runner cannot cover a missing callback from another.
No identity, per-runner count, hostname, or other operational detail enters final evidence. The
regression suite covers successful one-per-runner coverage, failed callbacks not being counted, and
duplicate-one/missing-other rejection. The corrected runner-attributed live run passed with exactly
one successful callback from each runner, one authority refresh/generation, and the same clean
recovery, redaction, runner-home, and cleanup results.

The paid model-turn path refuses to start unless all three of these are explicitly set:

```bash
CCSWITCH_TEST_CODEX_EXTERNAL_AUTH=1 \
CCSWITCH_TEST_CODEX_MODEL_TURN=1 \
CCSWITCH_TEST_CODEX_ACCOUNT=<non-secret-account-alias> \
python scripts/spike_codex_external_auth.py 2>&1 \
  | tee /tmp/codex-external-auth-spike.log
```

Omitting `CCSWITCH_TEST_CODEX_MODEL_TURN` retains the default no-model-turn path; it does not
silently enable a paid turn.

`CCSWITCH_TEST_CODEX_RUNNERS` may override the default `local,rocky` pair. An alternate
authority-only Codex home may be selected with `CCSWITCH_TEST_CODEX_AUTHORITY_HOME`; these values
are paths/aliases, never native account identifiers or credentials.

## Verification completed

- Focused redaction/security suite: 127 tests passed.
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
  remote state.
- First post-fix credential-bearing live attempt: failed with a sanitized app-server JSON-RPC error;
  cleanup completed, but the then-current failure format did not identify the method, stage, or
  authority/runner role.
- Diagnostic live retry: reached a runner `account/rateLimits/read` at `refresh_trigger` before
  failing; the then-current evaluation did not prefer a recorded callback root failure over that
  downstream error.
- Callback-precedence live repeat: returned the same runner `account/rateLimits/read`
  `refresh_trigger` failure. It remained ambiguous until the four-state coalescer classifier was
  added.
- Classified live run: observed no runner callback through the downstream responses after healthy
  initial external-auth reads; review then identified the callback-registration scheduling race.
- Final fenced live rerun: returned the same static zero-callback result after both downstream
  responses plus the 0.1-second quiet/2-second maximum barrier. It exited 1, cleanup left zero
  matching temporary homes locally and on rocky, and rocky remained on `codex-cli 0.153.2`.
- Credential-free model-turn preflight: passed on both hosts for `gpt-5.6-luna`; each outgoing
  request used the final model/thread configuration and exposed no tool catalog.
- Final approved model-turn retry: passed with exactly one exact-`OK`, tools-disabled turn per
  runner; both callbacks coalesced to one authority refresh/generation, recovery reads passed, and
  independent redaction, runner-home, process, and cleanup checks passed.
- Runner-attributed confirmation: passed after binding an opaque handler identity to each process;
  each runner completed exactly one successful callback, duplicate-one/missing-other coverage was
  impossible, and independent cleanup again found zero temporary homes or processes on both hosts.

The focused tests cover exact-value removal for access tokens, refresh tokens, native account ids,
invalid test tokens, and native thread ids; recursive diagnostic sanitization; secret-free process
metadata; runner-home detection; allow-listed topology/final evidence; same-machine and non-local
topology rejection; meaningful account/model/rate-limit response validation; refresh refusal when
the managed access token does not advance; concurrent refresh coalescing onto one advanced token;
subprocess timeout/cancellation/exception reaping with original-exception preservation; cleanup
despite close failure; remote-home registration before an interruptible create; absent-home
cleanup; body-versus-cleanup exception precedence with sanitized diagnostics; cleanup-only failure
propagation; allow-listed combined-failure JSON without arbitrary-note or cleanup-detail leakage;
ordinary failure JSON; native error responses containing adversarial fake secrets and operational
details; fixed-vocabulary method/stage/role diagnostics; unsafe-context omission; and strict opt-in
behavior. Transport cases additionally cover broken-pipe, connection-reset, unexpected-write, and
cancellation behavior for requests and notifications, plus a late canceled response arriving while
another request remains pending. Response-id cases prove live and abandoned integer request `1`
cannot be matched by boolean `true`, and unknown/malformed ids fail safely. Callback cases cover a
genericized `SpikeError`, broken-pipe and connection-reset error replies, cancellation identity,
spawned-task exception consumption, and the exact final failure JSON allowlist. Trigger-precedence
cases cover callback-root preference, a successful callback, no callback, two concurrent runner
failures with stable precedence, and cancellation identity. Cancellation-precedence cases cover a
handler protocol failure followed by error-reply cancellation in the same callback slice, a second
runner's callback cancellation versus the first runner's protocol error, and downstream
cancellation versus a callback error. Four downstream-classification cases cover zero callbacks,
partial callbacks, all callbacks with an advanced generation, and all callbacks with incomplete
refresh/generation state relative to seeded prior success. Additional anomaly cases cover callback
overcount and repeated refresh, and the successful path remains unchanged. Callback-fence cases
cover registration before the fence, within the quiet interval, and after the barrier, plus a
non-quiescent deadline failure. The remote scanner test proves runner-visible access/account values
travel only in authenticated SSH stdin—not argv or environment—and the refresh token is excluded.

The five new SSH construction cases cover multiline Python preservation; exact `sh -c` and
app-server positional arguments; spaces and shell metacharacters as inert data; unsafe destination
rejection; and empty/NUL-bearing command-vector rejection.

## Live release gate passed

The no-model-turn endpoint remains a documented negative result, not the runtime refresh trigger.
The corrected owner-approved model-turn proof satisfied every Phase 0.2 condition on two distinct
runner machines using one authority-managed OpenAI account: healthy initial reads, target-local
zero-tool preflight, exact bounded turns, exactly one successful callback per runner, one coalesced
authority refresh/generation, token advancement, recovery reads, auth-clean host-local homes,
secret-clean output, and complete cleanup.

Phase 0.2 therefore unblocks implementation of the accepted dedicated-auth-listener architecture.
This does not authorize shared credentials or a shared home: runtime Codex support must keep Dobby
as the sole credential authority and give each runner its own host-local `CODEX_HOME`.
