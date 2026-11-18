"""Instant format checks for vehicle fields, plus duplicate and age rules.

These are the rules the browser only *hints* at; the server is the source of
truth, so every case below goes through the API rather than importing a regex.
"""

import base64
import io

from backend.tests.conftest import _make_driver, auth, login

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

PLATE = {"vehicle_type": "4-wheeler", "vehicle_model": "Format Car", "seat_count": 4}


def _post_vehicle(client, hdrs, **fields):
    payload = dict(PLATE)
    payload.update(fields)
    return client.post("/api/vehicles", headers=hdrs, json=payload)


def _identity(client, hdrs, dob=None, number="123456789012"):
    data = {"doc_type": "aadhaar", "doc_number": number, "full_name": "Asha Rao",
            "file": (io.BytesIO(PNG), "aadhaar.png")}
    if dob is not None:
        data["date_of_birth"] = dob
    return client.post("/api/kyc/identity", headers=hdrs, data=data,
                       content_type="multipart/form-data")


# --------------------------------------------------------------- registration
def test_a_registration_number_is_normalised_before_storage(client, driver):
    resp = _post_vehicle(client, driver["auth"], vehicle_number="ka-05 mj 4821")
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["vehicle"]["vehicle_number"] == "KA05MJ4821"


def test_a_bh_series_registration_number_is_accepted(client, driver):
    resp = _post_vehicle(client, driver["auth"], vehicle_number="22 BH 1234 AB")
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["vehicle"]["vehicle_number"] == "22BH1234AB"


def test_an_impossible_registration_number_is_refused(client, driver):
    for bad in ("KA05MJ48", "KAA5MJ4821", "05KA4821MJ", "KA05MJ48211", "1234"):
        resp = _post_vehicle(client, driver["auth"], vehicle_number=bad)
        assert resp.status_code == 422, bad
        assert resp.get_json()["error"]["details"]["fields"] == ["vehicle_number"]


# --------------------------------------------------------------------- licence
def test_a_licence_number_keeps_its_spacing_but_is_validated_compacted(
        client, driver):
    resp = _post_vehicle(client, driver["auth"], vehicle_number="KA05MJ4821",
                         dl_number="ka05-1993-0123456")
    assert resp.status_code == 201, resp.get_json()
    # The typed form is preserved for the reviewer; only the check is compacted.
    assert resp.get_json()["vehicle"]["dl_number"] == "ka05-1993-0123456"


def test_a_malformed_licence_number_is_refused(client, driver):
    for bad in ("KA05199301234", "K19930123456", "KA05 1993 012345678", "ABCDEFGHIJKLMNO"):
        resp = _post_vehicle(client, driver["auth"], vehicle_number="KA05MJ4821",
                             dl_number=bad)
        assert resp.status_code == 422, bad
        assert resp.get_json()["error"]["details"]["fields"] == ["dl_number"]


# ------------------------------------------------------------------- insurance
def test_a_policy_number_with_an_allowed_shape_is_accepted(client, driver):
    resp = _post_vehicle(client, driver["auth"], vehicle_number="KA05MJ4821",
                         insurance_number="POL-2026/7788")
    assert resp.status_code == 201, resp.get_json()


def test_a_policy_number_outside_the_allowed_shape_is_refused(client, driver):
    for bad in ("POL", "POL 2026 7788", "POLICY#2026", "P" * 41, "POLICY_2026"):
        resp = _post_vehicle(client, driver["auth"], vehicle_number="KA05MJ4821",
                             insurance_number=bad)
        assert resp.status_code == 422, bad
        assert resp.get_json()["error"]["details"]["fields"] == ["insurance_number"]


# ------------------------------------------------------------------ duplicates
def test_a_plate_cannot_be_active_on_two_accounts(client, db):
    owner = _make_driver(db, client, "Plate Owner", "plate-owner@test.in")
    owner_hdrs = auth(login(client, "plate-owner@test.in"))
    assert _post_vehicle(client, owner_hdrs, vehicle_number="KA05MJ4821").status_code == 201

    intruder = _make_driver(db, client, "Plate Intruder", "plate-intruder@test.in")
    intruder_hdrs = auth(login(client, "plate-intruder@test.in"))
    resp = _post_vehicle(client, intruder_hdrs, vehicle_number="KA 05 MJ 4821")

    assert resp.status_code == 409
    body = resp.get_json()["error"]
    assert body["code"] == "duplicate_vehicle"
    # Says what to do, and never leaks whose account holds the plate.
    assert "plate-owner@test.in" not in str(resp.get_json())
    del owner, intruder


