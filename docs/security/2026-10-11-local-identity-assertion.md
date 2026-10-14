# Threat note: a local process asserting the operator's Tailscale identity (#2980)

Status: decided and implemented 2026-10-11. Refs #2952 (always-allow follow-ups).

## The gap

`server/auth/identity.py::verify_identity` believes a `Tailscale-User-Login` header from a
trusted peer: loopback, this node's own tailnet addresses (#2134), or
`VIBERPIT_TRUSTED_PROXY_IPS`. The `tailscale serve` hop and any other process on the host look the
same by peer IP. A process that connects to `127.0.0.1:8787` with the operator's login and no
`X-Viberpit-Via` header was treated as "a person in the UI" by `session_actions.require_human`.

Since always-allow rules (#2960) that is worth more than one card. A `global` rule auto-allows a
pattern for every session on every host. Profile patterns become a session's tool allowlist at
spawn. So a session in `full-auto` (or one already allowed to run `curl`) could widen what
*other* sessions do unattended. The safety assessor still refuses `refused`-level patterns
(`Bash(rm:*)`, `Bash(git push:*)`). Anything rated `ok` or `warn` was reachable.

## Decision: attest the client socket's owner (a variant of option 1)

The front-end hop and a session's `curl` differ in one thing the kernel records: **who owns the
client socket**. dobby has two front-ends, both shipped in `deploy/`:

- `tailscale serve` on `:8443`, run by tailscaled as **root**. It stamps `Tailscale-User-Login`.
- nginx on `:443` (`deploy/viberpit-tailnet.nginx`), whose workers run as **www-data**. It
  strips the identity headers and sets `X-Real-IP`, so identity comes from `tailscale whois`.

Agent sessions, the edge and everything else the operator starts run as the operator's user
(uid 1000 on dobby). Checked on dobby (tailscale 1.102.5): established connections to `:8787` in
`/proc/net/tcp` show uid 0 for the serve hop and uid 1000 for local clients. rocky also runs
tailscaled as root, and has no edge or serve config today.

### The peer must be the socket, not `scope["client"]`

uvicorn's CLI defaults to `--proxy-headers` with `--forwarded-allow-ips=127.0.0.1`. For any
loopback caller it rewrites `scope["client"]` from `X-Forwarded-For`, **port included**.
`tailscale serve` sends that header, which is why a browser on dobby showed up as dobby's own
tailnet IP (#2134). It also means a local process chooses the `request.client` the app sees.
It can name a remote device, which has no socket here and looked remote. Or it can name
`127.0.0.1:<the serve hop's port>`, a root-owned socket. Both bypassed the first cut of this
gate (caught in review).

Turning proxy headers off outright is not an option. The `ccas1:` assistant token and the
internal token are loopback-only *because* the rewrite turns serve-proxied tailnet traffic into
a non-loopback `request.client`. So the app now owns the rewrite:

- `identity.TransportPeerMiddleware` (outermost in `create_app`) records the socket peer under
  `scope["viberpit.transport_peer"]`. It then runs uvicorn's own `ProxyHeadersMiddleware` with
  uvicorn's trusted-host default (`FORWARDED_ALLOW_IPS`, else `127.0.0.1`), so `request.client`
  and every check reading it behave exactly as before.
- `deploy/viberpit.service` runs uvicorn with `--no-proxy-headers`, so the app's rewrite is the
  only one.
- If uvicorn still rewrites first (a host whose installed unit predates this), the recorded
  peer is itself an `X-Forwarded-For` entry: port 0, or equal to one of the header's entries.
  The gate treats that as unattested, fails closed, and logs a throttled WARNING naming the flag.

### Socket ownership is necessary, not sufficient

A local process can also go *through* a front-end: `curl https://dobby.<tailnet>:8443` from
dobby. tailscaled then owns the socket to the edge **and** stamps the login of dobby's own node,
which is the operator. nginx does the same via `tailscale whois` of `X-Real-IP`. So once the
socket is attested as a front-end, the gate also reads the **original client** that front-end
stamped: tailscaled's `X-Forwarded-For` (last entry; tailscaled sets it to the source address)
for a root socket, nginx's `X-Real-IP` (nginx overwrites it) for a www-data socket. That client
must be **another tailnet device**: inside Tailscale's ranges (`100.64.0.0/10`,
`fd7a:115c:a1e0::/48`), not loopback, not one of this node's tailnet addresses, and not bindable
on this host. Anything else is local. That includes a LAN or docker address of this host, which
reaches nginx's `0.0.0.0:443` listener.

One more kernel detail: a client that sends its request and then closes leaves a *timewait
minisocket*. `/proc/net/tcp` prints it with **uid 0 and inode 0**, which looks like tailscaled.
Only an ESTABLISHED row (`st` = `01`) with a live inode counts as an owner. Any other matching
row makes the socket unattributable.

And a client can *reset* its connection after sending, which frees the 4-tuple at once. A new
tailscaled connection could then take that tuple before the handler looks. So
`TransportPeerMiddleware` also records the **inode of the edge's own accepted socket**: an
`fstat` of the uvicorn transport, with no `/proc` read. The lookup requires the edge-side row
with that inode to still be ESTABLISHED. The tuple therefore belongs to this live connection, or
nothing is attributed.

`identity.attested_front_end_hop(conn)` (called by `human_gate_via`) finds the recorded peer's
socket in `/proc/net/tcp{,6}` by its 4-tuple and decides:

| Peer socket on this host | `via` for the gate |
| --- | --- |
| any row below that yields `"local"`, with `VIBERPIT_RULES_TRUST_LOCAL_HOST=true` | `"local-trusted"`: admitted, recorded as such (spec 2026-10-14) |
| established, owned by one front-end, original client is another tailnet device | unchanged (`None`, i.e. human) |
| established, owned by one front-end, original client is this host (any address) or not a tailnet address | `"local"`: refused |
| the edge's own socket for this connection is gone (reset, closed) or its inode is unknown | `"local"`: fails closed |
| owned by a front-end, but its origin header is missing or this node's addresses are unknown | `"local"`: fails closed |
| owned by any other uid (a session, a shell, a cron job) | `"local"`: refused |
| a timewait/orphaned row (closed socket), or two owners for one tuple | `"local"`: fails closed |
| recorded peer came from `X-Forwarded-For`, or none was recorded | `"local"`: fails closed |
| not found, but the peer is loopback or a self-tailnet IP | `"local"`: fails closed |
| `/proc` unreadable | `"local"`: fails closed |
| not found, peer is remote | unchanged: a remote tailnet node verified by tailscale or by a `VIBERPIT_TRUSTED_PROXY_IPS` hop |

Every human-only write takes its `via` from this function:

- `POST /api/sessions/{sid}/permissions/{perm_id}/always`, `POST /api/permission-rules/preview`,
  and `PATCH`/`DELETE /api/permission-rules/{id}`;
- the legacy `decision: "always"` answer, over HTTP (`POST /api/sessions/{sid}/permissions/{perm_id}`)
  and over WS (`permissionResponse`);
- profile pattern writes (`POST`/`PATCH /api/permission-profiles[/{id}]` carrying `patterns`),
  whose `require_human` check #2997 added. It read `request.state.via` and now reads
  `human_gate_via`.

Reads, and plain allow/deny answers, are untouched. They never read `/proc` (about 6 ms on
dobby), so the lookup stays off the per-request path.

### Why not the other options

1. **Unix-socket / dedicated-port listener for serve.** A socket file or port reachable by the
   edge's user is reachable by every session too. They all run as that same user, so file
   permissions cannot separate them. The socket-owner check gives the separation that file
   permissions cannot, and needs only one uvicorn flag.
2. **CSRF step-up.** A local process can load the SPA and mint the token, as the issue notes.
   It adds nothing a forger lacks.
3. **Out-of-band confirmation.** The confirming request goes through the same edge, so a forger
   can confirm its own parked rule unless the confirmation is itself attested. Attesting the
   original write is simpler.
4. **Session-scope only from loopback.** The browser path *is* loopback, so this would take
   `repo` and `global` rules away from the operator.

## Residual risk

- **Root on the host** can forge anything. That is out of scope: root also owns tailscaled.
- **A browser on the edge host itself cannot write rules or profile patterns by default.** At the
  network level it is the same as an agent session on that host: same user, same source address,
  same front-end. Operators who use ViberPit mainly from the host can opt in with
  `VIBERPIT_RULES_TRUST_LOCAL_HOST=true` (spec `docs/superpowers/specs/2026-10-14-rules-trust-local-host-design.md`).
  The cost is explicit: every process on the host running as the operator, agent sessions
  included, can then write rules and profile patterns. Token-borne callers stay refused. Writes
  admitted this way carry `via`/`created_via` = `local-trusted`. The setting lives only in the
  root-owned env file, so a session cannot turn it on. A passkey step-up (#1410) remains the
  route to an agent-proof answer.
- **Any process running as www-data** is trusted like nginx. On dobby that account runs only
  nginx. Anything else given that account (a PHP app, say) would inherit the trust.
- **Until the edge unit is reinstalled** with `--no-proxy-headers` (`deploy/README.md`, "Edge
  unit"; units do not auto-apply), rule writes and profile-pattern writes through
  `tailscale serve` fail closed with `RULES_HUMAN_ONLY`. The nginx path has no
  `X-Forwarded-For` and keeps working.
- **tailscaled not running as root** (e.g. a rootless userspace-networking install as the
  operator's user) makes the serve hop look local, so rule and profile-pattern writes 403. This fails
  closed and is visible: the UI shows the `RULES_HUMAN_ONLY` message. Re-evaluate before running
  the edge on such a host.
- **A local process can still act through a remote device.** This control covers this host's
  processes only. A compromised tailnet device logged in as the operator is the operator, as
  before.
- **nginx passed a client's `X-Forwarded-For` through** (pre-existing, #3010). Through `:443`,
  any tailnet device could pick the address `tailscale whois` identifies. The vhost in
  `deploy/viberpit-tailnet.nginx` now overwrites it with `$remote_addr`. Like units, the vhost
  does not auto-apply (`deploy/README.md`, "Edge unit"), and the hole stays open on dobby until
  it is reinstalled. This gate never relied on that header: it reads nginx's `X-Real-IP`.
- **Other human-attributed routes** (starting sessions, answering cards allow/deny, settings)
  still accept a loopback-asserted identity. They do not create durable, fleet-wide grants, so
  they stay outside this change. Moving them behind `human_gate_via` is now a one-line change
  per route if a later review wants it.
- **The `/proc` lookup is Linux-only.** On any other OS the tables are missing, so loopback
  writes fail closed.

## Verification

- `server/tests/test_local_identity_assertion.py`: the lookup against real v4, v6 and dual-stack
  sockets; the decision table above; every listed route refuses a uid-1000 local socket and
  admits a front-end relaying another device; the WS `always` answer goes through the gate.
  Every review bypass is refused end to end, and each check was proven by a mutant that removes
  it and fails the tests:
  - `X-Forwarded-For` naming a remote device, or naming a root-owned local socket (the gate must
    read the recorded peer, not `scope["client"]`);
  - a local `curl` through serve or nginx (the original-client check);
  - a client that closes its socket before the handler runs (the ESTABLISHED filter; the test
    uses a synthetic table), or resets it so the tuple can be reused (the edge-socket inode
    binding; the test uses a real socket);
  - an origin that is not a tailnet address, or that is an address this host owns (the
    tailnet-range rule and the bind probe).
- Live check against real uvicorn with `--no-proxy-headers`: `request.client` is still rewritten
  from `X-Forwarded-For`, while the recorded peer is the real socket. Without the flag, a forged
  header is detected and the gate fails closed with the WARNING. The transport inode is
  captured for both HTTP and WebSocket connections.
- `server/tests/conftest.py` models the TestClient's `127.0.0.1:50000` peer as the serve hop
  (root-owned), matching the existing topology patch there.
