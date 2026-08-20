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
#   bash scripts/ccswitch-mint-mtls.sh <outdir> <daemon-dns> <daemon-ip>
#   e.g. bash scripts/ccswitch-mint-mtls.sh /tmp/mtls rocky.tail0dd570.ts.net 100.69.70.109
set -euo pipefail

OUT="${1:?usage: ccswitch-mint-mtls.sh <outdir> <daemon-dns> <daemon-ip>}"
DNS="${2:?daemon dns name, e.g. rocky.tail0dd570.ts.net}"
IP="${3:?daemon ip, e.g. 100.69.70.109}"
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
echo "minted in $OUT: ca.pem  server.pem/.key (SAN DNS:$DNS,IP:$IP)  client.pem/.key (CN=edge)"
