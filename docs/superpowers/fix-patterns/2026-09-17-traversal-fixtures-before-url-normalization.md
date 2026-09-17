# Traversal fixtures must reach the validator

## Symptom

Two unchanged API tests passed without a frontend build but failed with 405 after
`web/dist` existed: PR-comment traversal and Library skill CRUD. The full backend
gate exposed the difference after the frontend build.

## Anti-pattern

Literal `..` URL segments are normalized by the HTTP client's base-URL join before
they reach ASGI. Accepting 404 as traversal rejection can therefore test a missing
route rather than the security validator. With built assets, the SPA mount catches
that missing route and rejects POST/PUT with 405 instead.

## Fix shape

Encode the dot segment (`%2e%2e`) so it survives the client and is decoded at the
route boundary. Assert the validator's exact status and error, plus no external
write or filesystem change. Do not broaden accepted statuses to include 405 or
remove built assets to hide the problem. Production routing needs no change.

## Where to look

Search API test request URLs for literal `../` and comments claiming traversal
coverage. `test_github_write_endpoints.py` now requires the repo validator's 422
and zero GitHub writes. `test_library_api.py` requires the skill validator's 400
and unchanged shared files. Its nested-command sibling previously substituted a
space-containing name for traversal; it now exercises an encoded parent segment
as well, preserving the existing invalid-name case and asserting unchanged files.

The repository sweep found remaining parent-segment samples in direct Library
validator/mutator/scan tests, assistant tool arguments, JSON request bodies, and
repository-name validation. Those do not put literal dot segments in HTTP request
URLs and are not affected by client URL normalization.

## Verification

Both original failures reproduced against the unchanged test files with built
assets present. The relevant production routes and SPA mount were unchanged from
HEAD. A transport probe confirmed the client rewrites `/api/prs/acme/../42/comments`
to `/api/prs/42/comments` and `/api/library/skills/../evil` to `/api/library/evil`;
encoded segments remain intact. An isolated app returned 404 without the SPA mount
and 405 with it for the normalized requests. All 45 tests in the two affected test
files pass with built assets present; focused Ruff passes.
