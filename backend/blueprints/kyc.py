"""Identity KYC, registration certificates, and driver payout onboarding.

Three related but separate review queues, one blueprint, because they share the
same shape: the owner submits a document (or opens a flow), a human decides, and
the decision gates something. What they must never share is a shortcut, so each
one is routed to its own state machine rather than a generic "verified" flag.

* `/api/kyc/*`            identity documents, for drivers and passengers alike
* `/api/vehicles/<id>/rc` registration certificates, for a specific vehicle
* `/api/onboarding/*`     payout account onboarding, for drivers

Two rules hold across all of them:

* **No endpoint lets a user set their own status to approved.** Every route here
  either moves a document *into* review or records a reviewer's decision. The
  user-facing `GET`s return `can_submit`/`can_confirm`-style booleans so the UI
  can render the right next action, but a client cannot act on the approval side.
* **Document bytes never leave through these responses.** Metadata comes from
  `documents.public_metadata`, which drops the storage key; the only way to fetch
  the bytes is `GET .../document`, which re-checks ownership on every read.
"""

import hashlib
import os
import tempfile
import time

from flask import Blueprint, Response, current_app, g, request
from pymongo.errors import DuplicateKeyError

from .. import audit, identity, kyc_auto, onboarding, rc
from ..db import get_db, to_object_id, utcnow
from ..documents import load_private_document, public_metadata
from ..errors import APIError
from ..kyc_provider import provider_name, verify_signature
from ..ratelimit import rate_limit
from ..security import require_admin, require_auth

bp = Blueprint("kyc", __name__, url_prefix="/api/kyc")

_PRECHECK_MAX_BYTES = 5 * 1024 * 1024
_PRECHECK_MIME_TYPES = {"image/jpeg", "image/png"}


def _upload_bytes(field="file"):
    """Read one uploaded file, or raise a typed error."""
    file = request.files.get(field) or request.files.get("document")
    if file is None or not file.filename:
        raise APIError("No document provided.", 422, code="no_file")
    data = file.read()
    if not data:
        raise APIError("No document provided.", 422, code="no_file")
    return data, file.filename


def _precheck_file():
    file = request.files.get("file")
    if file is None or not file.filename:
        return None, None, {"reasons": ["bad_file_type"]}, 400
    data = file.read(_PRECHECK_MAX_BYTES + 1)
    if len(data) > _PRECHECK_MAX_BYTES:
        return None, None, {"reasons": ["file_over_5mb"]}, 413
    try:
        from ..documents import validate_upload

        _ext, mime = validate_upload(
            data, file.filename, max_mb=5, require_image=True)
    except APIError:
        return None, None, {"reasons": ["bad_file_type"]}, 400
    if mime not in _PRECHECK_MIME_TYPES:
        return None, None, {"reasons": ["bad_file_type"]}, 400
    return data, mime, None, None


@bp.post("/precheck/licence")
@require_auth
@rate_limit("precheck")
def precheck_licence():
    """Run a transient licence OCR check; never persist or log the upload."""
    data, mime, error, status = _precheck_file()
    if error:
        return error, status

    path = None
    try:
        suffix = ".png" if mime == "image/png" else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        from ..ocr_check import check_licence

        result = check_licence(
            path, g.user.get("name") or "",
            request.form.get("licence_no") or "")
        return {"reasons": result.get("reasons") or []}
    except Exception:
        return {"reasons": ["unreadable"]}
    finally:
        if path and os.path.exists(path):
            os.remove(path)


@bp.post("/precheck/image")
@require_auth
@rate_limit("precheck")
def precheck_image():
    """Validate RC/insurance image bytes without storing the upload."""
    if request.form.get("kind") not in {"rc", "insurance"}:
        return {"reasons": ["bad_file_type"]}, 400
    _data, _mime, error, status = _precheck_file()
    return (error, status) if error else {"reasons": []}


def _reject_banking_data():
    """Refuse any request carrying banking details or secrets.

    Banking data belongs at the provider. Refusing it here means a future
    frontend change cannot quietly start persisting an account number.
    """
    onboarding.assert_no_banking_data(request.form.to_dict())
    if request.is_json:
        onboarding.assert_no_banking_data(request.get_json(silent=True) or {})


