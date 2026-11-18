"""Transient vehicle-document prechecks and queued licence OCR."""

import base64
import io
import os
import sys
import types

from bson import ObjectId

from backend.tests.conftest import _make_user, auth, login

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _fake_ocr(monkeypatch, fn):
    module = types.ModuleType("backend.ocr_check")
    module.check_licence = fn
    monkeypatch.setitem(sys.modules, "backend.ocr_check", module)


def test_licence_precheck_returns_reasons_and_removes_temporary_file(
        client, driver, db, monkeypatch):
    paths = []

    def check(path, profile_name, licence_no):
        paths.append(path)
        assert os.path.exists(path)
        assert profile_name == driver["user"]["name"]
        assert licence_no == "KA05 1993 0123456"
        return {"reasons": ["image_blurry"], "licence_no": "sensitive-number"}

    _fake_ocr(monkeypatch, check)
    before = db.ocr_jobs.count_documents({})
    response = client.post(
        "/api/kyc/precheck/licence",
        headers=driver["auth"],
        data={"licence_no": "KA05 1993 0123456",
              "file": (io.BytesIO(PNG), "licence.png")},
        content_type="multipart/form-data")

    assert response.status_code == 200
    assert response.get_json() == {"reasons": ["image_blurry"]}
    assert paths and not os.path.exists(paths[0])
    assert db.ocr_jobs.count_documents({}) == before


def test_precheck_rejects_non_image_bytes_and_accepts_rc_image(
        client, driver, monkeypatch):
    called = []
    _fake_ocr(monkeypatch, lambda *_args: called.append(True))
    invalid = client.post(
        "/api/kyc/precheck/licence", headers=driver["auth"],
        data={"file": (io.BytesIO(b"not an image"), "licence.png")},
        content_type="multipart/form-data")
    assert invalid.status_code == 400
    assert invalid.get_json()["reasons"] == ["bad_file_type"]
    assert called == []

    rc = client.post(
        "/api/kyc/precheck/image", headers=driver["auth"],
        data={"kind": "rc", "file": (io.BytesIO(PNG), "rc.png")},
        content_type="multipart/form-data")
    assert rc.status_code == 200
    assert rc.get_json() == {"reasons": []}


def test_precheck_rejects_photos_over_five_megabytes(client, driver):
    response = client.post(
        "/api/kyc/precheck/licence", headers=driver["auth"],
        data={"file": (io.BytesIO(b"x" * (5 * 1024 * 1024 + 1)), "licence.jpg")},
        content_type="multipart/form-data")
    assert response.status_code == 413
    assert response.get_json()["reasons"] == ["file_over_5mb"]


def test_precheck_is_limited_to_ten_requests_per_minute(client, driver, monkeypatch):
    monkeypatch.setitem(client.application.config, "RATE_LIMIT_PRECHECK", 10)
    _fake_ocr(monkeypatch, lambda *_args: {"reasons": []})
    for _ in range(10):
        response = client.post(
            "/api/kyc/precheck/licence", headers=driver["auth"],
            data={"file": (io.BytesIO(PNG), "licence.png")},
            content_type="multipart/form-data")
        assert response.status_code == 200
    limited = client.post(
        "/api/kyc/precheck/licence", headers=driver["auth"],
        data={"file": (io.BytesIO(PNG), "licence.png")},
        content_type="multipart/form-data")
    assert limited.status_code == 429


def test_vehicle_accepts_supported_registration_and_spaced_licence_formats(
        client, driver):
    response = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler", "vehicle_number": "22 BH 1234 AB",
        "dl_number": "KA05 1993 0123456"})
    assert response.status_code == 201, response.get_json()
    vehicle = response.get_json()["vehicle"]
    assert vehicle["vehicle_number"] == "22BH1234AB"
    assert vehicle["dl_number"] == "KA05 1993 0123456"


def test_ocr_extracts_the_formatted_licence_example():
    from backend.ocr_check import extract_licence_no

    assert extract_licence_no("DL No: KA05 1993 0123456") == "KA0519930123456"


def test_licence_ocr_worker_saves_safe_result_for_admin(
        client, db, driver, monkeypatch):
    created = client.post("/api/vehicles", headers=driver["auth"], json={
        "vehicle_type": "4-wheeler", "vehicle_number": "KA01AB1234",
        "dl_number": "KA0520120012345", "insurance_number": "POLICY-44"})
    assert created.status_code == 201
    vehicle_id = created.get_json()["vehicle"]["id"]

    for kind in ("dl", "insurance"):
        uploaded = client.post(
            "/api/uploads/vehicle-doc", headers=driver["auth"],
            data={"vehicle_id": vehicle_id, "doc": kind,
                  "file": (io.BytesIO(PNG), f"{kind}.png")},
            content_type="multipart/form-data")
        assert uploaded.status_code == 200

    changed = client.patch(
        f"/api/vehicles/{vehicle_id}", headers=driver["auth"],
        json={"dl_number": "KA0520120099995"})
    assert changed.status_code == 200
    checked_numbers = []

    def check(_path, _name, number):
        checked_numbers.append(number)
        return {"reasons": ["expired"], "result": "needs_review",
                "licence_no": "must-not-be-persisted"}

    _fake_ocr(monkeypatch, check)
    from backend import ocr_queue

    assert ocr_queue.run_pass() == [vehicle_id]
    assert checked_numbers == ["KA0520120099995"]
    saved = db.vehicles.find_one({"_id": ObjectId(vehicle_id)})["licence_ocr_check"]
    assert saved["status"] == "needs_review"
    assert saved["reasons"] == ["expired"]
    assert "licence_no" not in saved

    submitted = client.post(
        f"/api/vehicles/{vehicle_id}/submit-verification",
        headers=driver["auth"], json={})
    assert submitted.status_code == 200

    admin_user = _make_user(db, "OCR Admin", "ocr_admin@test.in", role="admin")
    admin_auth = auth(login(client, admin_user["email"]))
    queue = client.get("/api/admin/vehicles/pending", headers=admin_auth)
    item = next(row for row in queue.get_json()["data"] if row["id"] == vehicle_id)
    assert item["licence_ocr_check"]["reasons"] == ["expired"]
