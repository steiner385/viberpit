# Framework lock schema 1

The optional `frameworks.lock` path in `contract.json` selects a JSON manifest.
Projects without this property retain their existing contract behavior. The
stdlib validator in `agentctl.py` is authoritative; `check` reads local files,
never installs dependencies, runs hooks, or grants runtime admission.

The manifest has `schema_version: 1` and a `frameworks` array. Each entry has:

| Field | Meaning |
| --- | --- |
| `id`, `owner` | Unique framework identifier and nonempty accountable owner. |
| `state` | `candidate`, `sandbox`, `pilot`, `approved`, `rejected`, or `revoked`; declarative inventory metadata, not an authenticated decision. |
| `upstream` | `repository` and a full lowercase 40-character `commit` SHA. An unknown commit is `null` with a nonempty `reason`; it cannot be promoted. |
| `concerns` | Unique strings naming workflow responsibilities; two selected entries cannot own the same concern. |
| `roots` | Nonempty list of repository directories whose entire packaged contents are inventoried. |
| `additional_files` | Optional explicit file paths outside those roots, for individually selected agent definitions. Each needs a `files` hash. |
| `runtime_paths` | List of paths strictly inside roots excluded from packaged hashes, such as ignored download/cache directories. Pilot/approved entries cannot contain un-inventoried runtime files. |
| `files` | Nonempty mapping of every packaged repository-relative filename to its lowercase SHA-256. |
| `provider_projections` | Nonempty mapping of provider to `{root, sha256}`. Root must be an inventory root. Distinct Claude/Codex builds keep distinct digests. |
| `dependencies` | List of objects with `id`, `kind`, optional `path`, `pin`, `sha256`, and `reviewed`. Local paths must have matching inventory hashes. |
| `compatibility` | List of strings recording verified combinations or explicit gaps; an empty list makes no compatibility claim. |
| `admission` | `null` until reviewed; pilot/approved requires nonempty distinct `author` and `reviewer` plus an existing repository-local `report`. These are reviewable declarations, not authenticated attestations. |
| `unresolved_references` | Optional mapping from `source/path.md:target` to a nonempty explanation. Allowed for candidate inventory; blocks pilot/approved. |

Additional descriptive metadata is permitted and contributes to the declaration
digest. Paths must be canonical relative paths: no absolute paths, backslashes,
empty components, `.` or `..`. Packaged symlinks are rejected, including dangling
links and directory aliases; escaping symlinks are rejected even in runtime paths.

Markdown inline and definition links to local files must resolve to locked files.
Fenced/inline code, URL links, and fragment-only anchors are excluded. This is a
bounded inventory check, not a complete Markdown parser or semantic reference
analyzer: prose tool names, dynamically constructed paths, HTML, and shell imports
still require human dependency-closure review.

Pilot/approved additionally requires an immutable upstream SHA, no unresolved
references, and pinned/reviewed dependencies with SHA-256. Executable files
(execute permission, shebang, or case-insensitive script/binary extensions including
`.cmd`/`.bat`) need dependency declarations. A local hash alone does not establish
upstream origin or executable safety. Candidate dependencies may record unverified
pins and missing hashes explicitly; validation success does not approve them.

`framework_tree_hash(path, runtime_paths)` preserves the contract's existing tree
hash format: SHA-256 over sorted relative filename bytes followed by each file's
bytes, excluding declared runtime paths. `framework_digest(root, contract)` hashes
canonical JSON of the declaration with frameworks sorted by ID. It is stable
across JSON object ordering and changes when metadata changes; it is not a runtime
attestation. Validate the inventory before using that declaration digest.

Run `python3 -m unittest discover -s .agents/tests -v` for disposable acceptance
fixtures. `evals/cases/framework-lock-integrity.yaml` runs those fixtures and the
actual contract check through the existing static eval runner. Evals CI includes
`.agents/**` on both push and pull request triggers.

## Validator regression pattern

- **Symptom:** a non-string or whitespace-padded identifier passes a required
  metadata check, or a looping symlink crashes validation.
- **Anti-pattern:** truthiness used instead of string validation; path resolution
  attempted before checking symlink components.
- **Fix shape:** require nonempty, trimmed string identifiers and pins; reject
  symlink components before resolution and return a diagnostic for resolution
  errors. Keep free-form explanatory prose distinct from identity fields.
