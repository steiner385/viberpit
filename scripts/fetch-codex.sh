#!/usr/bin/env bash
# fetch-codex.sh — Download, verify, and install the Codex CLI version ccswitch pins.
#
# Install destination: ~/.local/lib/ccswitch/codex
# Point the daemon at it with CCSWITCH_CODEX_BIN (server/codex/process.py resolves an
# explicit path directly, and a bare name through $PATH).
#
# Usage:
#   bash scripts/fetch-codex.sh                    # download the pinned version and install
#   bash scripts/fetch-codex.sh --from ./codex.tgz # install an archive/binary you already have
#   bash scripts/fetch-codex.sh --record           # record the digest of what was fetched
#
# Environment overrides:
#   CODEX_INSTALL_DIR   — install directory (default: ~/.local/lib/ccswitch)
#   CODEX_TARGET        — release target triple (default: x86_64-unknown-linux-musl)
#   CODEX_DOWNLOAD_URL  — full URL, bypassing the constructed one
#   CODEX_SKIP_VERIFY   — set to 1 to skip the SHA256 check (NOT recommended)
#
# Why the digest is trust-on-first-use rather than committed ahead of time
# -----------------------------------------------------------------------
# The point of pinning is that every runner in the fleet executes the SAME bytes:
# dobby and rocky sharing one ChatGPT account must not disagree about what `codex`
# is. A digest can only be established by fetching the artefact on a host that can
# reach the network and confirming its provenance, so this script records it once
# (`--record`, writing scripts/codex-cli.sha256) and then ENFORCES it on every host
# afterwards. Committing a placeholder instead would either break every install or,
# worse, teach an operator to overwrite the expected value with whatever they just
# downloaded — which is not a check at all.
#
# Version drift is caught independently and does not depend on the digest: the
# install is refused unless `codex --version` equals the contract this checkout was
# written against (server/codex/schema.py:PINNED_CLI_VERSION), and the daemon
# separately re-generates and hashes the app-server JSON schema before offering
# Codex at all (server/codex/process.py:probe_availability).
#
# A host without Codex is not a broken host: `probe_availability` reports
# `binary_not_found` and the daemon advertises Codex as unavailable through /info
# while continuing to serve Claude sessions. Never make this script a boot
# dependency.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHA256_FILE="${SCRIPT_DIR}/codex-cli.sha256"
SCHEMA_PY="${REPO_ROOT}/server/codex/schema.py"

INSTALL_DIR="${CODEX_INSTALL_DIR:-${HOME}/.local/lib/ccswitch}"
INSTALL_PATH="${INSTALL_DIR}/codex"
TARGET="${CODEX_TARGET:-x86_64-unknown-linux-musl}"

RECORD=0
SOURCE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --record) RECORD=1; shift ;;
        --from) SOURCE="${2:?--from needs a path or URL}"; shift 2 ;;
        -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

# The pinned version is READ from the schema module, never restated here: the CLI
# version and the app-server contract hash are one pin, and a script carrying its
# own copy would let the installer and the runtime gate disagree silently.
if [[ ! -f "${SCHEMA_PY}" ]]; then
    echo "ERROR: cannot find ${SCHEMA_PY}; run this from a ccswitch checkout." >&2
    exit 1
fi
PINNED_VERSION_STRING="$(sed -n 's/^PINNED_CLI_VERSION: Final = "\(.*\)"$/\1/p' "${SCHEMA_PY}" | head -1)"
if [[ -z "${PINNED_VERSION_STRING}" ]]; then
    echo "ERROR: could not read PINNED_CLI_VERSION from ${SCHEMA_PY}." >&2
    exit 1
fi
# "codex-cli 0.153.2" -> "0.153.2"
PINNED_VERSION="${PINNED_VERSION_STRING##* }"

# NOTE: confirm this asset name against the release page when bumping the pin —
# upstream is an experimental surface and has renamed assets before. The digest
# check below is what makes a wrong or substituted artefact fail closed.
ASSET_NAME="codex-${TARGET}.tar.gz"
DOWNLOAD_URL="${CODEX_DOWNLOAD_URL:-https://github.com/openai/codex/releases/download/rust-v${PINNED_VERSION}/${ASSET_NAME}}"

TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_WORK}"' EXIT

ARTEFACT="${TMPDIR_WORK}/artefact"

if [[ -n "${SOURCE}" && -f "${SOURCE}" ]]; then
    echo "==> Using local artefact: ${SOURCE}"
    cp "${SOURCE}" "${ARTEFACT}"
    ASSET_NAME="$(basename "${SOURCE}")"
else
    URL="${SOURCE:-${DOWNLOAD_URL}}"
    echo "==> Downloading Codex CLI ${PINNED_VERSION} (${TARGET})..."
    echo "    URL: ${URL}"
    if command -v curl &>/dev/null; then
        curl -fsSL --retry 3 --retry-delay 2 -o "${ARTEFACT}" "${URL}"
    elif command -v wget &>/dev/null; then
        wget -q --tries=3 -O "${ARTEFACT}" "${URL}"
    else
        echo "ERROR: Neither curl nor wget found. Install one and retry." >&2
        exit 1
    fi
fi

