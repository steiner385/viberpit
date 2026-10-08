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

## Observe-only applicability and GitHub review records

An optional `observation` object on a framework lock entry declares `mode`,
`trusted_github_reviewers` (an explicit list of GitHub login names), `concerns`,
and optionally `exceptions`. Each concern must already belong to that entry and
has `tier` (`deterministic`, `required_review`, or `advisory`), `include_paths`,
and `exclude_paths` arrays, plus an optional `mode` that overrides the entry's.
Patterns are repository-relative segment globs: `*` and `?` stay inside a segment;
`**` matches zero or more segments. Inclusions win. An exclusion is a skip only
when every changed file is explicitly excluded and the scope is complete and
nonempty. Backend paths without an explicit rule remain unknown; UI-facing API
changes can be included. ViberPit's initial UI policy includes `web/**` and
`server/api/**`, excludes `docs/**/*.md`, and trusts **no reviewers**.

The stdlib helper `.agents/framework_policy.py` is the single source for declaration
validation, routing, and canonical digests. Agentctl copies this sibling on
migration. The server loads its own installed helper, never executable code from
the repository being observed. The lock digest preserves the existing JSON
canonicalization (sort entries by id, sort object keys, compact separators).
The policy digest hashes the full contract with that JSON canonicalization; the
scope digest hashes the sorted, unique changed-path array. Git blob OIDs remain
separate identifiers. Policy and lock are pinned to one immutable default-branch
revision, so PR content cannot downgrade its own obligations.

The read-only GitHub adapter paginates files and reviews (100 items/page, at most
20 pages each) and checks PR base and head on every page and again after both
collections. Truncated, failed, or moving snapshots cannot authenticate a review.
GraphQL does not expose old filenames; renames make negative scope resolution
incomplete, while a known included destination still establishes applicability.

For a `required_review` concern only, a configured GitHub principal distinct from
the PR author can submit a review whose **entire trimmed body** is exactly:

````text
```viberpit-framework-review
{"version":1,"framework":"impeccable","concern":"ui-design","repo":"owner/repository","base_sha":"<40 hex>","head_sha":"<40 hex>","framework_digest":"<64 hex>","policy_digest":"<64 hex>","scope_digest":"<64 hex>","artifacts":[{"path":"docs/reviews/example.md","sha256":"<64 hex>"}],"findings":[]}
```
````

Use the current observation response's binding digests and PR revisions. A record
requires 1–20 repository-relative artifact references with SHA-256 hashes, and an
explicit findings list of at most 100 unique `{id, disposition}` objects. Allowed
completed dispositions are `fixed`, `accepted`, and `not_applicable`; an empty
findings list explicitly states no findings. Unresolved findings cannot produce
`review_recorded`. Unknown fields (including any body-supplied identity) invalidate
the record. References are attestations of artifact content hashes; this phase
does **not** fetch or independently verify artifact bytes or evaluate dispositions.

Identity, reviewed commit, state, submission time and edit metadata come from
GitHub. Only submitted COMMENTED/APPROVED reviews of the current head qualify.
Pending, dismissed, minimized, or CHANGES_REQUESTED reviews do not qualify. Edited
bodies require the editor to match the original reviewer. The latest relevant
record per principal and concern supersedes older success, including newer invalid
or dismissed records. Invalid timestamp ordering conservatively disqualifies that
principal. Ordinary approvals, prose examples and diagnostic POST claims never
count. Raw review bodies and artifact details are not returned or logged.

`review_recorded` means an authenticated principal attested to a bound scope. It
is not proof of model execution, independent agent identity, framework usage,
artifact availability, correctness, deterministic-check success, or admission.
Configured logins are policy authority, not cryptographic identity independent of
GitHub. No deterministic receipt collector exists yet. In `observe` mode all
three tiers remain non-blocking (see gate modes below), and
candidate/rejected/revoked admission states are never promoted.
Adding reviewer identities is a separately reviewed policy change.

### Gate modes and exceptions

`mode` is `observe` (never blocks), `evidence_required`, or `enforced`. A
non-observe, non-advisory concern holds an SDLC artifact merge or artifact-stage
advance when it applies to the PR and an applicable `required_review` has no
current recorded review, or the concern is `deterministic` (no trusted runner
receipts are ingested yet, so it is never satisfied). Unknown applicability
blocks only under `enforced`; it needs triage, never an automatic skip. A
`rejected` or `revoked` framework blocks only where its concern would already
block on scope (applicable, or unknown under `enforced`), even with a recorded
review; it never blocks out-of-scope changes. Advisory concerns never block, and
no risk tolerance bypasses a block. A failed default-branch policy read, or a
gating policy whose PR scope or reviews cannot be read, blocks as
`framework evidence unreadable`.

A review record is `stale` when it is well formed but bound to an older head,
base, framework digest, policy digest or scope, and `invalid` when it is
malformed, withdrawn (dismissed, minimized, changes requested), tied, edited by
someone else, or names another repository.

Each `exceptions` item (at most 50) has exactly `concern`, `pull_request`,
`owner` (a GitHub login), `reason`, `expires_at` (ISO-8601 with a timezone) and
`compensating_evidence`. Only one exception may name a given concern and PR. It
waives a missing, stale, unknown-scope or revoked obligation for that concern on
that PR until it expires. It does nothing when its owner is the PR author (a
trailing `[bot]` is ignored when comparing) or the author is unknown. It never
waives an invalid review record. Like the reviewer allowlist, exceptions come
from the default branch, so a PR cannot except itself by editing the lock.
ViberPit's own lock stays `observe`.

Two prerequisites apply before any repository moves a concern out of `observe`:

- Policy edits under `.agents/**` must be human-reviewed (CODEOWNERS plus branch
  protection). An exception's `owner` is an unauthenticated string, so an
  agent-authored lock PR could otherwise grant itself an exception or downgrade
  a mode once merged.
- An armed auto-merge is not re-checked when new commits land on the PR. The
  merge-boundary required check (plan Task 6) closes that gap; the artifact-stage
  advance re-check only catches it after merge.

### Regression pattern: review identifiers are not chronology

- **Symptom:** an older valid record survives a newer invalid one submitted in the same second.
- **Anti-pattern:** treating an opaque provider ID as a chronological timestamp tiebreaker.
- **Fix shape:** reject ties at the latest known timestamp; allow a strictly newer record to supersede an older tie. An unparseable timestamp cannot establish ordering and remains invalid.
- **Where to look:** review/attestation supersession and any timestamp-plus-ID comparison used to establish authority. A repository search found no other receipt-chronology instance; deterministic display ordering is a different use of IDs.