# ================================================================= identity KYC
@bp.get("/me")
@require_auth
def my_kyc():
    """The caller's own identity status. Never any document bytes."""
    user_id = g.user["_id"]
    return {
        "ok": True,
        "kyc": identity.kyc_summary(g.user),
        "allowed_document_types": list(identity.allowed_doc_types()),
        # Only meaningful when an automated provider is configured. In manual
        # mode `required` is false and the existing flow is untouched.
        "consent": {
            "required": kyc_auto.provider_enabled(),
            "granted": kyc_auto.has_consent(user_id),
            "text_version": kyc_auto.consent_version(),
        },
        "provider": provider_name(),
        # Who decides. Every identity ticket is adjudicated by a RideMate
        # administrator in the KYC Review queue; an automated provider, when one
        # is configured, is only ever advisory. The frontend renders this so the
        # driver is never told a robot approved them.
        "review": {
            "reviewer": "RideMate Trust & Safety team",
            "manual": True,
            "automated_checks": bool(kyc_auto.provider_enabled()),
        },
    }


@bp.post("/consent")
@require_auth
@rate_limit("strict")
def record_consent():
    """Record explicit consent before any automated document lookup.

    Consent is append-only and versioned. A driver who has not accepted the
    current text cannot submit for verification while a provider is active.
    """
    data = request.get_json(silent=True) or request.form.to_dict()
    accepted = data.get("accepted")
    if not (accepted is True or str(accepted).strip().lower() in ("1", "true", "yes")):
        raise APIError("Consent must be explicitly accepted.", 422,
                       code="consent_required")

    row = kyc_auto.record_consent(g.user["_id"], ip=request.remote_addr)
    return {
        "ok": True,
        "consent": {
            "purpose": row["purpose"],
            "text_version": row["text_version"],
            "timestamp": row["timestamp"],
        },
    }


@bp.post("/identity")
@require_auth
@rate_limit("strict")
def submit_identity():
    """Upload an identity document and enter review.

    Accepts multipart (file) or JSON-with-base64 is deliberately NOT supported:
    a base64 field in a JSON body is trivially logged by proxies and is the most
    common way identity documents end up leaking into access logs.
    """
    _reject_banking_data()
    db = get_db()
    doc_type = (request.form.get("doc_type") or "").strip().lower()
    doc_number = (request.form.get("doc_number") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    dob = (request.form.get("date_of_birth") or "").strip()
    data, filename = _upload_bytes()

    identity.submit_identity(
        g.user["_id"], doc_type=doc_type, doc_number=doc_number, data=data,
        declared_filename=filename, full_name=full_name, date_of_birth=dob)

    from .. import notifications

    notifications.notify(
        g.user["_id"], "Identity submitted",
        "We are reviewing your identity document. You will hear from us once it "
        "has been checked.")
    fresh = db.users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "kyc": identity.kyc_summary(fresh)}, 201


@bp.delete("/identity")
@require_auth
@rate_limit("strict")
def withdraw_identity():
    """Withdraw a submitted document and the stored copy of it."""
    identity.clear_identity_document(g.user["_id"])
    fresh = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "kyc": identity.kyc_summary(fresh)}


@bp.get("/identity/document")
@require_auth
def download_identity_document():
    """Download one's own identity document.

    Ownership is re-derived from `g.user` and the AAD binding in
    `documents.load_private_document` means the bytes cannot be decrypted with
    another user's id, so this cannot be repurposed to read someone else's.
    """
    user = get_db().users.find_one({"_id": g.user["_id"]})
    doc = (user or {}).get("kyc_document") or {}
    key, doc_id = doc.get("key"), doc.get("doc_id")
    if not key:
        raise APIError("You have not uploaded an identity document.", 404,
                       code="not_found")
    data, mime = load_private_document(g.user["_id"], "identity", doc_id, key)
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": "inline",
                             "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})


