# `claude mcp login --no-browser` I/O Contract

Probed: 2026-08-31  
Binary: `/home/tony/.local/bin/claude` v2.1.251  
Servers probed: `plugin:railway:railway` (HTTP), `plugin:sentry:sentry` (HTTP), `plugin:aws-core:aws-mcp` (stdio)  
Account: `tonystein85_gmail.com`

---

## Key finding: server-name argument

`claude mcp login` expects the **exact server key** as it appears in the account's
`claude_desktop_config.json` — the name shown by `claude mcp list`. Plugin servers registered by
the ccswitch plugin system have the form `plugin:<plugin-name>:<server-name>` (e.g.
`plugin:railway:railway`). Passing just `railway` produces:

```
No MCP server named "railway". Configured servers: claude.ai Figma, …
```

The `_needs_auth_by_server()` map returns the short name (`railway`), not the full key. Task 4 must
resolve the short name to the full `plugin:<ns>:<name>` key before invoking `claude mcp login`.

---

## HTTP / SSE server — output shape (`--no-browser`)

### Raw bytes (railway, PTY)

```
b'Starting authentication for "plugin:railway:railway"\xe2\x80\xa6\r\n'
b'Visit this URL to authorize:\r\n'
b'  \x1b]8;;https://backboard.railway.com/oauth/auth?<REDACTED>\x07'
b'https://backboard.railway.com/oauth/auth?<REDACTED>'
b'\x1b]8;;\x07\r\n\r\n'
b'Waiting for authorization\xe2\x80\xa6 (^C to cancel)\r\n'
```

### Annotated

Line 1 — preamble:
```
Starting authentication for "<server-key>"…\r\n
```

Line 2 — label (no URL yet):
```
Visit this URL to authorize:\r\n
```

Line 3 — URL line (indented two spaces, with OSC 8 hyperlink wrapping):
```
  \x1b]8;;<url>\x07<url>\x1b]8;;\x07\r\n
```
The URL appears **twice** in this line:
1. Inside the OSC 8 hyperlink params: `\x1b]8;;<url>\x07`
2. As the visible display text: `<url>`
3. Closed with `\x1b]8;;\x07` (empty OSC 8 = close hyperlink)

A simple regex over the raw bytes can extract the first plain-text `https://` occurrence.

Line 4 — blank: `\r\n`

Line 5 — block line (process hangs here awaiting redirect):
```
Waiting for authorization… (^C to cancel)\r\n
```

After line 5 the process **blocks indefinitely** listening on `localhost:3118/callback`.
It does NOT emit a redirect-paste prompt over the PTY — that phase requires the user to
complete the OAuth browser flow, which writes to the callback listener, not stdin.

### Sentry reference (confirms consistent shape)

```
Starting authentication for "plugin:sentry:sentry"…\r\n
Visit this URL to authorize:\r\n
  \x1b]8;;https://mcp.sentry.dev/oauth/authorize?<REDACTED>\x07https://mcp.sentry.dev/oauth/authorize?<REDACTED>\x1b]8;;\x07\r\n
\r\n
Waiting for authorization… (^C to cancel)\r\n
```

Identical structure, different host/path.

---

## Non-PTY fallback (no `--no-browser`, stdin not a terminal)

When `--no-browser` is omitted and stdin is not a TTY, the process emits the URL then
immediately fails with:

```
Couldn't complete authentication for "<server-key>": stdin isn't a terminal, so
authentication can't be completed here. Re-run in an interactive terminal — e.g. `ssh -t`
— and paste the redirect URL when prompted.
```

This is the **non-browser fallback error path** — it implies a paste prompt exists in
interactive (TTY + browser) mode, but is gated on the local HTTP callback listener receiving
a redirect. It is not emitted via `--no-browser`.

---

## Without `--no-browser` (PTY, interactive)

Line 2 changes to:
```
If the browser didn't open, visit:\r\n
```
Rest of the output is identical. The process still blocks on `Waiting for authorization…`.
The redirect-paste prompt is reached only after the OAuth completes in the browser —
the ccswitch use-case (headless) uses `--no-browser` and therefore never reaches it.

---

## stdio server — output shape

`plugin:aws-core:aws-mcp` (transport: stdio) fails immediately:

```
\x1b[0m\x1b[31m"plugin:aws-core:aws-mcp" doesn't support OAuth login — it's only available for HTTP and SSE servers.\x1b[0m\r\n\x1b[?25h
```

Decoded (stripping ANSI):
```
"plugin:aws-core:aws-mcp" doesn't support OAuth login — it's only available for HTTP and SSE servers.
```

Exit code: 1. No URL is emitted. The process exits immediately.

---

## URL structural shape (PKCE params redacted)

Railway:
```
https://backboard.railway.com/oauth/auth?<state=…&code_challenge=…&…>
```

Sentry:
```
https://mcp.sentry.dev/oauth/authorize?<response_type=code&client_id=…&code_challenge=…&…>
```

The redirect URI is always `http://localhost:3118/callback` (literal in the URL).

---

## Heuristics for the parser (Task 4)

1. **URL extraction**: scan raw PTY bytes for the first occurrence of `https://` that is NOT
   inside the OSC 8 escape (`\x1b]8;;`). The display-text copy appears immediately after
   the `\x07` separator and before `\x1b]8;;\x07`. A safe regex over stripped bytes:
   `re.search(rb'https://[^\x07\r\n\x1b ]+', buf)` captures the display-text URL.

2. **"awaiting" sentinel**: the line `Waiting for authorization` (UTF-8 `…` = `\xe2\x80\xa6`)
   signals the process is blocked and the URL is fully emitted. Poll until this line appears,
   then extract.

3. **stdio servers**: when the process exits with code 1 immediately and output contains
   `doesn't support OAuth login`, degrade gracefully — skip auth, surface a user-facing error.

4. **Unknown server name**: output contains `No MCP server named` — the short name passed to
   `_needs_auth_by_server()` must be resolved to the full plugin key before invoking
   `claude mcp login`.

5. **Deadline**: 30 s is sufficient; URL appears within ~1–2 s of process start.
