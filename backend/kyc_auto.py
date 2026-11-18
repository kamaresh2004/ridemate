"""Automated driver-KYC checks that sit in front of the manual review queue.

The manual flow in `backend/kyc.py` stays authoritative. This module only
answers "what do the supplied documents say?" and attaches the answer to the
vehicle as `auto_check`; a human still makes the decision unless
`KYC_AUTO_APPROVE` is explicitly enabled, and even then nothing is ever
auto-rejected.

    submit_for_verification
        -> consent check (no consent = no lookup)
        -> kyc_jobs row queued
        -> worker claims the job, calls the provider
        -> pass         -> verified only if KYC_AUTO_APPROVE, else manual queue
        -> fail         -> manual queue, reasons attached (never rejected)
        -> inconclusive -> manual queue, reasons attached
        -> provider error -> retry with backoff (max KYC_MAX_ATTEMPTS) -> manual

Data-protection rules enforced here, not just documented:

* A consent row `{user_id, purpose, text_version, timestamp, ip}` must exist for
  the *current* consent text version before a job is created.
* Provider payloads are never stored or logged. `provider_ref` is an opaque id
  and `reasons` are short fix-it strings, never document numbers.
* Finished jobs and the per-check detail are purged after `KYC_RETENTION_DAYS`.
"""

import logging
from datetime import timedelta

from flask import current_app
from pymongo import ReturnDocument

from . import audit, notifications
from .db import get_db, utcnow
from .errors import APIError
from .kyc_provider import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    REASON_PROVIDER_UNAVAILABLE,
    ProviderError,
    get_provider,
    provider_name,
)

log = logging.getLogger(__name__)

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_RETRY = "retry"
JOB_DONE = "done"
JOB_FAILED = "failed"

CONSENT_PURPOSE = "driver_kyc"

CHECKING = "checking"
UNAVAILABLE = "unavailable"


def provider_enabled():
    """True when an automated provider is configured (anything but `manual`)."""
    return provider_name() != "manual"


def consent_version():
    return str(current_app.config.get("KYC_CONSENT_TEXT_VERSION") or "2026-11-01")


# ------------------------------------------------------------------- consent
def record_consent(user_id, ip=None, purpose=CONSENT_PURPOSE):
    """Append an explicit consent record. Never updates or reuses an old row.

    Keeping one row per acceptance is what makes "which text did they agree to,
    and when" answerable later; a mutable flag could not.
    """
    db = get_db()
    doc = {
        "user_id": user_id,
        "purpose": purpose,
        "text_version": consent_version(),
        "timestamp": utcnow(),
        "ip": (str(ip) if ip else None),
    }
    db.kyc_consents.insert_one(doc)
    audit.record("identity.kyc.consent", domain=audit.IDENTITY, actor_id=user_id,
                 actor_role="user", target_type="user", target_id=user_id,
                 meta={"purpose": purpose, "text_version": doc["text_version"]})
    return doc


def has_consent(user_id, purpose=CONSENT_PURPOSE):
    """Whether the *current* consent text has been accepted."""
    row = get_db().kyc_consents.find_one(
        {"user_id": user_id, "purpose": purpose,
         "text_version": consent_version()},
        sort=[("timestamp", -1)])
    return row is not None


def assert_consent_ready(user_id):
    """Fail a submission early when a provider lookup would be unauthorised.

    Called before the vehicle moves to `submitted`, so a driver is never left in
    a state where the automated check has silently been skipped for want of
    consent. With `KYC_PROVIDER=manual` this is a no-op and behaviour is exactly
    what it was before this module existed.
    """
    if not provider_enabled():
        return
    if not has_consent(user_id):
        raise APIError(
            "Give consent for document checks before submitting for verification.",
            403, code="kyc_consent_required")


