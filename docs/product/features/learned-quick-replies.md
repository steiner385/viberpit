---
slug: learned-quick-replies
parent: session-workspaces
title: Learned quick replies
state: live
links:
  code:
    - server/quick_replies.py
    - server/api/quick_replies.py
    - web/src/api/quickReplies.ts
    - web/src/features/sessions/composer/Composer.tsx
    - web/src/features/sessions/list/SessionCard.tsx
---

# Learned quick replies

The quick-reply menus on session cards and in the composer share a personal set of
up to five short replies, learned from typed follow-up messages across sessions.
The authenticated user's identity owns the history; agent accounts, repositories,
and session hosts do not divide it, and other users' messages do not contribute.

Semantically equivalent messages share a shape: “continue” and “please continue”
contribute two observations to the same continuation shape. A model groups the
observations and supplies concise reusable labels. The server counts the validated
members itself and ranks the shapes by frequency. Approval and continuation remain
distinct, and negated instructions must not be grouped with affirmative ones.
There is no fixed reply vocabulary.

Only successfully submitted, directly typed replies contribute. Choosing a learned
reply or an AI suggestion does not reinforce its ranking. Automated sends, messages
with attachments, detailed task instructions, and sensitive or identifying content
are excluded. Curation runs separately from prompt delivery; a model failure must
not prevent a message from reaching its agent.

Learning starts with inputs submitted after this feature is enabled. Existing
coding transcripts do not reliably identify the human author of each message, so
they are not imported into personal history. Until useful shapes have been learned,
the menu explains that it is learning instead of showing invented defaults. An
existing successful ranking remains available if the model is temporarily unavailable.

The current session's AI suggestions remain a separate menu section: they answer
what to do next in that session, while learned replies reflect the user's habits
across sessions.

The corpus retains at most 200 candidate replies from the last 90 days per user.
A read can refresh the ranking at most once an hour, using the existing Anthropic
Haiku helper-account selection. A configured, authenticated helper account is
required to learn new shapes; temporary model failures retain the previous ranking.
The helper has no built-in or MCP tools and does not persist a native transcript.

Validation includes a real native helper call over synthetic replies: three
continuation variants grouped together, two approvals together, two recommendation
questions together, and a negated continuation stayed separate. A specific task
request was omitted. Model output is validated before its membership counts affect
the menu, including fenced JSON and duplicate/out-of-range member rejection.
