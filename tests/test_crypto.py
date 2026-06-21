"""Tests for ccswitch.crypto — envelope encryption (AES-256-GCM)."""
import base64
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import temp_env

# A deterministic 32-byte test key (base64-encoded).
_TEST_KEY_BYTES = bytes(range(32))
_TEST_KEY_B64 = base64.b64encode(_TEST_KEY_BYTES).decode("ascii")


def _with_key(func):
    """Decorator: run test with CCSWITCH_MASTER_KEY set to _TEST_KEY_B64."""
    def wrapper(self, *args, **kwargs):
        old = os.environ.get("CCSWITCH_MASTER_KEY")
        os.environ["CCSWITCH_MASTER_KEY"] = _TEST_KEY_B64
        try:
            return func(self, *args, **kwargs)
        finally:
            if old is None:
                os.environ.pop("CCSWITCH_MASTER_KEY", None)
            else:
                os.environ["CCSWITCH_MASTER_KEY"] = old
    return wrapper


class TestCryptoModule(unittest.TestCase):
    """Unit tests for crypto.py primitives."""

    def _clear_key(self):
        """Ensure CCSWITCH_MASTER_KEY is unset and secret-tool returns nothing."""
        os.environ.pop("CCSWITCH_MASTER_KEY", None)

    # ------------------------------------------------------------------
    # import safety — module import must not raise even without cryptography
    # ------------------------------------------------------------------

    def test_module_imports_without_raising(self):
        """Importing crypto must never raise even with no key / no cryptography."""
        # The import already happened above; just verify the module is accessible.
        import ccswitch.crypto  # noqa: F401

    # ------------------------------------------------------------------
    # master_key / key_available
    # ------------------------------------------------------------------

    @_with_key
    def test_master_key_from_env(self):
        from ccswitch.crypto import master_key
        key = master_key()
        self.assertEqual(key, _TEST_KEY_BYTES)

    def test_master_key_none_when_not_configured(self):
        self._clear_key()
        # mock secret-tool to return non-zero (not found)
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=1, stdout="")
            from ccswitch.crypto import master_key
            self.assertIsNone(master_key())

    def test_master_key_malformed_base64_returns_none(self):
        os.environ["CCSWITCH_MASTER_KEY"] = "not-valid-base64!!!"
        try:
            from ccswitch.crypto import master_key
            # malformed → _decode_key returns None
            with mock.patch("subprocess.run") as m:
                m.return_value = mock.Mock(returncode=1, stdout="")
                self.assertIsNone(master_key())
        finally:
            os.environ.pop("CCSWITCH_MASTER_KEY", None)

    def test_master_key_wrong_length_returns_none(self):
        # 16 bytes ≠ 32
        short = base64.b64encode(b"x" * 16).decode()
        os.environ["CCSWITCH_MASTER_KEY"] = short
        try:
            from ccswitch.crypto import master_key
            with mock.patch("subprocess.run") as m:
                m.return_value = mock.Mock(returncode=1, stdout="")
                self.assertIsNone(master_key())
        finally:
            os.environ.pop("CCSWITCH_MASTER_KEY", None)

    @_with_key
    def test_key_available_true_when_key_set(self):
        from ccswitch.crypto import key_available
        self.assertTrue(key_available())

    def test_key_available_false_when_no_key(self):
        self._clear_key()
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=1, stdout="")
            from ccswitch.crypto import key_available
            self.assertFalse(key_available())

    # ------------------------------------------------------------------
    # is_encrypted
    # ------------------------------------------------------------------

    def test_is_encrypted_detects_sentinel(self):
        from ccswitch.crypto import is_encrypted
        self.assertTrue(is_encrypted("ccenc1:AAAABBBB"))
        self.assertFalse(is_encrypted("plain-token"))
        self.assertFalse(is_encrypted(""))
        self.assertFalse(is_encrypted("ccenc2:AAAABBBB"))  # wrong version

    # ------------------------------------------------------------------
    # encrypt / decrypt round-trip
    # ------------------------------------------------------------------

    @_with_key
    def test_encrypt_decrypt_roundtrip(self):
        from ccswitch.crypto import encrypt, decrypt
        pt = "my-secret-refresh-token-abc123"
        ct = encrypt(pt)
        self.assertTrue(ct.startswith("ccenc1:"))
        self.assertNotEqual(ct, pt)
        recovered = decrypt(ct)
        self.assertEqual(recovered, pt)

    @_with_key
    def test_encrypt_produces_different_nonce_each_time(self):
        from ccswitch.crypto import encrypt
        pt = "same-plaintext"
        ct1 = encrypt(pt)
        ct2 = encrypt(pt)
        self.assertNotEqual(ct1, ct2)  # different nonces → different ciphertext

    @_with_key
    def test_decrypt_passes_through_legacy_plaintext(self):
        from ccswitch.crypto import decrypt
        plain = "legacy-refresh-token"
        self.assertEqual(decrypt(plain), plain)

    def test_decrypt_raises_unavailable_when_no_key_and_encrypted(self):
        self._clear_key()
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=1, stdout="")
            from ccswitch.crypto import decrypt, CryptoUnavailable
            with self.assertRaises(CryptoUnavailable):
                decrypt("ccenc1:AAAAAAAAAAAAAAAAAAAAAA==")

    @_with_key
    def test_decrypt_raises_crypto_error_on_tampered_ciphertext(self):
        from ccswitch.crypto import encrypt, decrypt, CryptoError
        ct = encrypt("secret")
        # Tamper with the last byte of the base64 payload
        payload = ct[len("ccenc1:"):]
        raw = bytearray(base64.b64decode(payload))
        raw[-1] ^= 0xFF
        tampered = "ccenc1:" + base64.b64encode(bytes(raw)).decode()
        with self.assertRaises(CryptoError):
            decrypt(tampered)

    def test_encrypt_raises_unavailable_when_no_key(self):
        self._clear_key()
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=1, stdout="")
            from ccswitch.crypto import encrypt, CryptoUnavailable
            with self.assertRaises(CryptoUnavailable):
                encrypt("plaintext")

    # ------------------------------------------------------------------
    # generate_key
    # ------------------------------------------------------------------

    def test_generate_key_returns_valid_32_byte_base64(self):
        from ccswitch.crypto import generate_key
        k = generate_key()
        raw = base64.b64decode(k)
        self.assertEqual(len(raw), 32)

    def test_generate_key_is_random(self):
        from ccswitch.crypto import generate_key
        self.assertNotEqual(generate_key(), generate_key())

    # ------------------------------------------------------------------
    # keyring fallback (master_key reads from secret-tool)
    # ------------------------------------------------------------------

    def test_master_key_from_keyring_when_env_absent(self):
        self._clear_key()
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(returncode=0, stdout=_TEST_KEY_B64 + "\n")
            from ccswitch.crypto import master_key
            key = master_key()
        self.assertEqual(key, _TEST_KEY_BYTES)

    def test_master_key_secret_tool_missing_returns_none(self):
        self._clear_key()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            from ccswitch.crypto import master_key
            self.assertIsNone(master_key())


