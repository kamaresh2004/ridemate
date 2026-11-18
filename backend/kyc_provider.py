"""Automated driver-KYC provider adapters.

The manual review flow in `backend/kyc.py` stays the source of truth: a human
still approves a vehicle unless `KYC_AUTO_APPROVE` is explicitly turned on, and
every automatic result is recorded *alongside* the human decision rather than
replacing it. This module only answers one question -- "what do the documents
say?" -- in a normalised shape the rest of the service understands.

Design rules, all deliberate:

* **One normalised result.** `{status, checks, reasons, provider_ref, raw_ref}`.
  The worker, the admin queue and the driver UI read the same fields regardless
  of which provider produced them.
* **No raw payloads, no document numbers.** `raw_ref` is an opaque provider id
  or a hash -- never the provider's response body. `reasons` are short, safe,
  fix-it strings: a reviewer sees why a check failed, not the number it failed on.
* **Unknown is not a failure.** A check we could not run ("expiry not provided")
  yields `inconclusive`, which routes to a human. Only an explicit negative is
  `fail`, and even then nothing is auto-rejected.
* **No invented endpoints.** The `signzy` adapter reads its paths from the
  environment. Every endpoint/field that has not been confirmed against the
  provider's documentation is marked TODO rather than guessed.
"""

import abc
import hashlib
import hmac
import logging
import re
import unicodedata
from datetime import date, datetime

from flask import current_app

log = logging.getLogger(__name__)

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"
STATUSES = (PASS, FAIL, INCONCLUSIVE)

# Check names are a stable vocabulary: the admin UI renders them, tests assert
# them, and a provider adapter must map onto them rather than inventing its own.
CHECK_DL_EXISTS = "dl_exists"
CHECK_DL_EXPIRY = "dl_expiry"
CHECK_NAME = "name_match"
CHECK_DOB = "dob_match"
CHECK_CLASS = "vehicle_class"
CHECK_RC_NUMBER = "rc_number"
CHECK_RC_OWNER = "rc_owner"
CHECK_INSURANCE = "insurance_valid"
CHECK_FACE = "face_match"

# Safe, human-readable reasons. These are shown to drivers, so they say what to
# fix and never echo a document number, a provider payload or an internal code.
REASON_DL_MISSING = "Licence details are missing"
REASON_DL_EXPIRED = "Licence expired"
REASON_DL_UNREADABLE = "Licence expiry could not be read"
REASON_NAME_MISMATCH = "Name does not match profile"
REASON_NAME_UNKNOWN = "Name on the licence could not be compared"
REASON_DOB_MISMATCH = "Date of birth does not match profile"
REASON_DOB_UNKNOWN = "Date of birth on the licence could not be compared"
REASON_WRONG_CLASS = "Licence class does not cover this vehicle"
REASON_CLASS_UNKNOWN = "Licence class could not be confirmed"
REASON_RC_MISMATCH = "Registration certificate does not match this plate"
REASON_RC_OWNER = "Registration certificate names a different owner"
REASON_RC_UNKNOWN = "Registration certificate could not be read"
REASON_INSURANCE_INVALID = "Insurance policy is not valid"
REASON_INSURANCE_EXPIRED = "Insurance expired"
REASON_INSURANCE_UNKNOWN = "Insurance could not be confirmed"
REASON_INSURANCE_VEHICLE = "Insurance does not cover this vehicle"
REASON_FACE_UNKNOWN = "Selfie check was not completed"
REASON_CONSENT_MISSING = "Consent for document checks is not on record"
REASON_PROVIDER_UNAVAILABLE = "Document check service was unavailable"

# Licence class implied by the vehicle type. Indian DL classes: LMV covers cars
# and light autos, MCWG motorcycles/scooters, HMV heavy vehicles such as buses.
VEHICLE_CLASS_REQUIRED = {
    "2-wheeler": "MCWG",
    "4-wheeler": "LMV",
    "auto": "LMV",
    "bus": "HMV",
}


class ProviderError(RuntimeError):
    """The provider could not answer (timeout, 5xx, malformed response).

    Always retryable: an unavailable provider must never become a rejection.
    """


class ProviderNotConfigured(ProviderError):
    """Provider selected but its credentials/endpoints are missing."""


def make_result(status, checks=None, reasons=None, provider_ref="", raw_ref=""):
    """Build the normalised result. Raises on an unknown status."""
    if status not in STATUSES:
        raise ValueError("unknown KYC status %r" % (status,))
    return {
        "status": status,
        "checks": dict(checks or {}),
        "reasons": list(reasons or []),
        "provider_ref": str(provider_ref or ""),
        "raw_ref": str(raw_ref or ""),
    }


