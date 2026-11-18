"""Automated driver-KYC checks: provider outcomes, consent, webhook, safety.

The properties asserted here are the ones that make putting an automated check
in front of a human reviewer safe:

* no provider lookup without a recorded consent;
* a failing or inconclusive check never auto-rejects;
* a green check only auto-approves when that is explicitly configured;
* provider downtime is retried and then handed to a person, never treated as a
  negative result;
* the webhook is authenticated, idempotent and replay-limited;
* `KYC_PROVIDER=manual` leaves the original flow exactly as it was.
"""

import base64
import hashlib
import hmac
import io
import json
from datetime import date, timedelta

import pytest
from bson import ObjectId

from backend import kyc_auto
from backend.tests.conftest import (
    _make_user,
    auth,
    login,
    make_ride,
    satisfy_other_publish_gates,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def admin(client, db):
    _make_user(db, "Auto KYC Admin", "auto_kyc_admin@test.in", role="admin")
    return {"auth": auth(login(client, "auto_kyc_admin@test.in"))}


def _enable(monkeypatch, app, *, auto_approve=False, max_attempts=2):
    monkeypatch.setitem(app.config, "KYC_PROVIDER", "mock")
    monkeypatch.setitem(app.config, "KYC_AUTO_APPROVE", auto_approve)
    monkeypatch.setitem(app.config, "KYC_FACE_MATCH", False)
    monkeypatch.setitem(app.config, "KYC_MAX_ATTEMPTS", max_attempts)
    monkeypatch.setitem(app.config, "KYC_RETRY_BASE_SECONDS", 0)
    monkeypatch.setitem(app.config, "KYC_PROVIDER_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setitem(app.config, "KYC_WEBHOOK_MAX_AGE_SECONDS", 300)


def _upload(client, driver, vid, kind):
    resp = client.post(
        "/api/uploads/vehicle-doc", headers=driver["auth"],
        data={"vehicle_id": vid, "doc": kind,
              "file": (io.BytesIO(PNG), f"{kind}.png")},
        content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()


def _vehicle(client, db, driver, plate, dl_number):
    """A complete DL+insurance packet with the fields the mock provider reads."""
    resp = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler", "vehicle_number": plate,
        "vehicle_model": "Auto Check Car", "seat_count": 4,
        "dl_number": dl_number, "insurance_number": "POL-AUTO-1"})
    assert resp.status_code == 201, resp.get_json()
    vid = resp.get_json()["vehicle"]["id"]

    _upload(client, driver, vid, "dl")
    _upload(client, driver, vid, "insurance")

    dob = date(1993, 1, 5)
    db.users.update_one({"_id": driver["user"]["_id"]},
                        {"$set": {"kyc_date_of_birth": dob.isoformat()}})
    db.vehicles.update_one({"_id": ObjectId(vid)}, {"$set": {
        "dl_holder_name": "Driver One",
        "dl_date_of_birth": dob.isoformat(),
        "dl_expiry": (date.today() + timedelta(days=365)).isoformat(),
        "vehicle_class": "LMV",
        "insurance_expiry": (date.today() + timedelta(days=365)).isoformat(),
        "rc_document": {"rc_number": plate.replace(" ", "").upper(),
                        "owner_name": "Driver One"},
    }})
    return vid


def _consent(client, driver):
    resp = client.post("/api/kyc/consent", headers=driver["auth"],
                       json={"accepted": True})
    assert resp.status_code == 200, resp.get_json()


def _submit(client, driver, vid):
    resp = client.post(f"/api/vehicles/{vid}/submit-verification",
                       headers=driver["auth"], json={})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _run(app, limit=10):
    with app.app_context():
        return kyc_auto.run_pass(limit=limit)


def _vehicle_doc(db, vid):
    return db.vehicles.find_one({"_id": ObjectId(vid)})


# ------------------------------------------------------------------- consent
def test_consent_required_before_any_lookup(client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app)
    vid = _vehicle(client, db, driver, "KA21AA0001", "KA0520120012345")

    resp = client.post(f"/api/vehicles/{vid}/submit-verification",
                       headers=driver["auth"], json={})
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "kyc_consent_required"
    assert db.kyc_jobs.count_documents({}) == 0
    assert _vehicle_doc(db, vid)["verification_status"] == "unverified"


def test_consent_endpoint_records_a_versioned_row(client, db, driver):
    _consent(client, driver)
    row = db.kyc_consents.find_one({"user_id": driver["user"]["_id"]})
    assert row["purpose"] == "driver_kyc"
    assert row["text_version"]
    assert row["ip"] is not None

    me = client.get("/api/kyc/me", headers=driver["auth"]).get_json()
    assert me["consent"]["granted"] is True

    bad = client.post("/api/kyc/consent", headers=driver["auth"], json={})
    assert bad.status_code == 422


# -------------------------------------------------------------- mock outcomes
def test_pass_waits_for_manual_review_when_auto_approve_is_off(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=False)
    vid = _vehicle(client, db, driver, "KA21AA0002", "KA0520120012345")
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    stored = _vehicle_doc(db, vid)
    assert stored["auto_check"]["status"] == "pass"
    # A human still decides: pass is not a verification by itself here.
    assert stored["verification_status"] == "submitted"
    assert db.kyc_jobs.find_one({"vehicle_id": ObjectId(vid)})["status"] == "done"


def test_pass_auto_approves_only_when_enabled(client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0003", "KA0520120012345")
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    stored = _vehicle_doc(db, vid)
    assert stored["auto_check"]["status"] == "pass"
    assert stored["verification_status"] == "verified"
    assert stored["kyc_reviewed_by"] == "auto:mock"


def test_expired_licence_routes_to_manual_with_reason(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0004", "KA0520120000000")
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    stored = _vehicle_doc(db, vid)
    assert stored["auto_check"]["status"] == "fail"
    assert "Licence expired" in stored["auto_check"]["reasons"]
    # Never auto-rejected, even with auto-approve on.
    assert stored["verification_status"] == "submitted"


def test_name_mismatch_routes_to_manual_with_reason(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0005", "KA0520120011111")
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    auto = _vehicle_doc(db, vid)["auto_check"]
    assert auto["status"] == "fail"
    assert "Name does not match profile" in auto["reasons"]


def test_wrong_vehicle_class_routes_to_manual_with_reason(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0006", "KA0520120022222")
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    auto = _vehicle_doc(db, vid)["auto_check"]
    assert auto["status"] == "fail"
    assert "Licence class does not cover this vehicle" in auto["reasons"]


def test_inconclusive_routes_to_manual(client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0007", "KA0520120033333")
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    stored = _vehicle_doc(db, vid)
    assert stored["auto_check"]["status"] == "inconclusive"
    assert stored["verification_status"] == "submitted"


def test_provider_error_retries_then_falls_back_to_manual(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True, max_attempts=2)
    vid = _vehicle(client, db, driver, "KA21AA0008", "KA0520120099999")
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)
    job = db.kyc_jobs.find_one({"vehicle_id": ObjectId(vid)})
    assert job["status"] == "retry"
    assert job["attempts"] == 1
    assert _vehicle_doc(db, vid)["verification_status"] == "submitted"

    _run(app)
    job = db.kyc_jobs.find_one({"vehicle_id": ObjectId(vid)})
    assert job["status"] == "failed"
    assert job["attempts"] == 2

    stored = _vehicle_doc(db, vid)
    assert stored["auto_check"]["status"] == "unavailable"
    assert stored["verification_status"] == "submitted"


def test_insurance_must_cover_the_vehicle(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0016", "KA0520120012345")
    db.vehicles.update_one({"_id": ObjectId(vid)},
                           {"$set": {"insurance_vehicle_number": "KA99ZZ9999"}})
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    auto = _vehicle_doc(db, vid)["auto_check"]
    assert auto["status"] == "fail"
    assert "Insurance does not cover this vehicle" in auto["reasons"]


def test_expired_insurance_routes_to_manual(client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0017", "KA0520120012345")
    db.vehicles.update_one({"_id": ObjectId(vid)}, {"$set": {
        "insurance_expiry": (date.today() - timedelta(days=1)).isoformat()}})
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    auto = _vehicle_doc(db, vid)["auto_check"]
    assert auto["status"] == "fail"
    assert "Insurance expired" in auto["reasons"]
    assert _vehicle_doc(db, vid)["verification_status"] == "submitted"


def test_a_registration_that_does_not_match_the_plate_is_flagged(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0018", "KA0520120012345")
    db.vehicles.update_one({"_id": ObjectId(vid)}, {"$set": {
        "rc_document.rc_number": "KA99ZZ0000"}})
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    auto = _vehicle_doc(db, vid)["auto_check"]
    assert auto["status"] == "fail"
    assert "Registration certificate does not match this plate" in auto["reasons"]


def test_a_missing_registration_record_is_unknown_not_a_pass(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=True)
    vid = _vehicle(client, db, driver, "KA21AA0019", "KA0520120012345")
    db.vehicles.update_one({"_id": ObjectId(vid)}, {"$unset": {"rc_document": ""}})
    _consent(client, driver)
    _submit(client, driver, vid)

    _run(app)

    auto = _vehicle_doc(db, vid)["auto_check"]
    assert auto["status"] != "pass"
    assert "Registration certificate could not be read" in auto["reasons"]
    assert _vehicle_doc(db, vid)["verification_status"] == "submitted"


# --------------------------------------------------------------- manual mode
def test_manual_provider_leaves_the_flow_unchanged(
        client, db, app, driver, monkeypatch):
    monkeypatch.setitem(app.config, "KYC_PROVIDER", "manual")
    vid = _vehicle(client, db, driver, "KA21AA0009", "KA0520120012345")

    body = _submit(client, driver, vid)

    assert body["vehicle"]["verification_status"] == "submitted"
    assert db.kyc_jobs.count_documents({}) == 0
    assert not _vehicle_doc(db, vid).get("auto_check")


# ------------------------------------------------------------- reset/retrigger
def test_reupload_resets_and_retriggers_the_check(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app)
    vid = _vehicle(client, db, driver, "KA21AA0010", "KA0520120012345")
    _consent(client, driver)
    _submit(client, driver, vid)

    _upload(client, driver, vid, "dl")

    stored = _vehicle_doc(db, vid)
    assert stored["verification_status"] == "unverified"
    # The stale verdict is gone; a fresh job is already queued.
    assert stored["auto_check"]["status"] == "checking"
    job = db.kyc_jobs.find_one({"vehicle_id": ObjectId(vid)})
    assert job["trigger"] == "document_reupload"

    # Re-triggered means it actually runs: the fresh documents are checked, but
    # the vehicle still needs an explicit resubmission before it can be verified.
    _run(app)
    stored = _vehicle_doc(db, vid)
    assert stored["auto_check"]["status"] == "pass"
    assert stored["verification_status"] == "unverified"


def test_a_driver_cannot_self_verify_after_an_auto_check(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=False)
    vid = _vehicle(client, db, driver, "KA21AA0011", "KA0520120012345")
    _consent(client, driver)
    _submit(client, driver, vid)
    _run(app)

    resp = client.patch(f"/api/vehicles/{vid}", headers=driver["auth"],
                        json={"verification_status": "verified"})
    assert resp.status_code == 200
    assert _vehicle_doc(db, vid)["verification_status"] == "submitted"


def test_publish_gate_still_blocks_until_a_human_approves(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app, auto_approve=False)
    vid = _vehicle(client, db, driver, "KA21AA0012", "KA0520120012345")
    satisfy_other_publish_gates(db, driver["user"]["_id"], vid, vehicle_kyc=False)
    _consent(client, driver)
    _submit(client, driver, vid)
    _run(app)

    resp = make_ride(client, driver["auth"], {"id": vid})
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "kyc_pending_review"


# --------------------------------------------------------------- admin + audit
def test_admin_queue_shows_the_automated_result(
        client, db, app, driver, admin, monkeypatch):
    _enable(monkeypatch, app)
    vid = _vehicle(client, db, driver, "KA21AA0013", "KA0520120012345")
    _consent(client, driver)
    _submit(client, driver, vid)
    _run(app)

    queue = client.get("/api/admin/vehicles/pending",
                       headers=admin["auth"]).get_json()["data"]
    row = next(item for item in queue if item["id"] == vid)
    assert row["auto_check"]["status"] == "pass"


def test_every_automated_decision_is_audited(
        client, db, app, driver, monkeypatch):
    _enable(monkeypatch, app)
    vid = _vehicle(client, db, driver, "KA21AA0014", "KA0520120012345")
    _consent(client, driver)
    _submit(client, driver, vid)
    _run(app)

    rows = list(db.audit_logs.find({"action": "identity.kyc.auto_check"}))
    assert rows
    assert str(rows[0]["target_id"]) == vid
    assert rows[0]["meta"]["provider"] == "mock"


# ------------------------------------------------------------------- webhook
def _webhook_headers(payload, *, secret="whsec_test", timestamp=None):
    raw = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-KYC-Signature": signature}
    if timestamp is not None:
        headers["X-KYC-Timestamp"] = str(timestamp)
    return raw, headers


def _submitted_vehicle(client, db, driver, monkeypatch, app, vid=None):
    _enable(monkeypatch, app)
    vid = vid or _vehicle(client, db, driver, "KA21AA0015", "KA0520120012345")
    _consent(client, driver)
    _submit(client, driver, vid)
    return vid


def test_webhook_accepts_a_valid_signature(client, db, app, driver, monkeypatch):
    vid = _submitted_vehicle(client, db, driver, monkeypatch, app)
    payload = {"event_id": "evt_valid_1", "vehicle_id": vid, "status": "pass",
               "checks": {"dl_expiry": "pass"}, "reasons": [],
               "provider_ref": "req_1"}
    raw, headers = _webhook_headers(payload)

    resp = client.post("/api/kyc/webhook", data=raw, headers=headers)

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json().get("status") == "pass", resp.get_json()
    assert _vehicle_doc(db, vid)["auto_check"]["status"] == "pass"


def test_webhook_rejects_a_bad_signature(client, db, app, driver, monkeypatch):
    vid = _submitted_vehicle(client, db, driver, monkeypatch, app)
    payload = {"event_id": "evt_bad_1", "vehicle_id": vid, "status": "pass"}
    raw, headers = _webhook_headers(payload, secret="wrong-secret")

    resp = client.post("/api/kyc/webhook", data=raw, headers=headers)

    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "invalid_signature"
    assert db.kyc_jobs.count_documents({"status": "done"}) == 0


def test_webhook_is_idempotent_on_the_event_id(
        client, db, app, driver, monkeypatch):
    vid = _submitted_vehicle(client, db, driver, monkeypatch, app)
    payload = {"event_id": "evt_dup_1", "vehicle_id": vid, "status": "pass"}
    raw, headers = _webhook_headers(payload)

    first = client.post("/api/kyc/webhook", data=raw, headers=headers)
    second = client.post("/api/kyc/webhook", data=raw, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json().get("duplicate") is True
    assert db.webhook_events.count_documents(
        {"provider": "kyc:mock", "event_id": "evt_dup_1"}) == 1


def test_webhook_rejects_a_replayed_old_event(client, db, app, driver, monkeypatch):
    vid = _submitted_vehicle(client, db, driver, monkeypatch, app)
    payload = {"event_id": "evt_old_1", "vehicle_id": vid, "status": "pass"}
    import time as _time

    raw, headers = _webhook_headers(payload, timestamp=int(_time.time()) - 3600)

    resp = client.post("/api/kyc/webhook", data=raw, headers=headers)

    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "stale_webhook"