# ------------------------------------------------------------------ job queue
def enqueue(vehicle_id, user_id, trigger="submit"):
    """Queue one automated check for a vehicle.

    Idempotent while a job for the vehicle is still in flight: a second submit
    (or a document re-upload) refreshes the existing job rather than stacking
    provider calls.
    """
    if not provider_enabled():
        return None
    if not has_consent(user_id):
        # The caller normally checks first; this is the belt-and-braces path so
        # no other future call site can create a lookup without consent.
        return None

    db = get_db()
    now = utcnow()
    existing = db.kyc_jobs.find_one(
        {"vehicle_id": vehicle_id, "status": {"$in": [JOB_QUEUED, JOB_RUNNING]}})
    if existing:
        db.kyc_jobs.update_one(
            {"_id": existing["_id"]},
            {"$set": {"next_attempt_at": now, "trigger": trigger,
                      "updated_at": now}})
        db.vehicles.update_one(
            {"_id": vehicle_id},
            {"$set": {"auto_check": {"status": CHECKING, "checks": {},
                                     "reasons": [], "provider": provider_name(),
                                     "attempts": int(existing.get("attempts") or 0),
                                     "updated_at": now}}})
        return existing["_id"]

    job = {
        "vehicle_id": vehicle_id,
        "user_id": user_id,
        "status": JOB_QUEUED,
        "attempts": 0,
        "trigger": trigger,
        "provider": provider_name(),
        "next_attempt_at": now,
        "claimed_at": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }
    job_id = db.kyc_jobs.insert_one(job).inserted_id
    db.vehicles.update_one(
        {"_id": vehicle_id},
        {"$set": {"auto_check": {"status": CHECKING, "checks": {}, "reasons": [],
                                 "provider": provider_name(), "attempts": 0,
                                 "updated_at": now}}})
    return job_id


def on_submit(vehicle_id, user_id):
    """Hook for `submit_for_verification` after the state flip."""
    return enqueue(vehicle_id, user_id, trigger="submit")


def on_documents_changed(vehicle_id, user_id):
    """Re-trigger after a document re-upload when the packet is complete.

    The upload itself has already reset verification to `unverified`; this only
    starts a fresh check when there is enough to check, so uploading a licence
    first does not fire a doomed job.
    """
    if not provider_enabled():
        return None
    from .kyc import missing_requirements

    vehicle = get_db().vehicles.find_one({"_id": vehicle_id, "user_id": user_id})
    if not vehicle or missing_requirements(vehicle):
        return None
    return enqueue(vehicle_id, user_id, trigger="document_reupload")


def _release_stale(now):
    """Return jobs from a worker that died mid-check to the queue."""
    ttl = int(current_app.config.get("KYC_CLAIM_TTL_SECONDS", 300) or 300)
    cutoff = now - timedelta(seconds=max(ttl, 0))
    get_db().kyc_jobs.update_many(
        {"status": JOB_RUNNING, "claimed_at": {"$lte": cutoff}},
        {"$set": {"status": JOB_QUEUED, "claimed_at": None, "next_attempt_at": now,
                  "updated_at": now}})


def _claim(now):
    job = get_db().kyc_jobs.find_one_and_update(
        {"status": {"$in": [JOB_QUEUED, JOB_RETRY]},
         "next_attempt_at": {"$lte": now}},
        {"$set": {"status": JOB_RUNNING, "claimed_at": now, "updated_at": now},
         "$inc": {"attempts": 1}},
        sort=[("next_attempt_at", 1)],
        return_document=ReturnDocument.AFTER)
    return job


def run_pass(limit=25):
    """Run up to `limit` queued checks. Returns the vehicle ids touched."""
    if not provider_enabled():
        return []
    now = utcnow()
    _release_stale(now)
    checked = []
    for _ in range(max(int(limit), 0)):
        job = _claim(now)
        if job is None:
            break
        try:
            outcome = _run_job(job)
        except Exception:  # noqa: BLE001 - one bad packet must not stop the pass
            log.exception("KYC job %s failed unexpectedly", job.get("_id"))
            outcome = _finish_failure(job, now, "unexpected_error")
        if outcome:
            checked.append(str(outcome))
    return checked


def _run_job(job):
    vehicle = get_db().vehicles.find_one({"_id": job["vehicle_id"]})
    user = get_db().users.find_one({"_id": job["user_id"]})
    # A vehicle that was deleted or reset while queued is simply not checked --
    # except after a document re-upload, where the reset to `unverified` is the
    # expected state and the check is exactly what should run. Auto-approval is
    # still gated on `submitted`, so this path can never verify on its own.
    status = (vehicle or {}).get("verification_status")
    allowed = status == "submitted" or (
        status == "unverified" and job.get("trigger") == "document_reupload")
    if not vehicle or not user or not allowed:
        _finish(job, JOB_DONE, now=utcnow())
        return None

    provider = get_provider()
    if provider is None:
        _finish(job, JOB_DONE, now=utcnow())
        return None

    documents = {
        "dl": vehicle.get("dl_document") or {},
        "insurance": vehicle.get("insurance_document") or {},
        "rc": vehicle.get("rc_document") or {},
    }
    try:
        result = provider.verify_driver(user, vehicle, documents)
    except ProviderError as exc:
        # Provider downtime is never a rejection; it is a retry, then a human.
        return _finish_failure(job, utcnow(), type(exc).__name__)

    return _store_result(job, vehicle, result, utcnow())