class TestReadWriteCredentials(unittest.TestCase):
    """Tests for creds.read_credentials / write_credentials."""

    def _seed_slot(self, path: Path, token: str) -> None:
        """Write a plaintext slot to disk bypassing write_credentials."""
        import json, os
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"accessToken": "at", "refreshToken": token, "expiresAt": 9999}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)

    @_with_key
    def test_write_credentials_encrypts_refresh_token(self):
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import write_credentials, read_json
            from ccswitch.crypto import is_encrypted
            paths.store_dir().mkdir(parents=True, exist_ok=True)
            slot = paths.slot_path("alpha")
            data = {"accessToken": "at", "refreshToken": "secret-rt", "expiresAt": 42}
            write_credentials(slot, data)
            on_disk = read_json(slot)
            self.assertTrue(is_encrypted(on_disk["refreshToken"]),
                            "refreshToken on disk should be encrypted")
            # accessToken must stay plaintext
            self.assertEqual(on_disk["accessToken"], "at")

    def test_write_credentials_plaintext_when_no_key(self):
        with temp_env():
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
            with mock.patch("subprocess.run") as m:
                m.return_value = mock.Mock(returncode=1, stdout="")
                from ccswitch import paths
                from ccswitch.creds import write_credentials, read_json
                import sys, io
                paths.store_dir().mkdir(parents=True, exist_ok=True)
                slot = paths.slot_path("alpha")
                data = {"accessToken": "at", "refreshToken": "plain-rt", "expiresAt": 42}
                # Capture stderr warning
                err = io.StringIO()
                with mock.patch.object(sys, "stderr", err):
                    # Reset the one-time warning flag so it fires in this test
                    import ccswitch.crypto as _c
                    _c._warned_no_key = False
                    write_credentials(slot, data)
                on_disk = read_json(slot)
                self.assertEqual(on_disk["refreshToken"], "plain-rt",
                                 "No key → refreshToken stays plaintext")
                self.assertIn("WARNING", err.getvalue())

    @_with_key
    def test_read_credentials_decrypts_encrypted_slot(self):
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import write_credentials, read_credentials
            from ccswitch.crypto import is_encrypted
            paths.store_dir().mkdir(parents=True, exist_ok=True)
            slot = paths.slot_path("alpha")
            # First write (encrypts)
            write_credentials(slot, {"accessToken": "at", "refreshToken": "my-rt", "expiresAt": 1})
            # Now read back — should get plaintext
            out = read_credentials(slot)
            self.assertEqual(out["refreshToken"], "my-rt")
            self.assertFalse(is_encrypted(out["refreshToken"]))

    @_with_key
    def test_read_credentials_legacy_plaintext_passthrough(self):
        """read_credentials must handle legacy plaintext slots silently."""
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import read_credentials
            paths.store_dir().mkdir(parents=True, exist_ok=True)
            slot = paths.slot_path("alpha")
            self._seed_slot(slot, "legacy-plain-rt")
            out = read_credentials(slot)
            self.assertEqual(out["refreshToken"], "legacy-plain-rt")

    @_with_key
    def test_write_credentials_already_encrypted_is_idempotent(self):
        """If the refreshToken is already encrypted, don't double-encrypt."""
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import write_credentials, read_credentials
            from ccswitch.crypto import encrypt
            paths.store_dir().mkdir(parents=True, exist_ok=True)
            slot = paths.slot_path("alpha")
            already_enc = encrypt("my-rt")
            data = {"accessToken": "at", "refreshToken": already_enc, "expiresAt": 1}
            write_credentials(slot, data)
            out = read_credentials(slot)
            self.assertEqual(out["refreshToken"], "my-rt",
                             "Should decrypt to original, not double-encrypt")


