// The Dev servers section's copy (D-18), apart from the component so tests and the
// viewer share one source and the component file exports components only.
export const DEV_SERVERS_COPY = {
  heading: 'Dev servers',
  empty: 'No dev servers detected — when the session starts one (e.g. npm run dev), it shows up here.',
  reachable: 'Also reachable from your network',
  noPreview: 'No preview open',
  failed: "Couldn't open this dev server.",
  unreachable: "Couldn't reach the server to open the preview.",
  errors: {
    PREVIEW_NO_DISPLAY: "No preview open — this session's host has no display free for it.",
    PREVIEW_SESSION_NOT_LIVE: "This session isn't running. Send it a prompt to start it, then try again.",
    PREVIEW_LAUNCH_FAILED: "The preview browser didn't start. Try again in a moment.",
    DEV_SERVER_NOT_DETECTED: "This dev server isn't running any more.",
    SESSION_CONTROL_HUMAN_ONLY: 'Only a person can open a preview.',
    daemon_outdated: "This session's host needs an update before it can open previews.",
  } as Record<string, string>,
}