def _finish_failure(job, now, error_label):
    """Retry with exponential backoff, then route to manual review."""
    max_attempts = int(current_app.config.get("KYC_MAX_ATTEMPTS", 3) or 3)
    base_cfg = current_app.config.get("KYC_RETRY_BASE_SECONDS")
    # 0 is a valid setting (tests use it to retry immediately); `or 30` would
    # silently turn an instant retry into a half-minute wait.
    base = 30 if base_cfg is None else int(base_cfg)
    attempts = int(job.get("attempts") or 0)

    if attempts < max_attempts:
        delay = base * (2 ** max(attempts - 1, 0))
        get_db().kyc_jobs.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": JOB_RETRY,
                      "next_attempt_at": now + timedelta(seconds=delay),
                      "last_error": str(error_label)[:120],
                      "updated_at": now}})
        audit.record(
            "identity.kyc.auto_check_retry", domain=audit.IDENTITY,
            actor_role="system", target_type="vehicle",
            target_id=job["vehicle_id"], outcome="retry",
            reason="provider_error",
            meta={"attempt": attempts, "max_attempts": max_attempts,
                  "error": str(error_label)[:80]})
        return None

    auto_check = {
        "status": UNAVAILABLE,
        "checks": {},
        "reasons": [REASON_PROVIDER_UNAVAILABLE],
        "provider": provider_name(),
        "attempts": attempts,
        "checked_at": now,
        "updated_at": now,
    }
    get_db().vehicles.update_one({"_id": job["vehicle_id"]},
                                 {"$set": {"auto_check": auto_check}})
    _finish(job, JOB_FAILED, now=now)
    audit.record(
        "identity.kyc.auto_check", domain=audit.IDENTITY, actor_role="system",
        target_type="vehicle", target_id=job["vehicle_id"], outcome="inconclusive",
        reason="provider_unavailable",
        meta={"attempts": attempts, "provider": provider_name()})
    _notify_owner(job["user_id"], job["vehicle_id"], auto_check,
                  manual_review=True)
    return job["vehicle_id"]


def _store_result(job, vehicle, result, now):
    auto_check = {
        "status": result["status"],
        "checks": result.get("checks") or {},
        "reasons": result.get("reasons") or [],
        "provider": provider_name(),
        "provider_ref": result.get("provider_ref") or "",
        "attempts": int(job.get("attempts") or 0),
        "checked_at": now,
        "updated_at": now,
    }

    auto_approved = False
    if result["status"] == PASS and current_app.config.get("KYC_AUTO_APPROVE"):
        updated = get_db().vehicles.find_one_and_update(
            {"_id": vehicle["_id"], "verification_status": "submitted"},
            {"$set": {"verification_status": "verified",
                      "kyc_reviewed_at": now,
                      "kyc_reviewed_by": "auto:" + provider_name(),
                      "kyc_reason": None,
                      "auto_check": auto_check,
                      "updated_at": now}},
            return_document=ReturnDocument.AFTER)
        auto_approved = updated is not None

    if not auto_approved:
        # pass-without-auto-approve, fail and inconclusive all stay in the
        # manual queue. The detail travels with the packet so the reviewer sees
        # exactly which check failed, and the driver gets fix-it reasons.
        get_db().vehicles.update_one({"_id": vehicle["_id"]},
                                     {"$set": {"auto_check": auto_check,
                                               "updated_at": now}})

    _finish(job, JOB_DONE, now=now)
    audit.record(
        "identity.kyc.auto_check", domain=audit.IDENTITY, actor_role="system",
        target_type="vehicle", target_id=vehicle["_id"],
        outcome=("verified" if auto_approved else result["status"]),
        reason="; ".join(auto_check["reasons"]) or None,
        meta={"provider": auto_check["provider"],
              "provider_ref": auto_check["provider_ref"],
              "checks": auto_check["checks"],
              "attempts": auto_check["attempts"],
              "auto_approved": auto_approved})
    _notify_owner(job["user_id"], vehicle["_id"], auto_check,
                  manual_review=not auto_approved)
    return vehicle["_id"]


def _finish(job, status, now):
    get_db().kyc_jobs.update_one(
        {"_id": job["_id"]},
        {"$set": {"status": status, "claimed_at": None, "updated_at": now}})


