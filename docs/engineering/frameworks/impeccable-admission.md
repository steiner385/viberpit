# Impeccable admission evidence

Updated: 2026-10-07. Status: **candidate; source content verified, execution not approved**.

This continues PR 1, Task 2 of
`docs/superpowers/plans/2026-10-07-vetted-engineering-frameworks.md` under the
corresponding spec. `.agents/frameworks.lock.json` remains the canonical selection
and hash inventory. The [installed-surface review](impeccable-surface-review.md)
records instruction and tooling findings. These results do not approve execution.

## Selection and resolved identity

The original inventory baseline was ViberPit commit
`f8ae589ae6f42e5c8bf589e50b4d3fd2700de18b`; vendoring occurred in
`2c093aa7da3a3a057388edefc5f8c277562d2e57`. These identify the local installation.

The 111 selected files comprise both real provider builds under
`.agents/skills/impeccable/` and `.claude/skills/impeccable/`, plus four exported
Claude agents under `.claude/agents/impeccable-*.md`. Preserve the contract's
`provider_built` distinction. Project-owned design/context files are not vendor assets.

| Component | Verified observation | Limit |
|---|---|---|
| Skill 4.3.1 | `skill-v4.3.1` resolves to `cd12f8660e2dde57b9615c8a6b8ea674101f9cfc`. All 111 selected installed files match that commit's source archive byte-for-byte; none are missing or different. | Establishes selected content identity, not instruction safety, dynamic dependency completeness, or compatibility. |
| Installer 4.1.0 | npm metadata names commit `73a6f51a540bc3938a2c40677d074c70b81fa5a0`. Downloaded package matches registry SHA-512 integrity. Its `LICENSE`, `cli/bin/cli.js`, and `package.json` match that source commit exactly. | That commit contains skill 4.3.0 and matches only 105 of the installed files. It is not the installed skill's source pin. Registry signatures were not independently verified. |
| Engine 0.1.5 | The release tag resolves to `112703d5bf2469574758e0ddc5baf8e03c958f58`. Downloaded Linux x64/arm64, macOS x64/arm64, and Windows x64 payloads each match their publisher SHA-256 sidecar. Per-platform URLs, sizes, and observed hashes are in the lock. | A tag target and checksum do not prove a signed or reproducible source-to-binary build. No engine was executed or approved. |
| Linux npm platform package | `@impeccable/cli-linux-x64@0.1.5` matches registry SHA-512 integrity; its binary matches the GitHub Linux x64 payload. Its metadata declares no scripts or dependencies. | Publisher-channel agreement is not independent authentication or a full dependency audit. Other npm platform packages were not compared. |

Primary sources observed on 2026-10-07:

- [Pinned skill source](https://github.com/pbakaus/impeccable/tree/cd12f8660e2dde57b9615c8a6b8ea674101f9cfc) and [source archive](https://codeload.github.com/pbakaus/impeccable/tar.gz/cd12f8660e2dde57b9615c8a6b8ea674101f9cfc).
- [Installer metadata](https://registry.npmjs.org/impeccable/4.1.0) and [pinned installer implementation](https://github.com/pbakaus/impeccable/blob/73a6f51a540bc3938a2c40677d074c70b81fa5a0/cli/bin/cli.js).
- [Engine release](https://github.com/pbakaus/impeccable/releases/tag/engine-v0.1.5) and [Linux npm metadata](https://registry.npmjs.org/@impeccable/cli-linux-x64/0.1.5).

The lock retains exact archive digests and package integrity values. Release/tag
URLs are discovery references; immutable commits and hashes are comparison inputs.
A changed download hash requires investigation and a reviewed lock update, even
when a mutable tag or the archive service changed. No package-manager command,
installer, engine, hook, or vendor script was executed to obtain this evidence.

## Reproducing the comparison

Download `upstream.archive.url` from the lock through the operator's normal
download mechanism, then run:

```bash
python3 .agents/agentctl.py verify-source . --framework impeccable --archive /path/to/downloaded-source.tar.gz
```

The command validates the current inventory, verifies the entire archive hash,
and compares selected files inside the declared prefix. It rejects missing,
different, duplicate, linked or sparse selected members and bounds compressed
size, expanded scan, member size and count. It performs no downloads, extraction,
execution, or admission changes. The observed archive was 21,433,569 bytes; the
real comparison matched all 111 files.

The regular contract check stays offline and needs no cached archive. Static evals
exercise disposable adversarial archives and preserve checks for both actual
provider projections; they do not redownload upstream on every CI run. Re-run the
real source comparison when the selection changes. Historical integrity results
from PR #2770 remain inventory evidence, not proof for a later manifest digest.

## Vetting status

| Area | Evidence and remaining work |
|---|---|
| Instruction supply chain | The surface review screens both entry points, all 39 references, exported agents, metadata, launchers and selected browser data flows. It documents concrete permission, mutation and governance conflicts. It is not a line-by-line audit of every dependency. |
| License and attribution | Verified installer contains Apache-2.0 license text. Pinned source includes [LICENSE](https://github.com/pbakaus/impeccable/blob/cd12f8660e2dde57b9615c8a6b8ea674101f9cfc/LICENSE) and [NOTICE.md](https://github.com/pbakaus/impeccable/blob/cd12f8660e2dde57b9615c8a6b8ea674101f9cfc/NOTICE.md); the notice identifies MIT-derived platform references. Complete attribution, fonts, and the minified screenshot library's origin/notices remain unresolved. No blanket clearance is claimed. |
| Maintenance/security history | Release/source relationships are established. A full maintenance/advisory assessment of the selected source and engine dependency closure remains outstanding. |
| Executable closure | Five observed payload hashes are pinned with `reviewed: false`. Engine source/dependencies, build provenance, platform behavior, telemetry and updates still need review. A checksum is not execution approval. |
| Installer and launcher behavior | Verified npm shim resolves override, optional platform package, cache or download, then executes a subprocess. Download fallback checks a sidecar; other paths do not enforce the new lock. Installed skill launchers also allow sibling, home and PATH binaries. |
| Hooks | Tracked `.impeccable/config.json` sets `hook.enabled: true`, but no tracked native Impeccable hook registration was found. Local/account settings were not inspected. No hooks are enabled or disabled by this work. |
| Browser/privacy/network | Helpers handle tokens, source paths, screenshots, browser storage and page-derived requests. Review engine-side origin/auth/path controls, retention and egress before live mode. Use synthetic pages without authenticated user data. |
| Compatibility and value | No actual Claude/Codex invocation, resume, rotation, remote runner or paired UI evaluation has run in this slice. Listing an agent eval is not running it. |
| Ownership/admission | `steiner385` is the nominated maintainer, pending acceptance. Independent admission approval, an approved execution matrix, and an admitted rollback package are absent. Review of this verifier cannot supply those approvals. |

## Recommended pilot boundary

The next trial should use selected read-only `operate`, `clarify`, and `harden`
guidance as data, as described in the surface review. Do not invoke the complete
skill entry point: it requests engine context and can dispatch further commands.
Enforce tool/path/network limits outside the prompt, exclude exported roles and
downstream dispatch, and use disposable synthetic fixtures. Exclude executable
probes/downloads, live browser injection, hooks, automatic doctor repair, detector
suppression, and framework-defined shipping gates. Keep aesthetic advice advisory.

This trial is proposed, not enabled. Its future policy fixtures must detect
forbidden tool attempts and treat correctly denied actions as boundary success.
Before an executable pilot, establish the reviewed dependency/permission boundary,
verify real engine/harness behavior, and close the relevant surface findings.
Then obtain a separate admission decision backed by versioned evidence.

## Current decision and rollback

**Retain candidate state.** Source identity and observed payload integrity are
established. Executable trust, license completeness, policy compatibility, runtime
isolation, measured value, and admission authority remain unresolved. Existing
installations behave as before; no runtime deny or new SDLC gate is installed.

There is no approved rollback package yet. Immutable source and observed artifacts
provide reconstruction inputs, not an approved execution fallback. Before promotion,
test a reviewed withdrawal or rollback procedure preserving project-owned context
and historical evidence, with separate handling of host hooks, cached binaries and
browser artifacts.
