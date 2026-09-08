#!/usr/bin/env bash
# Mint the private CA + server/client certs for the ccswitch cross-host daemon channel.
#
# The server cert's SAN MUST match how the EDGE reaches the daemon. ccswitch's own
# quickstart flags hostname mismatch as the number-one footgun here: a mismatch fails
# with "certificate does not match the server name" and reads like a TLS bug.
#
# Use the TAILNET identity, not a LAN name: `rocky` resolves to a LAN address via mDNS,
# so a cert minted for rocky.lan would silently describe the wrong path.
#
# The CA pair is installed into $CCSWITCH_TLS_DIR (default /etc/viberpit/tls; existing legacy root is retained), not just
# the outdir: every future host onboarding needs ca.key, and the original fleet mint
# left it behind in an ephemeral outdir (issue #841). If a CA already exists there this
# script refuses to run — a re-mint creates a NEW CA the rest of the fleet does not
# trust. Sign the new host's cert with the existing CA instead: see
# docs/runbooks/onboarding-a-session-runner.md, Step 3 Option A.
#
#   sudo bash scripts/viberpit-mint-mtls.sh <outdir> <daemon-dns> <daemon-ip>
#   e.g. sudo bash scripts/viberpit-mint-mtls.sh /tmp/mtls rocky.tail0dd570.ts.net 100.69.70.109
set -euo pipefail

OUT="${1:?usage: viberpit-mint-mtls.sh <outdir> <daemon-dns> <daemon-ip>}"
DNS="${2:?daemon dns name, e.g. rocky.tail0dd570.ts.net}"
IP="${3:?daemon ip, e.g. 100.69.70.109}"
TLS_DEFAULT=/etc/viberpit/tls
if [ ! -d /etc/viberpit ] && [ -d /etc/ccswitch ]; then
  TLS_DEFAULT=/etc/ccswitch/tls
fi
TLS_DIR="${VIBERPIT_TLS_DIR:-${CCSWITCH_TLS_DIR:-$TLS_DEFAULT}}"

for existing in "$TLS_DIR"/ca*.pem "$TLS_DIR"/ca*.key; do
  [ -e "$existing" ] || continue
  echo "refusing to mint: $existing exists — this fleet already has a CA." >&2
  echo "A fresh CA would not be trusted by the existing edge/hosts. Sign the new host's" >&2
  echo "cert with the existing CA (onboarding runbook, Step 3 Option A), or point" >&2
  echo "CCSWITCH_TLS_DIR at another directory for a genuinely separate fleet." >&2
  exit 1
done

mkdir -p "$TLS_DIR" 2>/dev/null || true
if [ ! -d "$TLS_DIR" ] || [ ! -w "$TLS_DIR" ]; then
  echo "cannot write $TLS_DIR — run with sudo (or set CCSWITCH_TLS_DIR to a durable dir)." >&2
  echo "The CA key must land somewhere durable, not just the outdir: losing ca.key makes" >&2
  echo "every future host onboarding impossible (issue #841)." >&2
  exit 1
fi

mkdir -p "$OUT"; cd "$OUT"; umask 077

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem \
  -subj "/CN=ccswitch-ca" -days 3650 2>/dev/null

openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=sessiond" 2>/dev/null
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out server.pem -days 825 \
  -extfile <(printf "subjectAltName=DNS:%s,IP:%s" "$DNS" "$IP") 2>/dev/null

# CN=edge is what CCSWITCH_SESSIOND_CLIENT_ALLOWLIST pins against, narrowing trust
# from "any CA-signed client" to this one client.
openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr \
  -subj "/CN=edge" 2>/dev/null
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -out client.pem -days 825 2>/dev/null

rm -f server.csr client.csr
chmod 600 ./*.key
install -m 644 ca.pem "$TLS_DIR/ca.pem"
install -m 600 ca.key "$TLS_DIR/ca.key"
echo "minted in $OUT: ca.pem  server.pem/.key (SAN DNS:$DNS,IP:$IP)  client.pem/.key (CN=edge)"
echo "CA installed durably: $TLS_DIR/ca.pem + $TLS_DIR/ca.key (0600) — keep ca.key; it signs all future host certs"