- **Where to look:** framework, owner, concern, provider, dependency, admission
  identity fields, and paths entering `framework_path` or Markdown resolution.

## Offline upstream source comparison

An optional `upstream.archive` object contains exactly `url`, `sha256`, and
`prefix`. For this initial GitHub adapter, `repository` must identify a GitHub
owner/repository and `commit` must be a full lowercase SHA. The archive URL must
be `https://codeload.github.com/<owner>/<repository>/tar.gz/<commit>`. It is an
informational coordinate, never fetched by the validator. `sha256` hashes the
whole compressed archive; `prefix` is a canonical relative directory ending in
`/` (for example `impeccable-<commit>/`). Projects without archive metadata retain
their existing `check` behavior.

After obtaining the archive separately, run:

```sh
python3 .agents/agentctl.py verify-source . --framework impeccable --archive /path/source.tgz
```

This validates the current lock and local inventory first, hashes the archive
before parsing, then compares every selected locked file at `prefix + path`.
Missing, changed, duplicate selected members, links, and sparse selected files
fail. The command never extracts, installs, executes, fetches, or edits files.
Unselected members are ignored for content comparison; their sizes still count
against resource limits. This is not a complete transitive dependency audit or
an authenticated publisher signature, and does not change admission state.

Limits are 64 MiB compressed, 256 MiB decompressed scan (including ignored content
and trailing data), 32 MiB per tar member, and 10,000 members. Raising these limits
requires a reviewed validator change. Tar metadata and gzip integrity are checked
within the bounded scan. The caller must supply a trusted lock declaration:
changing both an archive and its declared digest is not evidence of upstream trust.

`evals/cases/framework-impeccable-projection-integrity.yaml` exercises disposable
source fixtures offline and requires exactly one installed Impeccable selection
with both Claude and Codex projections. A real downloaded archive comparison is separate evidence;
the static fixtures alone do not establish provenance of installed Impeccable.

### Archive diagnostic fix pattern

- **Symptom:** malformed compressed data produces a traceback instead of a contract
  diagnostic, including a malformed gzip member concatenated after a valid tar.
- **Anti-pattern:** catching only tar, filesystem, and EOF errors while a gzip
  reader can also raise `zlib.error` during parsing or trailing-data validation.
- **Fix shape:** normalize decompressor errors at the same boundary as archive
  errors, and test both initial corruption and corruption after valid content.
- **Where to look:** compressed archive readers and their final drain loops.
  The sibling search found `server/library/snapshots.py` uses a separate tar
  reader without this diagnostic boundary; snapshot error handling is outside
  this framework admission change and was not changed.

## Offline executable dependency comparison

After obtaining an artifact separately, compare it to an existing dependency:

```sh
python3 .agents/agentctl.py verify-dependency . --framework impeccable \
  --dependency impeccable-engine-linux-x64 --artifact /path/impeccable-linux-x64
```

The command validates the current framework lock and local inventory first, then
selects the exact framework/dependency IDs. It requires a lowercase SHA-256 and an
exact version (optionally prefixed, for example `engine-v0.1.5`), 40/64-character
lowercase hexadecimal revision, or `vendored-content` with an inventoried `path`.
Version labels are descriptive and can be retagged upstream; the SHA-256 provides
the content pin. Floating ranges, branch names, and unknown hashes cannot be
verified. Normal `check` continues to allow explicitly unverified candidates.
An optional dependency `bytes` field must be a positive integer (not a Boolean)
and match the artifact exactly.

Artifacts must be regular files with no symlink components. The verifier inspects
before opening, uses nonblocking/no-follow flags where supported, and hashes a
single opened descriptor in bounded chunks. The maximum artifact size is 128 MiB;
metadata checks reject observed replacement or modification during reading.
These checks are best-effort race detection, not a filesystem snapshot or a
runtime launcher policy. Verification never downloads, executes, installs,
extracts, probes PATH, or edits the artifact or declaration. Candidate and
`reviewed: false` dependencies can be compared; success explicitly grants no
admission, safety approval, publisher authentication, or source-to-binary proof.
The lock must itself be trusted: replacing both its hash and artifact defeats
any claim of upstream identity. `framework-dependency-integrity` exercises these
boundaries using disposable offline fixtures through the existing eval runner.
