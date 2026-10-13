"""凭证加密与密钥派生。"""

from __future__ import annotations

import base64

import pytest

from src.db.crypto import CredentialCipher, CredentialDecryptError, derive_key


def test_cipher_roundtrip():
    cipher = CredentialCipher("secret-a-0123456789")
    token = cipher.encrypt(b'{"uid": "1"}')
    assert cipher.decrypt(token) == b'{"uid": "1"}'

def test_cipher_wrong_secret_raises():
    token = CredentialCipher("secret-a-0123456789").encrypt(b"x")
    with pytest.raises(CredentialDecryptError):
        CredentialCipher("secret-b-0123456789").decrypt(token)

def test_cipher_rejects_empty_secret():
    with pytest.raises(ValueError):
        CredentialCipher("")

def test_derive_key_is_stable_urlsafe_base64():
    key = derive_key("abc")
    assert key == derive_key("abc")
    assert key != derive_key("abd")
    assert len(base64.urlsafe_b64decode(key)) == 32
