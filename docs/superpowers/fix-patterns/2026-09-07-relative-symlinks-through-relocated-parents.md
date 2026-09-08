# Relative symlinks through relocated parents

## Symptom

A newly created CLI alias exists, but executing it reports “No such file or
directory”. The same executable works from another alias on the same host.

## Anti-pattern

Computing a relative symlink with only lexical source and destination paths.
`..` traverses the physical parent after symlink resolution. On dobby,
`~/.local/bin` is relocated under a cache mount, so `../../GitHub/...` does not
reach `~/GitHub/...` even though a string-level path calculation says it should.
The source `.venv` is also a symlink; file-existence and executable-bit checks alone
prove nothing about the destination link.

## Fix shape

Keep an existing relative spelling when resolving it from the destination parent
reaches the intended source. Otherwise compute the relative target between the
resolved source and resolved destination parent. Keep conflict preflight strict;
repair an already-created wrong alias only by matching its recorded old target,
replacing that alias atomically, and recording the correction separately. Never
move or rewrite the real executable to fix an alias.

Regression coverage must include a relocated destination parent and a symlinked
source directory. Verify the resulting alias against the real executable on the
host, not just `is_symlink()` in a fixture.

## Where to look

`scripts/viberpit-host-aliases.py:link` and `tests/test_host_aliases.py`. The scripts
sweep found no other `relpath` implementation requiring the same correction.
