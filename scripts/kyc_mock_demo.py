"""Manual end-to-end demo of the automated KYC check with the mock provider.

Run it against a local backend that is already serving on :5000:

    # 1. In backend/.env (or the shell) set:
    #      KYC_PROVIDER=mock
    #      KYC_AUTO_APPROVE=0
    #    then restart:  python -m backend.app
    # 2. python scripts/kyc_mock_demo.py --outcome pass
    #    python scripts/kyc_mock_demo.py --outcome expired
    #    python scripts/kyc_mock_demo.py --outcome name
    #    python scripts/kyc_mock_demo.py --outcome class
    #    python scripts/kyc_mock_demo.py --outcome inconclusive
    #    python scripts/kyc_mock_demo.py --outcome error
    #    (--base http://127.0.0.1:5055 targets a different backend port)
    #
    # The script drives the real API: login -> consent -> vehicle + documents ->
    # submit -> one worker KYC pass -> read the result. It deletes the vehicle it
    # created unless --keep is passed. Run it from the repo root with the project
    # virtualenv, so `backend` and the same backend/.env are used:
    #     .venv\\Scripts\\python.exe scripts\\kyc_mock_demo.py --outcome pass
"""

import argparse
import base64
import io
import os
import sys
import time
from datetime import date, timedelta

import requests

# The script both calls the HTTP API and reaches into the DB/app directly (to
# seed fields the public API deliberately does not accept). Make those imports
# work no matter which directory it is launched from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://localhost:5000"
EMAIL = "kamaresh15.4@gmail.com"
PASSWORD = "Kamaresh@15"

# DL numbers whose last four digits select a deterministic mock outcome.
OUTCOMES = {
    "pass": "2345",
    "expired": "0000",
    "name": "1111",
    "class": "2222",
    "inconclusive": "3333",
    "error": "9999",
}

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def login():
    resp = requests.post(f"{BASE}/api/auth/login", timeout=30,
                         json={"email": EMAIL, "password": PASSWORD})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def main():
    global BASE

    parser = argparse.ArgumentParser()
    parser.add_argument("--outcome", choices=sorted(OUTCOMES), default="pass")
    parser.add_argument("--base", default=BASE,
                        help="backend base URL (default http://localhost:5000)")
    parser.add_argument("--keep", action="store_true",
                        help="leave the demo vehicle in the database")
    args = parser.parse_args()
    BASE = args.base.rstrip("/")

    headers = login()
    suffix = OUTCOMES[args.outcome]
    dl_number = f"KA052026000{suffix}"
    plate = f"KA20MD{int(time.time()) % 10000:04d}"

    me = requests.get(f"{BASE}/api/kyc/me", headers=headers, timeout=30).json()
    print(f"provider={me['provider']} consent_required={me['consent']['required']}")
    if me["provider"] != "mock":
        print("Refusing to run: set KYC_PROVIDER=mock in backend/.env and restart.")
        return 2

    if not me["consent"]["granted"]:
        consent = requests.post(f"{BASE}/api/kyc/consent", headers=headers,
                                json={"accepted": True}, timeout=30)
        consent.raise_for_status()
        print(f"consent recorded: version {consent.json()['consent']['text_version']}")

    created = requests.post(f"{BASE}/api/vehicles", headers=headers, timeout=30, json={
        "vehicle_type": "4-wheeler", "vehicle_number": plate,
        "vehicle_model": "Mock KYC Demo", "seat_count": 4,
        "dl_number": dl_number, "insurance_number": "POL-DEMO-1"})
    created.raise_for_status()
    vid = created.json()["vehicle"]["id"]
    print(f"vehicle created: {plate} ({vid}), DL ends {suffix}")

    for kind in ("dl", "insurance"):
        up = requests.post(
            f"{BASE}/api/uploads/vehicle-doc", headers=headers, timeout=30,
            data={"vehicle_id": vid, "doc": kind},
            files={"file": (f"{kind}.png", io.BytesIO(PNG), "image/png")})
        up.raise_for_status()
        print(f"  uploaded {kind}")

    # The public API does not let a driver state licence expiry/class or the RC
    # owner; the mock provider reads those from the vehicle, so seed them here.
    # The holder name is taken from the account so the name check compares like
    # with like, whatever the display name happens to be.
    from bson import ObjectId

    from backend.app import create_app
    from backend.db import get_db

    app = create_app()
    with app.app_context():
        db = get_db()
        user = db.users.find_one({"email": EMAIL})
        holder = user.get("name") or "Demo Driver"
        dob = date(1993, 1, 5)
        db.users.update_one({"_id": user["_id"]},
                            {"$set": {"kyc_date_of_birth": dob.isoformat()}})
        db.vehicles.update_one({"_id": ObjectId(vid)}, {"$set": {
            "dl_holder_name": holder,
            "dl_date_of_birth": dob.isoformat(),
            "dl_expiry": (date.today() + timedelta(days=365)).isoformat(),
            "vehicle_class": "LMV",
            "insurance_expiry": (date.today() + timedelta(days=365)).isoformat(),
            "rc_document": {"rc_number": plate.replace(" ", "").upper(),
                            "owner_name": holder},
        }})

    submitted = requests.post(f"{BASE}/api/vehicles/{vid}/submit-verification",
                              headers=headers, timeout=30, json={})
    submitted.raise_for_status()
    print("submitted for verification (HTTP returned immediately)")

    # One worker pass, in-process, so the demo does not depend on a running
    # settlement worker. A real deployment runs this in `python -m backend.worker`.
    from backend import kyc_auto

    with app.app_context():
        for attempt in range(1, 4):
            kyc_auto.run_pass(limit=5)
            job = db.kyc_jobs.find_one({"vehicle_id": ObjectId(vid)})
            if job and job["status"] not in ("queued", "retry", "running"):
                break
            print(f"  job still {job and job['status']} (attempt {attempt}); retrying")

    vehicle = next(v for v in requests.get(f"{BASE}/api/vehicles", headers=headers,
                                           timeout=30).json()["data"] if v["id"] == vid)
    auto = vehicle["kyc"].get("auto_check") or {}
    print("\n--- result ---")
    print(f"vehicle status : {vehicle['verification_status']}")
    print(f"auto_check     : {auto.get('status')}")
    print(f"reasons        : {auto.get('reasons')}")
    print(f"checks         : {auto.get('checks')}")

    # A well-formed ride, so the reply is the publish gate rather than a field
    # validation error. The gate checks identity first, then the vehicle, then
    # the RC: whatever code comes back names the first thing still closed.
    gate = requests.post(f"{BASE}/api/rides", headers=headers, timeout=30, json={
        "vehicle_id": vid,
        "origin": {"label": "Demo origin", "lat": 12.97, "lng": 77.59},
        "destination": {"label": "Demo destination", "lat": 12.93, "lng": 77.62},
        "departure_date": (date.today() + timedelta(days=2)).isoformat(),
        "departure_time": "09:30",
        "seats_total": 2,
        "fare_per_seat": 250,
    })
    print(f"publish probe  : {gate.status_code} "
          f"{(gate.json().get('error') or {}).get('code')}")

    if not args.keep:
        requests.delete(f"{BASE}/api/vehicles/{vid}", headers=headers, timeout=30)
        print("demo vehicle deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