# =================================================================== RC uploads
@bp.post("/vehicles/<vid>/rc")
@require_auth
@rate_limit("strict")
def submit_rc(vid):
    """Upload a vehicle's registration certificate into the review queue."""
    _reject_banking_data()
    vehicle_id = to_object_id(vid, "vehicle")
    data, filename = _upload_bytes()
    rc.submit_rc(vehicle_id, g.user["_id"], data=data,
                 declared_filename=filename,
                 rc_number=(request.form.get("rc_number") or "").strip())
    fresh = get_db().vehicles.find_one({"_id": vehicle_id})
    return {"ok": True, "rc": rc.rc_summary(fresh)}, 201


@bp.get("/vehicles/<vid>/rc")
@require_auth
def get_rc(vid):
    vehicle_id = to_object_id(vid, "vehicle")
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    if vehicle.get("user_id") != g.user["_id"] and not _is_admin():
        raise APIError("You do not have permission to view this document.", 403,
                       code="forbidden")
    return {"ok": True, "rc": rc.rc_summary(vehicle)}


@bp.get("/vehicles/<vid>/rc/document")
@require_auth
def download_rc(vid):
    """Owner or admin only. Same ownership rule as every other private read."""
    vehicle_id = to_object_id(vid, "vehicle")
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    if vehicle.get("user_id") != g.user["_id"] and not _is_admin():
        raise APIError("You do not have permission to view this document.", 403,
                       code="forbidden")
    doc = vehicle.get("rc_document") or {}
    if not doc.get("key"):
        raise APIError("This vehicle has no RC uploaded.", 404, code="not_found")
    data, mime = load_private_document(vehicle["user_id"], "rc",
                                       doc["doc_id"], doc["key"])
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": "inline",
                             "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})


# ======================================================== payout onboarding
@bp.get("/onboarding")
@require_auth
def my_onboarding():
    """The caller's payout onboarding status, for a driver."""
    if g.user.get("role") != "driver":
        raise APIError("Only drivers have a payout account.", 403, code="forbidden")
    return {"ok": True, "onboarding": onboarding.onboarding_summary(g.user)}


@bp.post("/onboarding/begin")
@require_auth
@rate_limit("strict")
def begin_onboarding():
    """Open the payout onboarding flow.

    Takes no banking details. The driver is redirected to the provider, and only
    the provider's opaque ids come back to us.
    """
    if g.user.get("role") != "driver":
        raise APIError("Only drivers have a payout account.", 403, code="forbidden")
    _reject_banking_data()
    last4 = request.form.get("account_last4") or request.form.get("last4")
    onboarding.begin_onboarding(g.user["_id"], account_last4=last4,
                                actor_id=g.user["_id"])
    fresh = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "onboarding": onboarding.onboarding_summary(fresh)}, 201


@bp.post("/onboarding/submit")
@require_auth
@rate_limit("strict")
def submit_onboarding():
    """Record that the provider accepted the driver's account details.

    `contact_id` / `linked_account_id` are the provider's own opaque
    identifiers. No account number, IFSC or UPI id is accepted or stored.
    """
    if g.user.get("role") != "driver":
        raise APIError("Only drivers have a payout account.", 403, code="forbidden")
    _reject_banking_data()
    form = request.form.to_dict()
    onboarding.submit_onboarding(
        g.user["_id"],
        contact_id=form.get("contact_id"),
        linked_account_id=form.get("linked_account_id"),
        provider_status=form.get("provider_status"),
        actor_id=g.user["_id"])
    fresh = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True, "onboarding": onboarding.onboarding_summary(fresh)}


@bp.get("/onboarding/status")
@require_auth
def onboarding_status():
    """A cheaper read of just the status, for polling after provider redirect."""
    user = get_db().users.find_one({"_id": g.user["_id"]})
    return {"ok": True,
            "status": onboarding.state_of(user),
            "settlement_allowed": onboarding.is_settled_ready(user),
            "reason": (user or {}).get("payout_onboarding_reason")}


