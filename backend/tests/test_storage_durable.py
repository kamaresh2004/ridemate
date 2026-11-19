"""Durable blob storage.

A hosted container's filesystem is wiped on every deploy, which silently
orphaned uploaded KYC documents while their metadata stayed in MongoDB -- the
staff console then answered "File not found" for a document that had definitely
been uploaded. The mongo backend keeps the bytes where the metadata already
lives, so they survive restarts.
"""

import pytest

from backend import storage
from backend.config import Config
from backend.errors import APIError


def test_mongo_backend_round_trip(app, monkeypatch):
    monkeypatch.setattr(Config, "STORAGE_BACKEND", "mongo")
    key = "private/test/roundtrip.bin"
    with app.app_context():
        try:
            storage.save_to_key(key, b"sealed-bytes", "image/jpeg")
            data, content_type, name = storage.read_key(key)
            assert data == b"sealed-bytes"
            assert content_type == "image/jpeg"
            assert name == "roundtrip.bin"

            # Re-saving the same key replaces, never duplicates.
            storage.save_to_key(key, b"second", "image/png")
            assert storage.read_key(key)[0] == b"second"

            storage.delete_key(key)
            with pytest.raises(APIError) as err:
                storage.read_key(key)
            assert err.value.code == "not_found"
        finally:
            storage.delete_key(key)


def test_mongo_avatars_are_served_through_the_api(app, monkeypatch):
    monkeypatch.setattr(Config, "STORAGE_BACKEND", "mongo")
    assert storage.public_avatar_url("avatars/u1/a.png") == "/api/uploads/avatar?key=avatars/u1/a.png"
