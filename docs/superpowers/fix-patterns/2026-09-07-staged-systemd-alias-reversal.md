# Staged systemd alias reversal

**Symptom:** A stopped-host migration aborts after unlinking its compatibility alias,
before moving the canonical unit file. Rollback can encounter the same gap.

**Anti-pattern:** Requiring every name of a stopped service to remain `loaded`
between filesystem mutations. systemd garbage-collects inactive units; a real
`systemctl show` then reports the temporarily absent alias as `not-found` even
though its stopped owner still exists under the other name.

**Fix shape:** Query every known identity. Reject active units, nonzero main/control
PIDs and unexpected load states. For a migrated alias pair, require at least one
loaded identity while allowing its inactive, PID-free counterpart to be absent.
Keep non-migrated watchdog guards strict. Validate the initial filesystem before
mutations and apply the same ownership guard during journaled rollback.

**Where to look:** Staged unit renames, filesystem alias swaps, migration preconditions
and rollback guards. The runtime unit resolver and shell health selector already
fall back on absent names; the repository audit found no matching guard defect.

Verification included a harmless real systemd fixture on dobby: after unlinking the
new alias, its load state became `not-found`; after moving the old file, recreating
the old alias and reloading, both names resolved to the new canonical identity.
Offline regressions cover both apply and rollback, missing pairs, active/PID-bearing
old aliases and missing retired watchdogs.

The same review found that a durable journal needs its newly created bundle's
**parent directory** fsynced before live mutations, as well as the journal file and
bundle itself. Other existing file-persistence helpers (`ledger` and
`account_registry`) have broader durability questions outside this host migration;
they were not changed without a matching migration failure.