# ================================================================= admin review
@bp.get("/admin/identity/queue")
@require_admin
def identity_queue():
    """Identity documents waiting on a human."""
    db = get_db()
    limit = min(int(request.args.get("limit", 50) or 50), 200)
    skip = max(int(request.args.get("skip", 0) or 0), 0)
    state = (request.args.get("status") or identity.KYC_SUBMITTED).strip()
    rows = db.users.find({"kyc_status": state}).sort("kyc_submitted_at", 1)
    return {"ok": True,
            "queue": [identity.admin_kyc_view(u) for u in rows.skip(skip).limit(limit)]}


@bp.get("/admin/rc/queue")
@require_admin
def rc_queue():
    """Registration certificates waiting on a human."""
    limit = min(int(request.args.get("limit", 50) or 50), 200)
    skip = max(int(request.args.get("skip", 0) or 0), 0)
    db = get_db()
    queue = rc.review_queue(limit=limit, skip=skip)
    for item in queue:
        vehicle = db.vehicles.find_one({"_id": to_object_id(item["vehicle_id"], "vehicle")},
                                       {"user_id": 1}) or {}
        owner = db.users.find_one({"_id": vehicle.get("user_id")},
                                  {"name": 1, "email": 1, "phone": 1}) or {}
        item["owner"] = {"name": owner.get("name", ""),
                         "email": owner.get("email", ""),
                         "phone": owner.get("phone", "")}
    return {"ok": True, "queue": queue}


@bp.get("/admin/identity/<uid>/document")
@require_admin
@rate_limit("default")
def admin_identity_document(uid):
    """Serve a submitted identity document to a reviewer.

    Reviewers must actually look at the paper, so this is the one route that
    decrypts somebody else's identity document. It is deliberately narrow: admin
    only, bound to the owner id in the AAD (so the key is useless for another
    account), never cached, and audited on every read -- the audit trail is what
    makes "who looked at whose Aadhaar" answerable.
    """
    from bson import ObjectId
    from .. import audit

    try:
        user_id = ObjectId(uid)
    except Exception:  # noqa: BLE001
        raise APIError("User not found.", 404, code="not_found")

    user = get_db().users.find_one({"_id": user_id})
    doc = (user or {}).get("kyc_document") or {}
    key, doc_id = doc.get("key"), doc.get("doc_id")
    if not key:
        raise APIError("No identity document on file.", 404, code="not_found")

    data, mime = load_private_document(user_id, "identity", doc_id, key)
    audit.record("identity.kyc.admin_view", domain=audit.IDENTITY,
                 actor_id=g.user["_id"], target_type="user", target_id=user_id)
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": "inline",
                             "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})


@bp.post("/admin/identity/<uid>/review")
@require_admin
@rate_limit("strict")
def review_identity(uid):
    """Approve or reject an identity. The only route to `verified`."""
    form = request.form.to_dict()
    decision = form.get("decision") or (request.get_json(silent=True) or {}).get("decision")
    reason = form.get("reason") or (request.get_json(silent=True) or {}).get("reason")
    code = (form.get("rejection_code")
            or (request.get_json(silent=True) or {}).get("rejection_code"))
    if not decision:
        raise APIError("decision is required.", 422, code="validation_error",
                       details={"fields": ["decision"]})

    from bson import ObjectId

    try:
        user_id = ObjectId(uid)
    except Exception:  # noqa: BLE001
        raise APIError("User not found.", 404, code="not_found")
    identity.review_identity(user_id, decision, reviewer_id=g.user["_id"],
                             reason=reason, rejection_code=code)
    fresh = get_db().users.find_one({"_id": user_id})

    # Tell the owner immediately: a driver waiting on a gate should not have to
    # refresh a page to discover the decision (the Captain page polls, and the
    # notification feed carries the same event to any open tab).
    from .. import notifications

    state = (fresh or {}).get("kyc_status")
    if state == identity.KYC_VERIFIED:
        notifications.notify(
            user_id, "Identity verified",
            "Your identity check is approved. Publish rides once a vehicle is verified.",
            ref_type="kyc_identity", ref_id=user_id,
            data={"event": "kyc.status", "scope": "identity", "status": state})
    elif state == identity.KYC_REJECTED:
        notifications.notify(
            user_id, "Identity needs another document",
            "We could not verify the document you sent. Upload a clearer copy to continue.",
            ref_type="kyc_identity", ref_id=user_id,
            data={"event": "kyc.status", "scope": "identity", "status": state})
    return {"ok": True, "kyc": identity.admin_kyc_view(fresh)}


