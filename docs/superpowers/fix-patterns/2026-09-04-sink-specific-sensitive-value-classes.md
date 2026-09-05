# Fix pattern: one sensitive-value set cannot serve every sink

## Symptom

A post-run credential scan reports a leak even though the matched value is expected runtime state,
or a redaction pass misses a value because a storage-specific allowlist was reused for output.

## Anti-pattern

Treating every sensitive or non-public value as one interchangeable collection and passing that
collection to scanners with different policies. Values such as access tokens, refresh tokens,
account identifiers, and native session identifiers do not have identical rules: a native thread
identifier belongs in host-local runtime state but must not appear in logs or public evidence,
while runner-visible authentication material must not persist in that state at all.

## Fix shape

Define sink-specific value classes at the source of truth. Output redaction receives every secret
and non-public identifier. Runner-home scanning receives only values forbidden from runner storage,
including their historical generations. Keep refresh-token and native-identifier histories separate
so merge or rollover logic cannot silently widen the wrong scanner set. Name each collection for its
policy rather than its transport origin.

## Where to look

- Exact-value log, stderr, exception, or evidence redactors.
- Filesystem and database leak scanners.
- Credential-generation merge/history helpers.
- Remote scanners whose allowlisted input crosses stdin.
- Tests that use one generic `values`, `secrets`, or `identifiers` tuple for several sinks.
