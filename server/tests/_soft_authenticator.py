"""A minimal software WebAuthn authenticator for tests (#1410).

Produces the same JSON a browser's ``PublicKeyCredential.toJSON()`` does — an ES256
key, ``none`` attestation, user presence + verification flags — so the server's real
py_webauthn verification runs end to end with no browser and no new dependency
(``cbor2`` and ``cryptography`` already come with ``webauthn``).
"""
from __future__ import annotations

import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

_UP, _UV, _AT = 0x01, 0x04, 0x40


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0

    def _rp_hash(self) -> bytes:
        return hashlib.sha256(self.rp_id.encode()).digest()

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge,
                           "origin": origin or self.origin, "crossOrigin": False}).encode()

    def create(self, public_key: dict, *, origin: str | None = None) -> dict:
        """``navigator.credentials.create`` for *public_key* (creation options JSON)."""
        numbers = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1,
                -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        attested = (bytes(16) + len(self.credential_id).to_bytes(2, "big")
                    + self.credential_id + cbor2.dumps(cose))
        auth_data = (self._rp_hash() + bytes([_UP | _UV | _AT])
                     + self.sign_count.to_bytes(4, "big") + attested)
        client_data = self._client_data("webauthn.create", public_key["challenge"], origin)
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(
                    cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def get(self, public_key: dict, *, origin: str | None = None,
            sign_count: int | None = None) -> dict:
        """``navigator.credentials.get`` for *public_key* (request options JSON)."""
        self.sign_count = self.sign_count + 1 if sign_count is None else sign_count
        auth_data = self._rp_hash() + bytes([_UP | _UV]) + self.sign_count.to_bytes(4, "big")
        client_data = self._client_data("webauthn.get", public_key["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
            },
            "clientExtensionResults": {},
        }
