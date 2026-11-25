"""Remote sessions: Claude Code Remote Control sessions started OUTSIDE ViberPit (an
interactive ``claude`` with ``/remote-control``, another SDK host, another machine) listed,
watched and driven from ViberPit (plan 2026-11-25-remote-sessions-reverse).

ViberPit does not own these sessions: no rotation, respawn or archive — only list, watch,
send a prompt, and answer a permission or question. The sessions API is a private,
undocumented claude.ai surface (constitution I), so everything sits behind the
``VIBERPIT_REMOTE_SESSIONS_ENABLED`` kill switch (default off) and degrades to
"unavailable" on any unexpected status or shape. The account's claude.ai login is used only
on the refresh-authority host, the primary daemon (constitution II); the edge reaches it
through daemon RPC.

* ``client``  — the HTTP client and the account credentials it sends.
* ``listing`` — which listed sessions ViberPit shows, and their summary.
* ``translate`` — claude.ai's stream-json events into ViberPit's wire events.
* ``service`` — the poller, the per-session watchers and the send / answer calls.
* ``view``    — the edge's live view of one watched session (the ``/ws`` attach).
"""