@bp.post("/admin/vehicles/<vid>/rc/review")
@require_admin
@rate_limit("strict")
def review_rc(vid):
    """Approve or reject an RC. The only route to `approved`."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    decision = payload.get("decision")
    if not decision:
        raise APIError("decision is required.", 422, code="validation_error",
                       details={"fields": ["decision"]})
    vehicle_id = to_object_id(vid, "vehicle")
    rc.review_rc(vehicle_id, decision, reviewer_id=g.user["_id"],
                 reason=payload.get("reason"))
    fresh = get_db().vehicles.find_one({"_id": vehicle_id})
    return {"ok": True, "rc": rc.admin_rc_view(fresh)}


@bp.get("/admin/onboarding/<uid>")
@require_admin
def admin_onboarding(uid):
    """Full onboarding detail for one driver, for adjudication."""
    from bson import ObjectId

    try:
        user_id = ObjectId(uid)
    except Exception:  # noqa: BLE001
        raise APIError("User not found.", 404, code="not_found")
    user = get_db().users.find_one({"_id": user_id})
    if not user:
        raise APIError("User not found.", 404, code="not_found")
    return {"ok": True, "onboarding": onboarding.admin_onboarding_view(user)}


@bp.post("/admin/onboarding/<uid>/review")
@require_admin
@rate_limit("strict")
def review_onboarding(uid):
    """Verify, reject, suspend or resume a driver's payout account."""
    from bson import ObjectId

    payload = request.get_json(silent=True) or request.form.to_dict()
    action = (payload.get("action") or "").strip().lower()
    reason = payload.get("reason")
    provider_status = payload.get("provider_status")

    try:
        user_id = ObjectId(uid)
    except Exception:  # noqa: BLE001
        raise APIError("User not found.", 404, code="not_found")

    if action in ("verify", "approve", "verified"):
        onboarding.verify_onboarding(user_id, reviewer_id=g.user["_id"],
                                     provider_status=provider_status,
                                     actor_role="admin")
    elif action in ("reject", "rejected"):
        if not (reason or "").strip():
            raise APIError("A reason is required when rejecting.", 422,
                           code="onboarding_reason_required")
        onboarding.reject_onboarding(user_id, reviewer_id=g.user["_id"],
                                     reason=reason, actor_role="admin")
    elif action == "suspend":
        if not (reason or "").strip():
            raise APIError("A reason is required when suspending.", 422,
                           code="onboarding_reason_required")
        onboarding.suspend_onboarding(user_id, actor_id=g.user["_id"],
                                      reason=reason, actor_role="admin")
    elif action == "resume":
        onboarding.resume_onboarding(user_id, actor_id=g.user["_id"],
                                     actor_role="admin")
    else:
        raise APIError("action must be verify, reject, suspend or resume.", 422,
                       code="validation_error", details={"fields": ["action"]})

    fresh = get_db().users.find_one({"_id": user_id})
    return {"ok": True, "onboarding": onboarding.admin_onboarding_view(fresh)}


def _is_admin():
    return (g.user or {}).get("role") == "admin"


# ============================================== trip completion / dispute (API)
# Mounted under /api/bookings by completion_api because the natural resource is
# a booking. Declared here to keep the two small surfaces together in review.
@bp.get("/vehicles/<vid>/documents")
@require_auth
def vehicle_documents(vid):
    """Safe projection of every private document on a vehicle, for its owner.

    Convenience for the settings screen: one call, no keys, no bytes.
    """
    vehicle_id = to_object_id(vid, "vehicle")
    db = get_db()
    vehicle = db.vehicles.find_one({"_id": vehicle_id, "user_id": g.user["_id"]})
    if not vehicle:
        raise APIError("Vehicle not found.", 404, code="not_found")
    return {
        "ok": True,
        "documents": {
            "rc": rc.rc_summary(vehicle),
            "dl": public_metadata(vehicle.get("dl_document")),
            "insurance": public_metadata(vehicle.get("insurance_document")),
        },
    }