def test_a_licence_cannot_be_active_on_two_accounts(client, db):
    _make_driver(db, client, "Licence Owner", "licence-owner@test.in")
    owner_hdrs = auth(login(client, "licence-owner@test.in"))
    assert _post_vehicle(client, owner_hdrs, vehicle_number="KA05MJ4821",
                         dl_number="KA05 1993 0123456").status_code == 201

    _make_driver(db, client, "Licence Intruder", "licence-intruder@test.in")
    intruder_hdrs = auth(login(client, "licence-intruder@test.in"))
    # Compacted differently on purpose: the rule is about the licence, not text.
    resp = _post_vehicle(client, intruder_hdrs, vehicle_number="KA06MJ4822",
                         dl_number="ka051993-0123456")

    assert resp.status_code == 409
    body = resp.get_json()["error"]
    assert body["code"] == "duplicate_licence"
    assert body["details"]["fields"] == ["dl_number"]
    assert "licence-owner@test.in" not in str(resp.get_json())


def test_one_account_can_use_one_licence_for_several_vehicles(client, driver):
    assert _post_vehicle(client, driver["auth"], vehicle_number="KA05MJ4821",
                         dl_number="KA0519930123456").status_code == 201
    assert _post_vehicle(client, driver["auth"], vehicle_number="KA06MJ4822",
                         dl_number="ka0519930123456").status_code == 201


def test_editing_a_licence_to_another_accounts_number_is_refused(client, db):
    _make_driver(db, client, "Edit Owner", "edit-owner@test.in")
    owner_hdrs = auth(login(client, "edit-owner@test.in"))
    assert _post_vehicle(client, owner_hdrs, vehicle_number="KA05MJ4821",
                         dl_number="KA0519930123456").status_code == 201

    _make_driver(db, client, "Edit Other", "edit-other@test.in")
    other_hdrs = auth(login(client, "edit-other@test.in"))
    created = _post_vehicle(client, other_hdrs, vehicle_number="KA07MJ4823",
                            dl_number="KA0520120012345")
    vid = created.get_json()["vehicle"]["id"]

    resp = client.patch(f"/api/vehicles/{vid}", headers=other_hdrs,
                        json={"dl_number": "KA05 1993 0123456"})
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "duplicate_licence"


# ------------------------------------------------------------------ date of birth
def test_a_driver_must_give_a_date_of_birth(client, driver):
    resp = _identity(client, driver["auth"])
    assert resp.status_code == 422
    assert resp.get_json()["error"]["details"]["fields"] == ["date_of_birth"]


def test_a_driver_under_eighteen_is_refused(client, driver):
    resp = _identity(client, driver["auth"], dob="2012-01-01")
    assert resp.status_code == 422
    body = resp.get_json()["error"]
    assert body["details"]["fields"] == ["date_of_birth"]
    assert "18" in body["message"]


def test_a_future_or_malformed_date_of_birth_is_refused(client, driver):
    for bad in ("1990-13-01", "12/04/1990", "2999-01-01"):
        resp = _identity(client, driver["auth"], dob=bad)
        assert resp.status_code == 422, bad
        assert resp.get_json()["error"]["details"]["fields"] == ["date_of_birth"]


def test_an_adult_date_of_birth_is_stored_canonically(client, db, driver):
    resp = _identity(client, driver["auth"], dob="1990-04-12")
    assert resp.status_code == 201, resp.get_json()
    stored = db.users.find_one({"_id": driver["user"]["_id"]})
    assert stored["kyc_date_of_birth"] == "1990-04-12"


def test_a_passenger_identity_does_not_require_a_date_of_birth(client, rider):
    """The 18+ rule is about a licence, so a passenger is not asked for it."""
    resp = _identity(client, rider["auth"], number="123456789012")
    assert resp.status_code == 201, resp.get_json()
