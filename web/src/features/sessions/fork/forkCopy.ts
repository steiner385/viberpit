/**
 * What "Fork from here" (D-8) says — THE one table of fork copy. Pure data with
 * no imports, loaded on first use (`useForkFrom`) so it stays off the entry chunk.
 *
 * The codes are the server's (`server/sessions/fork.py:FORK_CODES`,
 * `viberpit/worktrees.py:FORK_WORKTREE_CODES`, `daemon_outdated` and the source
 * account's usage 409); a code missing here falls back to the server's own message.
 */

/** The success toast: the child's files are the source's worktree NOW, not at the cut. */
export const FORK_SUCCESS = 'Forked — files start from the current worktree, not this message'

/** `/fork` with no settled reply to fork at — said instead of sending a request. */
export const FORK_NOTHING_TO_FORK = 'Nothing to fork yet — /fork needs a finished reply'

/** A failure whose body carries no usable message. */
export const FORK_FAILED_FALLBACK = "Couldn't fork this session"

export const FORK_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  FORK_UNAVAILABLE: "This session can't be forked",
  TURN_NOT_FORKABLE: "This turn can't be forked — it is still running or ended without a reply",
  PREFILL_TOO_LONG: 'That message is too long to carry into a fork',
  FORK_AT_CAPACITY: 'Too many sessions are running — fork once one finishes',
  FORK_NEEDS_APPROVAL: "This session's model needs approval, so it can't be forked",
  FORK_OWNED_BY_AUTOMATION: 'An automation is running this session — fork it after the run finishes',
  FORK_OWNERSHIP_STALE: 'An automation started on this session — fork it after the run finishes',
  FORK_UNTRACKED_TOO_LARGE: 'The untracked files are too large to copy into a fork',
  FORK_NOT_A_REPO: "This session's folder isn't a git repository, so it can't be forked",
  FORK_NO_HEAD: "This session's repository has no commits yet, so it can't be forked",
  FORK_DEST_EXISTS: "Couldn't fork — the new worktree's folder already exists",
  FORK_UNSAFE_CONFIG: "This repository's git config uses includeIf, so it can't be forked safely",
  FORK_APPLY_FAILED: "Couldn't carry the uncommitted changes into the fork",
  FORK_DISK_FULL: "Couldn't fork — the disk is full",
  FORK_FAILED: "Couldn't create the fork's worktree",
  daemon_outdated: 'The session runner needs an update before it can fork — try after the next rollout',
  // Not `ALL_LIMITED_MESSAGE`: a fork runs on the SOURCE's account, so this is that
  // one account at its ceiling, not "every account". `accounts_need_reauth` reads
  // `REAUTH_MESSAGE` (createSessionFailure) — see `useForkFrom`.
  all_accounts_limited: "The session's account is at its usage ceiling — fork after it resets",
}
