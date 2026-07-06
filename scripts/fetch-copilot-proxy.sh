#!/usr/bin/env bash
# fetch-copilot-proxy.sh — Download, verify, and install the pinned copilot2api binary.
#
# Install destination: ~/.local/lib/ccswitch/copilot2api
# This path is checked by resolve_copilot_proxy() in server/sessions/copilot_proxy.py.
#
# Usage:
#   bash scripts/fetch-copilot-proxy.sh
#
# Environment overrides:
#   COPILOT_PROXY_INSTALL_DIR  — override install directory (default: ~/.local/lib/ccswitch)
#   COPILOT_PROXY_ARCH         — override arch tag   (default: linux-amd64)
#   COPILOT_PROXY_SKIP_VERIFY  — set to 1 to skip SHA256 check (NOT recommended)

set -euo pipefail

COPILOT_PROXY_VERSION="v0.4.0"
ARCH="${COPILOT_PROXY_ARCH:-linux-amd64}"
INSTALL_DIR="${COPILOT_PROXY_INSTALL_DIR:-${HOME}/.local/lib/ccswitch}"
INSTALL_PATH="${INSTALL_DIR}/copilot2api"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHA256_FILE="${SCRIPT_DIR}/copilot2api.sha256"

ASSET_NAME="copilot2api-${ARCH}"
DOWNLOAD_URL="https://github.com/whtsky/copilot2api/releases/download/${COPILOT_PROXY_VERSION}/${ASSET_NAME}"

TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_WORK}"' EXIT

echo "==> Downloading copilot2api ${COPILOT_PROXY_VERSION} (${ARCH})..."
echo "    URL: ${DOWNLOAD_URL}"

if command -v curl &>/dev/null; then
    curl -fsSL --retry 3 --retry-delay 2 -o "${TMPDIR_WORK}/${ASSET_NAME}" "${DOWNLOAD_URL}"
elif command -v wget &>/dev/null; then
    wget -q --tries=3 -O "${TMPDIR_WORK}/${ASSET_NAME}" "${DOWNLOAD_URL}"
else
    echo "ERROR: Neither curl nor wget found. Install one and retry." >&2
    exit 1
fi

if [[ "${COPILOT_PROXY_SKIP_VERIFY:-0}" != "1" ]]; then
    if [[ ! -f "${SHA256_FILE}" ]]; then
        echo "ERROR: SHA256 file not found: ${SHA256_FILE}" >&2
        exit 1
    fi

    echo "==> Verifying SHA256 checksum..."
    # Extract expected hash for this asset from the .sha256 file (first field on matching line)
    EXPECTED_HASH="$(grep "[[:space:]]${ASSET_NAME}$" "${SHA256_FILE}" | awk '{print $1}')"
    if [[ -z "${EXPECTED_HASH}" ]]; then
        echo "ERROR: No SHA256 entry found for '${ASSET_NAME}' in ${SHA256_FILE}" >&2
        exit 1
    fi

    ACTUAL_HASH="$(sha256sum "${TMPDIR_WORK}/${ASSET_NAME}" | awk '{print $1}')"
    if [[ "${ACTUAL_HASH}" != "${EXPECTED_HASH}" ]]; then
        echo "ERROR: SHA256 mismatch for ${ASSET_NAME}" >&2
        echo "  expected: ${EXPECTED_HASH}" >&2
        echo "  actual:   ${ACTUAL_HASH}" >&2
        exit 1
    fi
    echo "    Checksum OK: ${ACTUAL_HASH}"
else
    echo "WARNING: SHA256 verification skipped (COPILOT_PROXY_SKIP_VERIFY=1)" >&2
fi

echo "==> Installing to ${INSTALL_PATH}..."
mkdir -p "${INSTALL_DIR}"
cp "${TMPDIR_WORK}/${ASSET_NAME}" "${INSTALL_PATH}"
chmod +x "${INSTALL_PATH}"

echo "==> Done. copilot2api ${COPILOT_PROXY_VERSION} installed at: ${INSTALL_PATH}"
