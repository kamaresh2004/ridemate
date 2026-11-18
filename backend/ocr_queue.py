"""Background OCR checks for vehicle licence uploads."""

import os
import secrets
import tempfile
from datetime import timedelta

from pymongo import ReturnDocument

from .db import get_db, utcnow
from .documents import load_private_document


def enqueue_licence_check(vehicle_id, user_id):
    db = get_db()
    vehicle = db.vehicles.find_one(
        {"_id": vehicle_id, "user_id": user_id},
        {"dl_document": 1})
    document = (vehicle or {}).get("dl_document") or {}
    if not document.get("key"):
        return None

    now = utcnow()
    check_id = secrets.token_hex(12)
    db.ocr_jobs.delete_many(
        {"vehicle_id": vehicle_id, "status": "queued"})
    if document.get("content_type") not in ("image/jpeg", "image/png"):
        db.vehicles.update_one(
            {"_id": vehicle_id, "user_id": user_id},
            {"$set": {"licence_ocr_check": {
                "check_id": check_id,
                "status": "unavailable",
                "result": "needs_review",
                "reasons": ["bad_file_type"],
                "checked_at": now,
            }}})
        return None

    db.vehicles.update_one(
        {"_id": vehicle_id, "user_id": user_id,
         "dl_document.doc_id": document.get("doc_id")},
        {"$set": {"licence_ocr_check": {
            "check_id": check_id,
            "status": "checking",
            "result": None,
            "reasons": [],
            "checked_at": None,
        }}})
    return db.ocr_jobs.insert_one({
        "vehicle_id": vehicle_id,
        "user_id": user_id,
        "document_id": document.get("doc_id"),
        "check_id": check_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
    }).inserted_id


def run_pass(limit=25, claim_ttl_seconds=300):
    """Run queued licence OCR checks and persist only the safe verdict."""
    db = get_db()
    now = utcnow()
    cutoff = now - timedelta(seconds=max(int(claim_ttl_seconds), 0))
    db.ocr_jobs.update_many(
        {"status": "running", "claimed_at": {"$lte": cutoff}},
        {"$set": {"status": "queued", "claimed_at": None, "updated_at": now}})

    checked = []
    for _ in range(max(int(limit), 0)):
        job = db.ocr_jobs.find_one_and_update(
            {"status": "queued"},
            {"$set": {"status": "running", "claimed_at": now, "updated_at": now}},
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER)
        if job is None:
            break
        _run_job(job, now)
        checked.append(str(job["vehicle_id"]))
    return checked


def _run_job(job, now):
    db = get_db()
    vehicle = db.vehicles.find_one(
        {"_id": job["vehicle_id"], "user_id": job["user_id"]})
    user = db.users.find_one({"_id": job["user_id"]})
    document = (vehicle or {}).get("dl_document") or {}
    check = (vehicle or {}).get("licence_ocr_check") or {}
    if (not vehicle or not user
            or document.get("doc_id") != job.get("document_id")
            or check.get("check_id") != job.get("check_id")):
        db.ocr_jobs.delete_one({"_id": job["_id"]})
        return

    path = None
    reasons = ["unreadable"]
    result = "needs_review"
    unavailable = False
    try:
        image, _ = load_private_document(
            job["user_id"], "dl", document["doc_id"], document["key"])
        suffix = ".png" if document.get("content_type") == "image/png" else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(image)
            path = tmp.name
        from .ocr_check import check_licence

        checked_result = check_licence(
            path, user.get("name") or "", vehicle.get("dl_number"))
        reasons = checked_result.get("reasons") or []
        result = checked_result.get("result") or "needs_review"
    except Exception:
        unavailable = True
    finally:
        if path and os.path.exists(path):
            os.remove(path)

    db.vehicles.update_one(
        {"_id": vehicle["_id"], "dl_document.doc_id": job["document_id"],
         "licence_ocr_check.check_id": job["check_id"]},
        {"$set": {"licence_ocr_check": {
            "status": "unavailable" if unavailable else
                "checked" if result == "checks_passed" else "needs_review",
            "result": result,
            "reasons": reasons,
            "checked_at": now,
        }}})
    db.ocr_jobs.delete_one({"_id": job["_id"]})
