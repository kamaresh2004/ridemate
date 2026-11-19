"""MongoDB connection, indexes, and serialization helpers."""

import logging
from datetime import datetime

from bson import ObjectId
from bson.errors import InvalidId

from .errors import APIError
from .timeutil import utc_now, iso_utc

_db = None
_client = None
_active_uri = None

_LOGGER = logging.getLogger(__name__)


def _redact(uri: str) -> str:
    """Strip any embedded credentials so URIs are safe to log."""
    if "@" not in uri or "//" not in uri:
        return uri
    scheme, _, rest = uri.partition("//")
    _, _, host = rest.rpartition("@")
    return f"{scheme}//***@{host}"


def init_db(app):
    """Connect to the first reachable candidate in MONGO_URI_FALLBACKS."""
    global _db, _client, _active_uri
    from pymongo import MongoClient

    candidates = app.config.get("MONGO_URI_FALLBACKS") or [app.config["MONGO_URI"]]
    timeout = app.config["MONGO_SERVER_SELECTION_TIMEOUT_MS"]
    failures = []

    if _client is not None:
        try:
            _client.admin.command("ping")
            return _db
        except Exception:  # noqa: BLE001 - stale client, fall through to reconnect
            app.logger.info("Existing MongoDB client is stale; reconnecting.")

    for uri in candidates:
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=timeout)
            client.admin.command("ping")  # raises if unreachable
            _client, _db, _active_uri = client, client[app.config["MONGO_DB_NAME"]], uri
            create_indexes(strict=app.config.get("ENV") == "production")
            app.logger.info("MongoDB connected: %s via %s", app.config["MONGO_DB_NAME"], _redact(uri))
            return _db
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            app.logger.warning("MongoDB candidate unreachable, trying next: %s (%s)", _redact(uri), exc)
            failures.append(f"{_redact(uri)}: {type(exc).__name__}")

    _db = None
    app.logger.error(
        "No MongoDB candidate reachable. Tried -> %s. API will report database=false.",
        " | ".join(failures) or "none configured",
    )
    return None


def active_uri() -> str | None:
    return _active_uri


def get_db():
    if _db is None:
        raise APIError("Database is unavailable.", 503, code="db_unavailable")
    return _db


def _unique_string_index(coll, field, name=None):
    """Unique index that applies only when the field holds a non-empty string.

    Legacy code stored `None` in these fields and used a `sparse` unique index;
    sparse still rejects duplicate explicit-null values (a `null` value IS an
    indexed value), which made e.g. a second booking on a ride fail with an
    E11000. This upgrades/recreates such indexes as partial-on-string.
    """
    name = name or f"{field}_1"
    try:
        info = coll.index_information()
    except Exception:  # noqa: BLE001 - collection may not exist yet
        info = {}
    idx = info.get(name) or {}
    key_spec = idx.get("key")
    if isinstance(key_spec, dict):
        key_spec = list(key_spec.items())
    keys = {(str(f).split(".")[0], k) for f, k in (key_spec or [])}
    if keys == {(field, 1)} and idx.get("unique") and "partialFilterExpression" in idx:
        return
    if idx:
        coll.drop_index(name)
    coll.create_index([(field, 1)], unique=True,
                      partialFilterExpression={field: {"$type": "string"}})


