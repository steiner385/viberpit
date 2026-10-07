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
