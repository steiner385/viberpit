# Postgres fixtures must isolate destructive DDL

## Symptom

A focused test file passes alone, then fails beside the full-schema Postgres suite
because dropping a shared parent table encounters foreign keys from another test.
Parallel workers can also erase each other's state.

## Anti-pattern

A new fixture reads a database URL directly and creates/drops a subset of tables in
its default schema. This bypasses the existing test-database guard and treats a
shared database as if the fixture owned it exclusively.

## Fix shape

Resolve the URL through the canonical `pg_url` fixture before constructing an
engine. Give each fixture invocation a generated unique schema and pin every
pooled connection's search path to that schema, without a public-schema fallback.
Create tables there and drop only that owned schema in finally; always dispose the
engine. SQLite uses its existing per-test temporary database.

## Where to look

Search test fixtures for direct test-URL reads, create_async_engine, and drop_all.
The ownership fixture was the only new Postgres fixture in this change; the launch
and identity test modules introduce no sibling Postgres fixture. The existing
session-scoped pg_engine intentionally owns the guarded shared full-schema suite;
new fixtures must not mutate that schema.

Verification reproduced the automation_event foreign-key failure with pg_engine
preloaded, then passed that same probe after isolation. Ownership and Postgres
compatibility/schema suites also passed together under two xdist workers with
loadfile scheduling. A non-test database name was rejected by pg_url before any
connection attempt.
