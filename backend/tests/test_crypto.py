"""Round-trip tests for the Fernet wrapper used for stored credentials."""
import pytest

from app.core.crypto import decrypt, encrypt


def test_round_trip_ascii():
    assert decrypt(encrypt("hunter2")) == "hunter2"


def test_round_trip_unicode():
    payload = "pässwörd-é \U0001f512"
    assert decrypt(encrypt(payload)) == payload


def test_round_trip_empty_string():
    assert decrypt(encrypt("")) == ""


def test_two_encryptions_differ():
    """Fernet embeds a fresh IV per encryption, so the ciphertext should never match."""
    assert encrypt("same") != encrypt("same")


def test_decrypt_garbage_raises():
    with pytest.raises(ValueError, match="Failed to decrypt"):
        decrypt("not-a-valid-token")