def _norm_name(value):
    """Casefold, strip punctuation and collapse whitespace.

    Initials are handled by `name_similarity` rather than here: "K E" and
    "Kamaresh E" share too little text to score well on tokens alone, so the
    comparison expands single letters before scoring.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return " ".join(text.split())


def name_similarity(profile_name, document_name):
    """Tolerant name comparison for Indian documents.

    Handles, in one pass: case, accents, extra spaces, punctuation, token order,
    initials expanded from either side ("K E" vs "Kamaresh E"), and one name
    being a subset of the other (documents routinely omit middle names).

    Returns {"score": 0..1, "matched": bool}. The score is always recorded, even
    when the names match, so a reviewer can see a 0.78 vs a 1.0.
    """
    left = _norm_name(profile_name)
    right = _norm_name(document_name)
    if not left or not right:
        return {"score": 0.0, "matched": False}

    a_tokens, b_tokens = left.split(), right.split()

    def expand(tokens, other):
        out = []
        for tok in tokens:
            if len(tok) == 1:
                # A single letter matches any token in the other name that
                # starts with it: "K" -> "kamaresh".
                hits = [o for o in other if o.startswith(tok)]
                out.extend(hits or [tok])
            else:
                out.append(tok)
        return out

    a_tokens = expand(a_tokens, b_tokens)
    b_tokens = expand(b_tokens, a_tokens)
    a_set, b_set = set(a_tokens), set(b_tokens)

    shared = a_set & b_set
    if not shared:
        return {"score": 0.0, "matched": False}

    # Token overlap on the smaller name: "Kamaresh" is contained in "Kamaresh E".
    score = len(shared) / min(len(a_set), len(b_set))
    if a_set.issubset(b_set) or b_set.issubset(a_set):
        score = max(score, 0.9)
    return {"score": round(min(score, 1.0), 2), "matched": score >= 0.75}


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


class KycProvider(abc.ABC):
    """The interface every adapter implements."""

    name = "abstract"

    @abc.abstractmethod
    def verify_driver(self, user, vehicle, documents):
        """Return a normalised result for one vehicle submission.

        `documents` carries only metadata ({kind: {"doc_id", "key",
        "content_type"}}); adapters that need bytes ask `documents_loader`, which
        the caller passes in so this module never touches storage itself.
        """
        raise NotImplementedError

    def parse_webhook(self, payload):
        """Map a provider webhook body onto the normalised shape.

        Returns None when the event is not one this service acts on. Providers
        that do not call back can leave this alone.
        """
        return None


class MockProvider(KycProvider):
    """Deterministic provider for development, tests and demos.

    Outcomes are driven by the licence number's last four digits so a test (or a
    person poking at the UI) can reproduce every branch without a network call:

        9999 -> provider_error (retried, then manual)
        0000 -> expired licence
        1111 -> name mismatch
        2222 -> wrong vehicle class
        3333 -> inconclusive (something could not be read)
        else -> pass

    Everything else is derived from the vehicle/user fields, so the mock still
    exercises the real check-plumbing rather than returning a canned verdict.
    """

    name = "mock"

    def verify_driver(self, user, vehicle, documents):
        dl_number = str((vehicle or {}).get("dl_number") or "").replace(" ", "").upper()
        suffix = dl_number[-4:]
        if suffix == "9999":
            raise ProviderError("mock provider error (9999)")

        checks, reasons = {}, []

        checks[CHECK_DL_EXISTS] = "pass" if dl_number else "fail"
        if not dl_number:
            reasons.append(REASON_DL_MISSING)

        if suffix == "0000":
            checks[CHECK_DL_EXPIRY] = "fail"
            reasons.append(REASON_DL_EXPIRED)
        else:
            expiry = _as_date((vehicle or {}).get("dl_expiry"))
            if expiry is None:
                checks[CHECK_DL_EXPIRY] = "unknown"
                reasons.append(REASON_DL_UNREADABLE)
            elif expiry < date.today():
                checks[CHECK_DL_EXPIRY] = "fail"
                reasons.append(REASON_DL_EXPIRED)
            else:
                checks[CHECK_DL_EXPIRY] = "pass"

        holder = ((vehicle or {}).get("dl_holder_name")
                  or (user or {}).get("kyc_legal_name"))
        if suffix == "1111":
            checks[CHECK_NAME] = "fail"
            checks["name_score"] = 0.42
            reasons.append(REASON_NAME_MISMATCH)
        elif not holder:
            checks[CHECK_NAME] = "unknown"
            reasons.append(REASON_NAME_UNKNOWN)
        else:
            match = name_similarity((user or {}).get("name"), holder)
            checks[CHECK_NAME] = "pass" if match["matched"] else "fail"
            checks["name_score"] = match["score"]
            if not match["matched"]:
                reasons.append(REASON_NAME_MISMATCH)

        doc_dob = _as_date((vehicle or {}).get("dl_date_of_birth"))
        user_dob = _as_date((user or {}).get("kyc_date_of_birth"))
        if not doc_dob or not user_dob:
            checks[CHECK_DOB] = "unknown"
            reasons.append(REASON_DOB_UNKNOWN)
        elif doc_dob == user_dob:
            checks[CHECK_DOB] = "pass"
        else:
            checks[CHECK_DOB] = "fail"
            reasons.append(REASON_DOB_MISMATCH)

        required = VEHICLE_CLASS_REQUIRED.get((vehicle or {}).get("vehicle_type"))
        declared = str((vehicle or {}).get("vehicle_class") or "").upper()
        if suffix == "2222":
            checks[CHECK_CLASS] = "fail"
            reasons.append(REASON_WRONG_CLASS)
        elif not declared or not required:
            checks[CHECK_CLASS] = "unknown"
            reasons.append(REASON_CLASS_UNKNOWN)
        else:
            checks[CHECK_CLASS] = "pass" if required in declared else "fail"
            if required not in declared:
                reasons.append(REASON_WRONG_CLASS)

        rc = (vehicle or {}).get("rc_document") or {}
        rc_number = str((rc.get("rc_number") or "")).replace(" ", "").upper()
        plate = str((vehicle or {}).get("vehicle_number") or "").replace(" ", "").upper()
        if not rc_number:
            checks[CHECK_RC_NUMBER] = "unknown"
            reasons.append(REASON_RC_UNKNOWN)
        elif rc_number == plate:
            checks[CHECK_RC_NUMBER] = "pass"
            checks[CHECK_RC_OWNER] = "pass" if not rc.get("owner_name") else (
                "pass" if name_similarity((user or {}).get("name"), rc["owner_name"])["matched"]
                else "flag")
            if checks[CHECK_RC_OWNER] == "flag":
                reasons.append(REASON_RC_OWNER)
        else:
            checks[CHECK_RC_NUMBER] = "fail"
            reasons.append(REASON_RC_MISMATCH)

        insurance_number = str((vehicle or {}).get("insurance_number") or "").strip()
        insurance_expiry = _as_date((vehicle or {}).get("insurance_expiry"))
        policy_plate = str((vehicle or {}).get("insurance_vehicle_number") or "") \
            .replace(" ", "").upper()
        if not insurance_number:
            checks[CHECK_INSURANCE] = "unknown"
            reasons.append(REASON_INSURANCE_UNKNOWN)
        elif insurance_expiry is None:
            checks[CHECK_INSURANCE] = "unknown"
            reasons.append(REASON_INSURANCE_UNKNOWN)
        elif insurance_expiry < date.today():
            checks[CHECK_INSURANCE] = "fail"
            reasons.append(REASON_INSURANCE_EXPIRED)
        elif policy_plate and plate and policy_plate != plate:
            # A policy that names a different registration does not cover this
            # vehicle, even if the policy itself is in date.
            checks[CHECK_INSURANCE] = "fail"
            reasons.append(REASON_INSURANCE_VEHICLE)
        else:
            checks[CHECK_INSURANCE] = "pass"

        if current_app.config.get("KYC_FACE_MATCH"):
            # The mock has no selfie pipeline; a face check that was not actually
            # performed must not be reported as a pass.
            checks[CHECK_FACE] = "unknown"
            reasons.append(REASON_FACE_UNKNOWN)

        if suffix == "3333":
            checks[CHECK_DL_EXPIRY] = "unknown"
            reasons.append(REASON_DL_UNREADABLE)

        if any(value == "fail" for value in checks.values()):
            status = FAIL
        elif any(value in ("unknown", "flag") for value in checks.values()):
            status = INCONCLUSIVE
        else:
            status = PASS

        return make_result(status, checks, reasons,
                           provider_ref="mock_" + hashlib.sha256(
                               dl_number.encode("utf-8")).hexdigest()[:12],
                           raw_ref="")


class SignzyProvider(KycProvider):
    """HTTP adapter for Signzy's KYC APIs.

    Only the transport, auth and normalisation are implemented here. The exact
    endpoint paths and response field names are provider-contract details that
    this codebase has not verified, so they come from the environment and every
    mapping that depends on them is marked TODO -- guessing them would produce a
    silently wrong "pass", which is the one outcome this module must never fake.

    Configure with:
        KYC_PROVIDER=signzy
        KYC_PROVIDER_BASE_URL=https://...
        KYC_PROVIDER_API_KEY=...
        KYC_PROVIDER_DL_PATH=/...        # TODO: confirm path
        KYC_PROVIDER_RC_PATH=/...        # TODO: confirm path
        KYC_PROVIDER_INSURANCE_PATH=/... # TODO: confirm path
        KYC_PROVIDER_WEBHOOK_SECRET=...
    """

    name = "signzy"

    def __init__(self):
        cfg = current_app.config
        self.base_url = str(cfg.get("KYC_PROVIDER_BASE_URL") or "").rstrip("/")
        self.api_key = cfg.get("KYC_PROVIDER_API_KEY") or ""
        self.api_secret = cfg.get("KYC_PROVIDER_API_SECRET") or ""
        self.timeout = int(cfg.get("KYC_PROVIDER_TIMEOUT_SECONDS", 8) or 8)
        self.paths = {
            "dl": cfg.get("KYC_PROVIDER_DL_PATH") or "",
            "rc": cfg.get("KYC_PROVIDER_RC_PATH") or "",
            "insurance": cfg.get("KYC_PROVIDER_INSURANCE_PATH") or "",
        }
        if not self.base_url or not self.api_key:
            raise ProviderNotConfigured("KYC_PROVIDER=signzy requires base URL and API key")

    def _post(self, path, payload):
        """POST one check, with a timeout and no request/response logging."""
        import requests

        if not path:
            # TODO: set KYC_PROVIDER_*_PATH once the provider's paths are confirmed.
            raise ProviderNotConfigured(
                "provider endpoint path is not configured (see KYC_PROVIDER_*_PATH)")
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = requests.post(self.base_url + path, json=payload,
                                     headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            # Never log the payload: it carries identity data.
            raise ProviderError(f"provider request failed: {type(exc).__name__}") from None
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderError(f"provider unavailable (HTTP {response.status_code})")
        if response.status_code >= 400:
            # A 4xx is the provider refusing the input, not an outage: treat it
            # as inconclusive so a human still looks at the packet.
            log.info("KYC provider rejected a check input (HTTP %s)", response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            raise ProviderError("provider returned a non-JSON body") from None

    def verify_driver(self, user, vehicle, documents):
        dl_number = str((vehicle or {}).get("dl_number") or "")
        if not dl_number:
            return make_result(FAIL, {CHECK_DL_EXISTS: "fail"},
                               [REASON_DL_MISSING], provider_ref="")

        # TODO: confirm the request body field names against Signzy's API docs
        # before enabling this provider. Only non-secret identity fields are sent.
        dl_payload = {
            "id_number": dl_number,
            "name": (user or {}).get("name"),
            "dob": (user or {}).get("kyc_date_of_birth"),
            "vehicle_type": (vehicle or {}).get("vehicle_type"),
        }
        dl = self._post(self.paths["dl"], dl_payload)
        if dl is None:
            return make_result(INCONCLUSIVE, {}, [REASON_DL_UNREADABLE])

        # TODO: map the provider's response into checks. Until the response
        # contract is confirmed, the adapter reports inconclusive rather than
        # asserting a pass from an unverified field name.
        return make_result(
            INCONCLUSIVE,
            {CHECK_DL_EXISTS: "pass"},
            [REASON_DL_UNREADABLE],
            provider_ref=str(dl.get("id") or dl.get("requestId") or ""),
            raw_ref=hashlib.sha256(repr(sorted(dl.keys())).encode()).hexdigest()[:12],
        )

    def parse_webhook(self, payload):
        # TODO: confirm the provider's webhook envelope (event id, status field,
        # signature header name) against its documentation.
        if not isinstance(payload, dict):
            return None
        return {
            "event_id": str(payload.get("id") or payload.get("eventId") or ""),
            "vehicle_id": str(payload.get("vehicleId") or ""),
            "status": payload.get("status"),
            "checks": payload.get("checks") or {},
            "reasons": payload.get("reasons") or [],
            "provider_ref": str(payload.get("requestId") or ""),
            "occurred_at": payload.get("timestamp"),
        }


def provider_name():
    return str(current_app.config.get("KYC_PROVIDER") or "manual").strip().lower()


def get_provider():
    """The adapter for the configured KYC_PROVIDER, or None for `manual`."""
    name = provider_name()
    if name in ("", "manual"):
        return None
    if name == "mock":
        return MockProvider()
    if name == "signzy":
        return SignzyProvider()
    # validate_runtime() rejects unknown providers in production; getting here in
    # development means the setting is a typo, so fail closed to manual.
    log.warning("unknown KYC_PROVIDER %r; falling back to manual review", name)
    return None


def verify_signature(raw_body, signature, secret):
    """Constant-time HMAC-SHA256 check for provider webhooks.

    `signature` may arrive hex or base64; both are accepted because providers
    differ and neither is a secret. An empty secret fails closed.
    """
    if not secret or not signature:
        return False
    import base64

    digest = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).digest()
    candidates = {digest.hex(), base64.b64encode(digest).decode("ascii")}
    presented = str(signature).strip()
    return any(hmac.compare_digest(candidate, presented) for candidate in candidates)