# =================================================================== provider webhook
def _webhook_signature():
    for header in ("X-KYC-Signature", "X-Signzy-Signature", "X-Signature"):
        value = request.headers.get(header)
        if value:
            return value.strip()
    return ""


def _webhook_timestamp():
    for header in ("X-KYC-Timestamp", "X-Webhook-Timestamp", "X-Signature-Timestamp"):
        value = request.headers.get(header)
        if value:
            return value.strip()
    return ""


def _signature_valid(raw_body, signature, secret, timestamp):
    """Accept the body-only signature and, when a timestamp is supplied, the
    timestamp-bound variant providers commonly use.

    TODO: confirm the exact signed string and header names against the selected
    provider's documentation before going live. Both forms are HMAC-SHA256 and
    compared in constant time by `verify_signature`.
    """
    if verify_signature(raw_body, signature, secret):
        return True
    if timestamp:
        bound = str(timestamp).encode("utf-8") + b"." + (raw_body or b"")
        return verify_signature(bound, signature, secret)
    return False


@bp.post("/webhook")
@rate_limit("strict")
def kyc_webhook():
    """Authenticated provider callback. No auth cookie; the signature is the auth.

    Idempotent on the provider's event id, replay-window limited when the
    provider sends a timestamp, and it can only ever move a vehicle forward to
    verified (with KYC_AUTO_APPROVE). A negative event is recorded for the
    reviewer and never auto-rejects.
    """
    raw_body = request.get_data(cache=True) or b""
    secret = current_app.config.get("KYC_PROVIDER_WEBHOOK_SECRET") or ""
    signature = _webhook_signature()
    timestamp = _webhook_timestamp()

    if not _signature_valid(raw_body, signature, secret, timestamp):
        audit.record("identity.kyc.webhook_rejected", domain=audit.IDENTITY,
                     actor_role="provider", target_type="webhook",
                     outcome="denied", reason="invalid_signature")
        raise APIError("Invalid signature.", 401, code="invalid_signature")

    if timestamp:
        max_age = int(current_app.config.get("KYC_WEBHOOK_MAX_AGE_SECONDS", 300) or 300)
        try:
            skew = abs(time.time() - float(timestamp))
        except (TypeError, ValueError):
            raise APIError("Invalid signature timestamp.", 401, code="invalid_signature")
        if skew > max(max_age, 0):
            raise APIError("Webhook is outside the replay window.", 401,
                           code="stale_webhook")

    if not kyc_auto.provider_enabled():
        # Nothing to apply: manual review is the configured mode.
        return {"ok": True, "ignored": "provider_manual"}

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise APIError("Webhook body must be JSON.", 422, code="validation_error")

    from ..kyc_provider import get_provider

    provider = get_provider()
    parsed = provider.parse_webhook(payload) if provider else None
    event = parsed or payload
    event_id = (str(event.get("event_id") or payload.get("id")
                    or payload.get("event_id") or "")).strip()
    if not event_id:
        raise APIError("Webhook event id is required.", 422, code="missing_event_id")

    # Dedupe on (provider, event_id) using the same unique index the payment
    # webhooks rely on. A duplicate is acknowledged, never reprocessed.
    provider_key = "kyc:" + provider_name()
    record = {
        "provider": provider_key,
        "event_id": event_id,
        "payload_hash": hashlib.sha256(raw_body).hexdigest()[:32],
        "first_received_at": utcnow(),
        "last_received_at": utcnow(),
    }
    try:
        get_db().webhook_events.insert_one(record)
    except DuplicateKeyError:
        get_db().webhook_events.update_one(
            {"provider": provider_key, "event_id": event_id},
            {"$set": {"last_received_at": utcnow()}})
        return {"ok": True, "duplicate": True}

    result = kyc_auto.apply_webhook(event)
    return result

