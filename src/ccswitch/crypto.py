"""Envelope encryption for the ccswitch credential store.

Design constraints:
- Import MUST NOT raise even if `cryptography` is missing or no key is configured.
  All crypto imports are lazy (inside functions).
- If no key is available, encryption is DISABLED: callers fall back to plaintext.
- Only the `refreshToken` field in store slots is encrypted.
- `accessToken` (short-lived) stays plaintext.
- Format: "ccenc1:" + base64url(nonce[12] || ciphertext+tag)  (AES-256-GCM)
"""
import base64
import os
import re
import subprocess
import sys
from pathlib import Path


class CryptoError(Exception):
    """Authentication-tag failure, bad ciphertext, or other decryption error."""


class CryptoUnavailable(CryptoError):
    """No master key is configured."""


# Sentinel that is prepended to encrypted values so we can detect them.
_SENTINEL = "ccenc1:"

# Track whether we have emitted the plaintext-fallback warning so we only warn once.
_warned_no_key: bool = False


def master_key() -> bytes | None:
    """Return the 32-byte AES key, or None if not configured.

    Sources (in order):
    1. CCSWITCH_MASTER_KEY env var (base64-encoded 32 bytes)
    2. GNOME keyring via `secret-tool lookup service ccswitch username master-key`
    3. None
    """
    # 1. Environment variable
    env_val = os.environ.get("CCSWITCH_MASTER_KEY", "")
    if env_val:
        return _decode_key(env_val)

    # 2. GNOME keyring
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "service", "ccswitch", "username", "master-key"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # result may be None if subprocess.run is mocked (e.g. in tests); treat as absent.
        if result is not None and result.returncode == 0:
            val = result.stdout.strip()
            if val:
                return _decode_key(val)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return None


def _decode_key(b64: str) -> bytes | None:
    """Decode a base64 string to exactly 32 bytes, or return None if malformed."""
    try:
        raw = base64.b64decode(b64)
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    return None


def key_available() -> bool:
    """Return True if a valid master key is configured."""
    return master_key() is not None


def is_encrypted(value: str) -> bool:
    """Return True iff *value* was produced by encrypt() (starts with the sentinel)."""
    return isinstance(value, str) and value.startswith(_SENTINEL)


def encrypt(plaintext: str) -> str:
    """AES-256-GCM encrypt *plaintext*.

    Returns: "ccenc1:" + base64(12-byte nonce || ciphertext+tag)
    Raises CryptoUnavailable if no key is configured.
    Raises ImportError (propagated) only if `cryptography` is not installed at all
    — callers should guard with key_available() first so this path is unreachable
    in normal no-key operation.
    """
    key = master_key()
    if key is None:
        raise CryptoUnavailable("No CCSWITCH_MASTER_KEY configured")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise CryptoUnavailable("cryptography package not available")

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    payload = base64.b64encode(nonce + ct).decode("ascii")
    return _SENTINEL + payload


def decrypt(token: str) -> str:
    """Decrypt a value produced by encrypt().

    If *token* is not encrypted (no sentinel), return it unchanged — legacy plaintext
    pass-through.
    Raises CryptoUnavailable if the value is encrypted but no key is configured.
    Raises CryptoError on authentication-tag failure or bad ciphertext.
    """
    if not is_encrypted(token):
        return token  # legacy plaintext — pass through unchanged

    key = master_key()
    if key is None:
        raise CryptoUnavailable(
            "Token is encrypted (ccenc1:) but CCSWITCH_MASTER_KEY is not configured"
        )

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise CryptoUnavailable("cryptography package not available")

    payload_b64 = token[len(_SENTINEL):]
    try:
        raw = base64.b64decode(payload_b64)
    except Exception as exc:
        raise CryptoError(f"Base64 decode failed: {exc}") from exc

    if len(raw) < 12:
        raise CryptoError("Ciphertext too short (< 12 bytes)")

    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    try:
        plaintext_bytes = aesgcm.decrypt(nonce, ct, None)
    except Exception as exc:
        raise CryptoError(f"AES-GCM authentication failed: {exc}") from exc

    return plaintext_bytes.decode("utf-8")


def generate_key() -> str:
    """Generate a new 32-byte AES key and return it as base64."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


def store_key_in_keyring(b64key: str) -> None:
    """Store *b64key* in the GNOME keyring via secret-tool.

    Raises RuntimeError if secret-tool is not available or returns non-zero.
    """
    proc = subprocess.run(
        ["secret-tool", "store", "--label", "ccswitch master key",
         "service", "ccswitch", "username", "master-key"],
        input=b64key,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"secret-tool store failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )


def _oat_env_name(account: str) -> str:
    return "CCSWITCH_OAT_" + re.sub(r"[^A-Za-z0-9]", "_", account).upper()


def _oat_file(account: str) -> Path:
    from . import paths
    return paths.store_dir() / "oat" / account


def store_oauth_token(account: str, token: str) -> None:
    """Store a setup-token. Preferred path: master-key-encrypted file under the store dir
    (readable by the keyring-less daemon, which has CCSWITCH_MASTER_KEY). Falls back to the
    GNOME keyring only when no master key is available (interactive dev without a key)."""
    if key_available():
        p = _oat_file(account)
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        enc = encrypt(token)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(enc)
        return
    proc = subprocess.run(
        ["secret-tool", "store", "--label", f"ccswitch oauth token {account}",
         "service", "ccswitch", "username", f"oat-{account}"],
        input=token, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"secret-tool store failed (exit {proc.returncode}): {proc.stderr.strip()}")


def read_oauth_token(account: str) -> str | None:
    """Return the stored setup-token for *account*, or None. Precedence mirrors master_key():
    1) env override CCSWITCH_OAT_<ACCOUNT>, 2) master-key-encrypted file, 3) GNOME keyring."""
    env_val = os.environ.get(_oat_env_name(account), "").strip()
    if env_val:
        return env_val
    p = _oat_file(account)
    try:
        if p.exists():
            raw = p.read_text().strip()
            if raw:
                return decrypt(raw) if is_encrypted(raw) else raw
    except Exception:
        return None
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", "service", "ccswitch", "username", f"oat-{account}"],
            text=True, capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _warn_no_key_once() -> None:
    """Emit a one-time stderr warning that encryption is disabled."""
    global _warned_no_key
    if not _warned_no_key:
        _warned_no_key = True
        print(
            "ccswitch: WARNING: No CCSWITCH_MASTER_KEY configured — "
            "credentials stored as plaintext. Run `ccswitch crypto init` to enable encryption.",
            file=sys.stderr,
        )