class TestComposePlaintext(unittest.TestCase):
    """compose() must write PLAINTEXT .credentials.json even when store is encrypted."""

    @_with_key
    def test_compose_writes_plaintext_credentials_when_slot_encrypted(self):
        with temp_env():
            from ccswitch import paths, configdir
            from ccswitch.creds import write_credentials
            from ccswitch.crypto import is_encrypted

            # Seed an encrypted store slot
            paths.store_dir().mkdir(parents=True, exist_ok=True)
            write_credentials(paths.slot_path("alpha"),
                              {"accessToken": "A", "refreshToken": "secret-rt", "expiresAt": 9})
            write_credentials(paths.shared_mcp_path(), {})

            # compose should decrypt for claude
            cfg_dir = configdir.compose("alpha")
            cred_file = cfg_dir / ".credentials.json"
            self.assertFalse(cred_file.is_symlink())
            data = json.loads(cred_file.read_text())
            rt = data["claudeAiOauth"]["refreshToken"]
            self.assertFalse(is_encrypted(rt),
                             f"composed .credentials.json must be plaintext, got: {rt!r}")
            self.assertEqual(rt, "secret-rt")


class TestCryptoCLI(unittest.TestCase):
    """Tests for `ccswitch crypto` subcommands."""

    def test_crypto_status_no_key(self):
        with temp_env():
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
            import io, sys
            from ccswitch.cli import main
            with mock.patch("subprocess.run") as m:
                m.return_value = mock.Mock(returncode=1, stdout="")
                out = io.StringIO()
                with mock.patch.object(sys, "stdout", out):
                    rc = main(["crypto", "status"])
            self.assertEqual(rc, 0)
            self.assertIn("no master key", out.getvalue().lower())

    @_with_key
    def test_crypto_status_with_key(self):
        with temp_env():
            import io, sys
            from ccswitch.cli import main
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                rc = main(["crypto", "status"])
            self.assertEqual(rc, 0)
            self.assertIn("env", out.getvalue())

    def test_crypto_init_stores_key(self):
        with temp_env():
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
            import io, sys
            from ccswitch.cli import main
            with mock.patch("subprocess.run") as m:
                # secret-tool lookup → not found; secret-tool store → success
                m.side_effect = [
                    mock.Mock(returncode=1, stdout=""),   # lookup (key_available check)
                    mock.Mock(returncode=0, stdout=""),   # store
                ]
                out = io.StringIO()
                with mock.patch.object(sys, "stdout", out):
                    rc = main(["crypto", "init"])
            self.assertEqual(rc, 0)
            self.assertIn("generated", out.getvalue().lower())

    @_with_key
    def test_crypto_init_noop_when_key_exists(self):
        with temp_env():
            import io, sys
            from ccswitch.cli import main
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                rc = main(["crypto", "init"])
            self.assertEqual(rc, 0)
            self.assertIn("already configured", out.getvalue())

    @_with_key
    def test_crypto_migrate_encrypts_plaintext_slots(self):
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import read_json, write_json_secure
            from ccswitch.crypto import is_encrypted
            import io, sys
            from ccswitch.cli import main

            # Seed two plaintext slots
            paths.store_dir().mkdir(parents=True, exist_ok=True)
            for name in ("alpha", "beta"):
                write_json_secure(paths.slot_path(name),
                                  {"accessToken": "at", "refreshToken": "rt-" + name, "expiresAt": 1})

            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                rc = main(["crypto", "migrate"])
            self.assertEqual(rc, 0)
            self.assertIn("2 slot(s) encrypted", out.getvalue())

            # Verify both are now encrypted on disk
            for name in ("alpha", "beta"):
                on_disk = read_json(paths.slot_path(name))
                self.assertTrue(is_encrypted(on_disk["refreshToken"]))

    @_with_key
    def test_crypto_migrate_idempotent(self):
        """Running migrate twice doesn't error or double-encrypt."""
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import write_json_secure, read_credentials
            import io, sys
            from ccswitch.cli import main

            paths.store_dir().mkdir(parents=True, exist_ok=True)
            write_json_secure(paths.slot_path("alpha"),
                              {"accessToken": "at", "refreshToken": "rt-alpha", "expiresAt": 1})

            # First migrate
            main(["crypto", "migrate"])
            # Second migrate
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                rc = main(["crypto", "migrate"])
            self.assertEqual(rc, 0)
            self.assertIn("0 slot(s) encrypted", out.getvalue())  # already encrypted → skipped
            # Value still decryptable
            self.assertEqual(read_credentials(paths.slot_path("alpha"))["refreshToken"], "rt-alpha")

    def test_crypto_migrate_refuses_without_key(self):
        with temp_env():
            os.environ.pop("CCSWITCH_MASTER_KEY", None)
            import io, sys
            from ccswitch.cli import main
            with mock.patch("subprocess.run") as m:
                m.return_value = mock.Mock(returncode=1, stdout="")
                err = io.StringIO()
                with mock.patch.object(sys, "stderr", err):
                    rc = main(["crypto", "migrate"])
            self.assertNotEqual(rc, 0)
            self.assertIn("no master key", err.getvalue().lower())