def _payment_booking_unique(coll, strict=False):
    """At most ONE payment document per booking, enforced by the database.

    This index is what makes `create_order` safe. Order creation must claim the
    booking BEFORE calling the gateway, and that claim is only atomic if the
    database rejects the second writer. With a plain (non-unique) index two
    concurrent requests both "succeed", producing two payment rows for one
    booking and two live orders at the gateway.

    Legacy databases created before this constraint may already hold duplicates
    (the old code swallowed the insert error), so they are collapsed first --
    keeping the most advanced document, which is the one with real money
    movement behind it.
    """
    name = "booking_id_1"
    try:
        info = coll.index_information()
    except Exception:  # noqa: BLE001
        info = {}
    existing = info.get(name)
    if existing and existing.get("unique"):
        return

    # Keep the most advanced row per booking: success > any other status, then
    # the newest. Superseded rows are removed so the unique index can be built.
    pipeline = [
        {"$sort": {"status": -1, "created_at": -1}},
        {"$group": {"_id": "$booking_id", "keep": {"$first": "$_id"},
                    "drop": {"$push": "$_id"}}},
        {"$project": {"drop": 1, "_id": 0}},
    ]
    try:
        for group in coll.aggregate(pipeline, allowDiskUse=True):
            drop = [d for d in group.get("drop", []) if d != group["keep"]]
            if drop:
                coll.delete_many({"_id": {"$in": drop}})
    except Exception:  # noqa: BLE001 - no duplicates is the common case
        if strict:
            raise

    if existing:
        coll.drop_index(name)
    try:
        coll.create_index([("booking_id", 1)], unique=True)
    except Exception:  # noqa: BLE001 - concurrent index build; next start retries
        if strict:
            raise


_ACTIVE_BOOKING_STATES = ("pending_payment", "payment_failed", "confirmed")


def _booking_active_unique(coll):
    """A rider can hold at most ONE active (seat-holding) booking per ride.

    The index is partial on ride_id+rider_id: it only covers active states, so
    a rider may rebook after a cancellation/refund/completion but duplicate
    ACTIVE bookings (double-click, concurrent POSTs) are rejected atomically.
    This replaces the legacy permanent-unique (ride_id, rider_id) index which
    locked a rider out forever after a single cancel.
    """
    name = "ride_id_1_rider_id_1"
    try:
        info = coll.index_information()
    except Exception:  # noqa: BLE001
        info = {}
    idx = info.get(name) or {}
    key_spec = idx.get("key")
    if isinstance(key_spec, dict):
        key_spec = list(key_spec.items())
    keys = {(str(f).split(".")[0], k) for f, k in (key_spec or [])}
    expected_partial = {"status": {"$in": list(_ACTIVE_BOOKING_STATES)}}
    if keys == {("ride_id", 1), ("rider_id", 1)} and idx.get("unique") \
            and idx.get("partialFilterExpression") == expected_partial:
        return
    if idx:
        coll.drop_index(name)
    coll.create_index(
        [("ride_id", 1), ("rider_id", 1)], unique=True,
        partialFilterExpression={"status": {"$in": list(_ACTIVE_BOOKING_STATES)}})


def _ensure_index(collection, label, builder, *, critical=False, strict=False):
    """Build one index, surfacing failures instead of swallowing them.

    ``critical`` marks an index the application's correctness depends on:
    uniqueness (duplicate money/identity rows become possible without it) or
    geo queries (live tracking and nearby search silently return nothing).
    In production (``strict``) a failed critical index refuses to boot rather
    than running with weaker guarantees. Performance-only indexes stay
    best-effort, but a failure is still logged with collection, index and error
    so an operator can see it.
    """
    try:
        builder()
    except Exception as exc:  # noqa: BLE001 - startup index policy
        _LOGGER.log(
            logging.ERROR if critical else logging.WARNING,
            "Index %s on %s failed to build: %s",
            label, collection, exc,
            exc_info=critical,
        )
        if critical and strict:
            raise