ACTUAL_HASH="$(sha256sum "${ARTEFACT}" | awk '{print $1}')"

if [[ "${RECORD}" == "1" ]]; then
    echo "==> Recording digest for ${ASSET_NAME}"
    echo "    ${ACTUAL_HASH}"
    mkdir -p "$(dirname "${SHA256_FILE}")"
    # Replace any previous line for this asset; keep other targets' entries.
    if [[ -f "${SHA256_FILE}" ]]; then
        grep -v "[[:space:]]${ASSET_NAME}\$" "${SHA256_FILE}" > "${TMPDIR_WORK}/sha" || true
    else
        : > "${TMPDIR_WORK}/sha"
    fi
    printf '%s  %s\n' "${ACTUAL_HASH}" "${ASSET_NAME}" >> "${TMPDIR_WORK}/sha"
    LC_ALL=C sort -k2 "${TMPDIR_WORK}/sha" > "${SHA256_FILE}"
    echo "    Wrote ${SHA256_FILE} — commit it so every other runner enforces this digest."
elif [[ "${CODEX_SKIP_VERIFY:-0}" == "1" ]]; then
    echo "WARNING: SHA256 verification skipped (CODEX_SKIP_VERIFY=1)" >&2
else
    EXPECTED_HASH=""
    if [[ -f "${SHA256_FILE}" ]]; then
        EXPECTED_HASH="$(grep "[[:space:]]${ASSET_NAME}\$" "${SHA256_FILE}" | awk '{print $1}')"
    fi
    if [[ -z "${EXPECTED_HASH}" ]]; then
        echo "ERROR: no recorded SHA256 for '${ASSET_NAME}'." >&2
        echo "  Confirm this artefact's provenance against the upstream release, then" >&2
        echo "  pin it once with:  bash scripts/fetch-codex.sh --record" >&2
        echo "  and commit ${SHA256_FILE} so every other runner enforces it." >&2
        exit 1
    fi
    echo "==> Verifying SHA256 checksum..."
    if [[ "${ACTUAL_HASH}" != "${EXPECTED_HASH}" ]]; then
        echo "ERROR: SHA256 mismatch for ${ASSET_NAME}" >&2
        echo "  expected: ${EXPECTED_HASH}" >&2
        echo "  actual:   ${ACTUAL_HASH}" >&2
        exit 1
    fi
    echo "    Checksum OK: ${ACTUAL_HASH}"
fi

echo "==> Extracting..."
BINARY="${ARTEFACT}"
if file --brief --mime-type "${ARTEFACT}" 2>/dev/null | grep -qE 'gzip|x-tar' \
   || [[ "${ASSET_NAME}" == *.tar.gz || "${ASSET_NAME}" == *.tgz ]]; then
    mkdir -p "${TMPDIR_WORK}/unpacked"
    tar -xzf "${ARTEFACT}" -C "${TMPDIR_WORK}/unpacked"
    BINARY="$(find "${TMPDIR_WORK}/unpacked" -type f -name 'codex*' -perm -u+x | head -1)"
    if [[ -z "${BINARY}" ]]; then
        echo "ERROR: no executable named codex* inside ${ASSET_NAME}." >&2
        exit 1
    fi
fi

# Install to a staging path FIRST and only promote after the version assertion, so
# a failed check never leaves a wrong binary where the daemon would resolve it.
STAGED="${TMPDIR_WORK}/codex-staged"
cp "${BINARY}" "${STAGED}"
chmod +x "${STAGED}"

echo "==> Verifying version against the ccswitch pin..."
INSTALLED_VERSION="$("${STAGED}" --version 2>/dev/null || true)"
if [[ -z "${INSTALLED_VERSION}" ]]; then
    echo "ERROR: '${ASSET_NAME}' did not answer --version; refusing to install." >&2
    exit 1
fi
if [[ "${INSTALLED_VERSION}" != "${PINNED_VERSION_STRING}" ]]; then
    echo "ERROR: version mismatch — refusing to install." >&2
    echo "  ccswitch pins: ${PINNED_VERSION_STRING}  (server/codex/schema.py)" >&2
    echo "  this binary:   ${INSTALLED_VERSION}" >&2
    echo "  Admitting another version is an explicit operator decision:" >&2
    echo "  set CCSWITCH_CODEX_SUPPORTED_VERSIONS — but note it can never waive a" >&2
    echo "  schema-contract change, only a version label." >&2
    exit 1
fi
echo "    Version OK: ${INSTALLED_VERSION}"

echo "==> Installing to ${INSTALL_PATH}..."
mkdir -p "${INSTALL_DIR}"
install -m 0755 "${STAGED}" "${INSTALL_PATH}"

cat <<EOF

==> Done. Codex CLI ${PINNED_VERSION} installed at: ${INSTALL_PATH}

Next:
  1. Point the daemon at it:   CCSWITCH_CODEX_BIN=${INSTALL_PATH}
  2. Check the whole boundary: python -m server.codex.doctor
     (binary/schema, runtime home, authority reachability, mTLS identity,
      account manifest, and the two launch flags)

Licence: the Codex CLI is Apache-2.0 upstream. This script installs an unmodified
released artefact — never patch or rebuild it (constitution Principle I).
EOF
