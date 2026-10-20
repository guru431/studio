"""Программный аутентификатор WebAuthn для тестов: ключ EC P-256, authenticatorData и clientDataJSON
собираются здесь, проверяет их настоящая библиотека webauthn службы (спека screen-lock, 6.11)."""
import base64
import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, AT = 0x01, 0x04, 0x40


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftKey:
    def __init__(self, origin="https://testserver", rp_id="testserver", uv=True):
        self.origin, self.rp_id, self.uv = origin, rp_id, uv
        self.raw_id = os.urandom(16)
        self.id = b64u(self.raw_id)
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.count = 0
        numbers = self._key.public_key().public_numbers()
        #: COSE: kty EC2, alg ES256, crv P-256, x, y — для вставки в passkeys мимо API
        self.public_key = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
                                       -3: numbers.y.to_bytes(32, "big")})

    def _auth_data(self, extra_flags: int, tail: bytes = b"") -> bytes:
        flags = UP | (UV if self.uv else 0) | extra_flags
        return (hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big")
                + tail)

    def _client_data(self, kind: str, options: dict) -> bytes:
        return json.dumps({"type": kind, "challenge": options["challenge"], "origin": self.origin,
                           "crossOrigin": False}).encode()

    def register(self, options: dict) -> dict:
        """Ответ navigator.credentials.create() в JSON-форме (байты — base64url без `=`)."""
        tail = bytes(16) + len(self.raw_id).to_bytes(2, "big") + self.raw_id + self.public_key
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(AT, tail)})
        return {"id": self.id, "rawId": self.id, "type": "public-key",
                "response": {"clientDataJSON": b64u(self._client_data("webauthn.create", options)),
                             "attestationObject": b64u(attestation), "transports": ["internal"]},
                "clientExtensionResults": {}}

    def assertion(self, options: dict) -> dict:
        """Ответ navigator.credentials.get(); счётчик растёт на 1 с каждым вызовом."""
        self.count += 1
        auth_data = self._auth_data(0)
        client_data = self._client_data("webauthn.get", options)
        signature = self._key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": self.id, "rawId": self.id, "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "authenticatorData": b64u(auth_data),
                             "signature": b64u(signature), "userHandle": None},
                "clientExtensionResults": {}}