def create_indexes(strict=False):
    users = _db.users
    # Email is optional now: a rider can sign up with a phone number and no
    # inbox. A plain unique index treats every missing field as NULL and allows
    # exactly one such account, so it must be unique-on-non-empty-string instead.
    _unique_string_index(users, "email")
    # A phone number is a login credential, so it identifies one account. The
    # partial filter keeps accounts that have not added a number out of the way.
    _unique_string_index(users, "phone")
    users.create_index([("google_id", 1)], unique=True, sparse=True)
    users.create_index([("role", 1), ("created_at", -1)])

    # Outstanding one-time codes. TTL on expires_at lets Mongo expire them even
    # if this process dies between issuing and verifying; the code itself is
    # never stored, only its HMAC (see backend/otp.py).
    otps = _db.phone_otps
    otps.create_index("phone", unique=True)
    otps.create_index("expires_at", expireAfterSeconds=0)

    vehicles = _db.vehicles
    vehicles.create_index([("user_id", 1), ("created_at", -1)])
    vehicles.create_index("vehicle_number", unique=True)
    vehicles.create_index([("user_id", 1), ("verification_status", 1)])

    rides = _db.rides
    rides.create_index([("owner_id", 1), ("departure_at", -1)])
    rides.create_index([("status", 1), ("departure_at", 1)])
    rides.create_index([("origin.label", 1), ("destination.label", 1), ("departure_date", 1)])
    rides.create_index([("vehicle_id", 1), ("status", 1), ("departure_at", 1)])
    # GeoJSON Point fields written by create_ride (origin_location /
    # destination_location). Legacy documents without them are excluded from
    # geo searches until migrated. Production must not start without these:
    # validated live tracking and radius search would silently return nothing.
    _ensure_index(
        "rides", "origin_location 2dsphere",
        lambda: rides.create_index([("origin_location", "2dsphere")]),
        critical=True, strict=strict)
    _ensure_index(
        "rides", "destination_location 2dsphere",
        lambda: rides.create_index([("destination_location", "2dsphere")]),
        critical=True, strict=strict)

    bookings = _db.bookings
    bookings.create_index([("rider_id", 1), ("created_at", -1)])
    _booking_active_unique(bookings)
    bookings.create_index([("status", 1), ("ride_id", 1)])
    _unique_string_index(bookings, "idempotency_key")

    payments = _db.payments
    _unique_string_index(payments, "provider_reference")
    _unique_string_index(payments, "idempotency_key")
    _payment_booking_unique(payments, strict=strict)

    refunds = _db.refunds
    # Legacy databases may already have the conventional payment_id_1 lookup
    # index. Avoid trying to create an equivalent non-unique index under a new
    # name, which MongoDB rejects as IndexOptionsConflict. A basic index and a
    # unique partial index may share this key pattern; give the invariant its
    # own stable name so existing databases can migrate without data deletion.
    try:
        refund_indexes = refunds.index_information()
    except Exception:  # noqa: BLE001 - collection may not exist yet
        refund_indexes = {}
    has_payment_id_index = False
    for index in refund_indexes.values():
        key_spec = index.get("key") or []
        if isinstance(key_spec, dict):
            key_spec = list(key_spec.items())
        if list(key_spec) == [("payment_id", 1)]:
            has_payment_id_index = True
            break
    if not has_payment_id_index:
        refunds.create_index([("payment_id", 1)], name="payment_id_lookup")

    _unique_string_index(refunds, "provider_reference")
    _unique_string_index(refunds, "idempotency_key")
    try:
        # At most one live/processed refund per payment -> concurrent refund
        # claims are resolved atomically by the unique index (E11000).
        refunds.create_index(
            [("payment_id", 1)], name="uniq_active_refund_per_payment", unique=True,
            partialFilterExpression={"status": {"$in": ["processing", "processed"]}})
    except Exception:  # noqa: BLE001 - uniqueness protects concurrent refunds
        if strict:
            raise

    webhook_events = _db.webhook_events
    _ensure_index(
        "webhook_events", "(provider, event_id) unique",
        lambda: webhook_events.create_index(
            [("provider", 1), ("event_id", 1)], unique=True),
        critical=True, strict=strict)
    webhook_events.create_index([("provider", 1), ("first_received_at", -1)])

    ledger_entries = _db.ledger_entries
    ledger_entries.create_index([("account_id", 1), ("created_at", 1)])
    ledger_entries.create_index([("booking_id", 1)])
    ledger_entries.create_index("entry_id", unique=True)
    # One financial effect, one entry. Every entry that represents a money
    # movement carries a `business_key` naming that effect, and this index makes
    # the database refuse a second one. Application-level guards (payment
    # claims, refund claims, payout period keys) all race; this does not.
    #
    # Partial on "a key exists" so any entry posted without one is still
    # allowed -- a missing key must not block a write, it just forgoes the
    # protection.
    _unique_string_index(ledger_entries, "business_key", name="uniq_ledger_business_key")

    # Immutable record of every gateway reconciliation: what was verified, from
    # which source, against which order/payment/event. The unique business_key
    # is what makes a duplicated callback, a retried webhook and an
    # out-of-order callback/webhook pair converge on one capture with one row.
    payment_reconciliations = _db.payment_reconciliations
    _unique_string_index(payment_reconciliations, "business_key",
                         name="uniq_payment_recon_business_key")
    payment_reconciliations.create_index([("payment_id", 1), ("verified_at", -1)])
    payment_reconciliations.create_index([("razorpay_payment_id", 1)])
    payment_reconciliations.create_index([("razorpay_order_id", 1)])
    payment_reconciliations.create_index([("verification_source", 1), ("verified_at", -1)])

    payouts = _db.payouts
    payouts.create_index([("user_id", 1), ("created_at", -1)])
    _unique_string_index(payouts, "reference")
    # One payout per (user, settlement period). The worker checks for an
    # existing payout for the period before creating one, but a check followed
    # by an insert is a race two concurrent workers can both win -- and paying a
    # driver twice for one week's earnings is not a recoverable bug. The unique
    # index makes the database the arbiter instead of the application.
    _ensure_index(
        "payouts", "(user_id, period.auto_key) unique",
        lambda: payouts.create_index(
            [("user_id", 1), ("period.auto_key", 1)], unique=True,
            partialFilterExpression={"period.auto_key": {"$type": "string"}}),
        critical=True, strict=strict)
    # The settlement worker's eligibility scan: oldest completed booking per
    # driver, filtered by completion state.
    _ensure_index(
        "payouts", "(user_id, status, created_at)",
        lambda: payouts.create_index(
            [("user_id", 1), ("status", 1), ("created_at", 1)]),
        strict=strict)

    # KYC / payout-onboarding queues and settlement scans. Each of these is a
    # filter the admin screens and the worker run on every pass; without an index
    # they are collection scans that get slower with every row written.
    users.create_index([("kyc_status", 1), ("kyc_submitted_at", 1)])
    users.create_index([("payout_onboarding_status", 1),
                        ("payout_onboarding_updated_at", 1)])
    # One identity number, one account, per KYC role. The fingerprint is a
    # keyed HMAC of the document number, so this is unique WITHOUT storing the
    # number itself in a queryable field. Uniqueness is scoped to the role
    # because the fingerprint mixes it in: a person who drives and also rides
    # legitimately holds a driver record and a passenger record, and a global
    # unique index would reject the second one.
    #
    # Partial on "a fingerprint exists" so the many accounts that have never
    # submitted identity KYC do not all collide on a null.
    users.create_index([("kyc_fingerprint", 1)], unique=True, name="uniq_kyc_fingerprint",
                       partialFilterExpression={"kyc_fingerprint": {"$type": "string"}})
    vehicles.create_index([("rc_document.verification_status", 1),
                           ("rc_document.uploaded_at", 1)])
    vehicles.create_index([("user_id", 1), ("rc_document.verification_status", 1)])

    # Automated KYC checks. The worker claims the oldest due job; the queue view
    # and the "is there already a job for this vehicle" check both need the
    # vehicle/status pair. Consent is looked up by user + purpose + version, and
    # kept append-only so the acceptance itself is the evidence.
    kyc_jobs = _db.kyc_jobs
    kyc_jobs.create_index([("status", 1), ("next_attempt_at", 1)])
    kyc_jobs.create_index([("vehicle_id", 1), ("status", 1)])
    ocr_jobs = _db.ocr_jobs
    ocr_jobs.create_index([("status", 1), ("created_at", 1)])
    ocr_jobs.create_index([("vehicle_id", 1), ("status", 1)])
    kyc_consents = _db.kyc_consents
    kyc_consents.create_index([("user_id", 1), ("purpose", 1), ("timestamp", -1)])

    # Completion window scan: the worker finds trips whose confirmation window
    # has lapsed. Without this it reads every booking on every pass.
    _ensure_index(
        "bookings", "(completion_status, completion_deadline)",
        lambda: bookings.create_index(
            [("completion_status", 1), ("completion_deadline", 1)]),
        strict=strict)
    # The dispute queue, sorted oldest-first.
    bookings.create_index([("status", 1), ("disputed_at", 1)])

    refresh_tokens = _db.refresh_tokens
    refresh_tokens.create_index("jti", unique=True)
    refresh_tokens.create_index([("user_id", 1), ("revoked", 1)])
    refresh_tokens.create_index([("expires_at", 1)], expireAfterSeconds=0)

    notifications = _db.notifications
    notifications.create_index([("user_id", 1), ("created_at", -1)])
    notifications.create_index([("user_id", 1), ("read", 1)])

    notification_preferences = _db.notification_preferences
    notification_preferences.create_index([("user_id", 1)], unique=True)

    ratings = _db.ratings
    ratings.create_index("booking_id")
    ratings.create_index([("rated_user_id", 1), ("created_at", -1)])
    _ensure_index(
        "ratings", "(booking_id, reviewer_id, rated_user_id) unique",
        lambda: ratings.create_index(
            [("booking_id", 1), ("reviewer_id", 1), ("rated_user_id", 1)],
            unique=True,
            partialFilterExpression={"booking_id": {"$type": "objectId"}},
        ),
        critical=True, strict=strict)

    locations = _db.locations
    locations.create_index([("ride_id", 1), ("timestamp", 1)])
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            ttl_hours = current_app.config.get("LOCATION_HISTORY_TTL_HOURS", 24) or 0
        else:
            ttl_hours = 24
        if ttl_hours > 0:
            _ensure_index(
                "locations", "(timestamp) TTL",
                lambda: locations.create_index(
                    [("timestamp", 1)],
                    expireAfterSeconds=int(ttl_hours * 3600)),
                strict=strict)
    except Exception as exc:  # noqa: BLE001 - the TTL is best-effort, never fatal
        _LOGGER.warning("Index (timestamp) TTL on locations failed to build: %s", exc)

    audit_logs = _db.audit_logs
    audit_logs.create_index([("actor_id", 1), ("created_at", -1)])

    blocks = _db.blocks
    _ensure_index(
        "blocks", "(blocker_id, blocked_id) unique",
        lambda: blocks.create_index(
            [("blocker_id", 1), ("blocked_id", 1)], unique=True),
        critical=True, strict=strict)

    reports = _db.reports
    reports.create_index([("target_user_id", 1), ("created_at", -1)])
    reports.create_index([("status", 1), ("created_at", -1)])
    audit_logs.create_index([("action", 1), ("created_at", -1)])
    # "Every event that can move money", which is the query an operator runs
    # first. Also covers resolving a single trip's money history.
    audit_logs.create_index([("target_type", 1), ("target_id", 1), ("created_at", -1)])


def to_object_id(raw, label="id"):
    try:
        return ObjectId(str(raw))
    except (InvalidId, TypeError):
        raise APIError(f"Invalid {label}.", 400, code="invalid_id")


def utcnow():
    """Back-compat alias. Canonical now = naive-UTC instant (see timeutil)."""
    return utc_now()


def clean(value):
    """Recursively convert ObjectId -> str and datetime -> ISO for JSON."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return str(value)


def public_ride(ride):
    ride = dict(ride)
    return clean(ride)