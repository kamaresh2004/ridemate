"""Database-index startup safety regression tests.

Correctness-critical indexes (uniqueness and GeoJSON) must refuse a production
boot when they cannot be built; performance-only indexes stay best-effort but
must still be visible in the logs. See backend/db.py:_ensure_index.
"""

import logging

import pytest

from backend import db as dbmodule
from backend.db import _payment_booking_unique


class _FailingIndexCollection:
    def index_information(self):
        return {}

    def aggregate(self, pipeline, allowDiskUse=False):
        return []

    def create_index(self, *args, **kwargs):
        raise RuntimeError("index build failed")


def test_production_payment_uniqueness_index_failure_is_fatal():
    with pytest.raises(RuntimeError, match="index build failed"):
        _payment_booking_unique(_FailingIndexCollection(), strict=True)


def test_nonproduction_payment_uniqueness_index_failure_can_retry_on_restart():
    # Development keeps the existing retry-on-next-start behavior.
    _payment_booking_unique(_FailingIndexCollection(), strict=False)


# (collection, key spec, human label) for every index the app's correctness
# depends on: the two unique identity/money indexes named in the hardening task
# plus the geo and idempotency indexes guarded alongside them.
_CRITICAL_INDEXES = [
    ("rides", [("origin_location", "2dsphere")], "origin_location 2dsphere"),
    ("rides", [("destination_location", "2dsphere")], "destination_location 2dsphere"),
    ("payouts", [("user_id", 1), ("period.auto_key", 1)],
     "(user_id, period.auto_key) unique"),
    ("webhook_events", [("provider", 1), ("event_id", 1)],
     "(provider, event_id) unique"),
    ("ratings", [("booking_id", 1), ("reviewer_id", 1), ("rated_user_id", 1)],
     "(booking_id, reviewer_id, rated_user_id) unique"),
    ("blocks", [("blocker_id", 1), ("blocked_id", 1)],
     "(blocker_id, blocked_id) unique"),
]

_PERFORMANCE_INDEX = (
    "payouts", [("user_id", 1), ("status", 1), ("created_at", 1)]
)


def _fail_one_index_build(monkeypatch, collection, keys):
    """Make exactly one collection/key-spec create_index call raise."""
    from pymongo.collection import Collection

    real_create_index = Collection.create_index

    def create_index(self, index, *args, **kwargs):
        wanted = index if isinstance(index, str) else list(index)
        if self.name == collection and wanted == keys:
            raise RuntimeError("index build failed")
        return real_create_index(self, index, *args, **kwargs)

    monkeypatch.setattr(Collection, "create_index", create_index)


@pytest.mark.parametrize("collection,keys,label", _CRITICAL_INDEXES)
def test_critical_index_failure_refuses_production_startup(
        app, monkeypatch, collection, keys, label):
    _fail_one_index_build(monkeypatch, collection, keys)

    with pytest.raises(RuntimeError, match="index build failed"):
        dbmodule.create_indexes(strict=True)


@pytest.mark.parametrize("collection,keys,label", _CRITICAL_INDEXES)
def test_critical_index_failure_only_logs_in_development(
        app, monkeypatch, caplog, collection, keys, label):
    _fail_one_index_build(monkeypatch, collection, keys)

    with caplog.at_level(logging.ERROR, logger="backend.db"):
        dbmodule.create_indexes(strict=False)

    assert any(
        record.levelno == logging.ERROR
        and label in record.getMessage()
        and collection in record.getMessage()
        for record in caplog.records
    )


def test_performance_index_failure_is_best_effort_in_production(
        app, monkeypatch, caplog):
    collection, keys = _PERFORMANCE_INDEX
    _fail_one_index_build(monkeypatch, collection, keys)

    with caplog.at_level(logging.WARNING, logger="backend.db"):
        dbmodule.create_indexes(strict=True)

    assert any(
        record.levelno == logging.WARNING
        and collection in record.getMessage()
        and "(user_id, status, created_at)" in record.getMessage()
        for record in caplog.records
    )


def test_location_ttl_failure_is_best_effort_in_production(
        app, monkeypatch, caplog):
    _fail_one_index_build(monkeypatch, "locations", [("timestamp", 1)])

    with caplog.at_level(logging.WARNING, logger="backend.db"):
        dbmodule.create_indexes(strict=True)

    assert any(
        "locations" in record.getMessage()
        and "TTL" in record.getMessage()
        for record in caplog.records
    )
