---
name: github-via-graphql
description: Query pull requests, issues, checks, commits, and repository metadata through batched GitHub GraphQL calls with the gh CLI. Use for GitHub reads; keep unsupported mutations on their normal gh commands.
---

# GitHub via GraphQL

1. Resolve the repository owner and name from `git remote -v`; do not hardcode them
   from memory.
2. Prefer one `gh api graphql` query that requests all required fields over multiple
   `gh pr view`, `gh pr list`, or `gh issue view` calls.
3. Request only fields needed for the decision and handle pagination when the result
   can exceed the selected page size.
4. Use normal `gh` mutation commands when they are the supported path, including
   `gh pr create`, `gh pr merge`, `gh issue create`, and `gh issue close`.
5. Never send an entire GET response back in a mutation. Construct the smallest input
   containing only fields the mutation accepts.
6. If `gh` authentication unexpectedly fails, check whether a stale `GITHUB_TOKEN`
   environment variable shadows the keyring login before changing credentials.

GitHub reads are non-mutating. Obtain the authorization required by the active task
before creating, closing, merging, or otherwise changing external state.