def _notify_owner(user_id, vehicle_id, auto_check, *, manual_review):
    """Push a status event; the Captain page polls this same state."""
    status = auto_check.get("status")
    reasons = auto_check.get("reasons") or []
    if status == PASS and not manual_review:
        title, body = "Vehicle verified", "Your documents were checked and the vehicle is verified."
    elif status == PASS:
        title = "Documents checked"
        body = "Your documents passed the automated check and are queued for final review."
    elif status == UNAVAILABLE:
        title = "Manual review needed"
        body = "The document check service was unavailable, so a reviewer will check your vehicle."
    elif status == FAIL:
        title = "Vehicle check needs attention"
        body = "; ".join(reasons) or "A document check did not pass. A reviewer will confirm."
    else:
        title = "Vehicle check needs a reviewer"
        body = "; ".join(reasons) or "A reviewer will look at your documents."

    notifications.notify(
        user_id, title, body, ref_type="kyc_vehicle", ref_id=vehicle_id,
        data={"event": "kyc.status", "scope": "vehicle", "status": status,
              "vehicle_id": str(vehicle_id), "reasons": reasons})


# -------------------------------------------------------------------- webhook
def apply_webhook(payload):
    """Apply a provider callback that has already been authenticated.

    Only ever moves a vehicle *forward* to verified (and only with
    KYC_AUTO_APPROVE on). A negative callback is recorded for the reviewer but
    never auto-rejects; that is the manual queue's job.
    """
    vehicle_id = payload.get("vehicle_id")
    if not vehicle_id:
        return {"ok": True, "ignored": "no_vehicle"}
    try:
        from bson import ObjectId

        oid = ObjectId(vehicle_id)
    except Exception:  # noqa: BLE001 - an unknown id is not an error worth 500
        return {"ok": True, "ignored": "bad_vehicle_id"}

    vehicle = get_db().vehicles.find_one({"_id": oid})
    if not vehicle:
        return {"ok": True, "ignored": "vehicle_not_found"}

    status = str(payload.get("status") or "").lower()
    status = {"passed": PASS, "failed": FAIL, "unknown": INCONCLUSIVE}.get(status, status)
    if status not in (PASS, FAIL, INCONCLUSIVE):
        return {"ok": True, "ignored": "unknown_status"}

    now = utcnow()
    auto_check = {
        "status": status,
        "checks": payload.get("checks") or {},
        "reasons": payload.get("reasons") or [],
        "provider": provider_name(),
        "provider_ref": payload.get("provider_ref") or "",
        "attempts": 0,
        "checked_at": now,
        "updated_at": now,
    }

    auto_approved = False
    if status == PASS and current_app.config.get("KYC_AUTO_APPROVE"):
        updated = get_db().vehicles.find_one_and_update(
            {"_id": oid, "verification_status": "submitted"},
            {"$set": {"verification_status": "verified",
                      "kyc_reviewed_at": now,
                      "kyc_reviewed_by": "auto:" + provider_name(),
                      "kyc_reason": None, "auto_check": auto_check,
                      "updated_at": now}},
            return_document=ReturnDocument.AFTER)
        auto_approved = updated is not None

    if not auto_approved:
        get_db().vehicles.update_one({"_id": oid},
                                     {"$set": {"auto_check": auto_check,
                                               "updated_at": now}})

    audit.record("identity.kyc.webhook", domain=audit.IDENTITY,
                 actor_role="provider", target_type="vehicle", target_id=oid,
                 outcome=("verified" if auto_approved else status),
                 reason="; ".join(auto_check["reasons"]) or None,
                 meta={"provider": auto_check["provider"],
                       "provider_ref": auto_check["provider_ref"],
                       "auto_approved": auto_approved})
    _notify_owner(vehicle.get("user_id"), oid, auto_check,
                  manual_review=not auto_approved)
    return {"ok": True, "status": status, "auto_approved": auto_approved}


# ------------------------------------------------------------------ retention
def cleanup_retention():
    """Purge raw provider references and finished jobs past the retention window.

    The *decision* survives (the vehicle status, the audit row); only the
    check-level detail and the opaque provider reference age out. That keeps the
    DPDP promise ("keep only what is needed, for as long as it is needed")
    without destroying the record of who was approved and by what.
    """
    days = int(current_app.config.get("KYC_RETENTION_DAYS", 180) or 180)
    cutoff = utcnow() - timedelta(days=max(days, 1))
    db = get_db()

    jobs = db.kyc_jobs.delete_many(
        {"status": {"$in": [JOB_DONE, JOB_FAILED]},
         "updated_at": {"$lt": cutoff}}).deleted_count

    purged = db.vehicles.update_many(
        {"auto_check.checked_at": {"$lt": cutoff}},
        {"$set": {"auto_check.checks": {},
                  "auto_check.reasons": [],
                  "auto_check.provider_ref": "",
                  "auto_check.purged_at": utcnow()}}).modified_count
    if jobs or purged:
        log.info("KYC retention purge: %d job(s), %d vehicle auto_check row(s)",
                 jobs, purged)
    return {"jobs": jobs, "vehicles": purged}
