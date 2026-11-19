"""Geocoding proxy: auth, region bias, caching, and resilient parsing.

These tests never hit the network: `requests.get` is replaced with a stub that
records the outgoing parameters so we can assert on the Nominatim contract.
"""

import pytest

from backend.blueprints import geo as geomod


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    """The suggestion cache is process-wide; keep tests independent.

    The offline gazetteer is disabled here so these tests exercise the
    Nominatim contract; test_places.py covers the gazetteer itself.
    """
    geomod.clear_search_cache()
    monkeypatch.setattr(geomod.places, "search_places", lambda q, limit=8: [])
    monkeypatch.setattr(geomod.places, "has_confident_match", lambda q, results: False)
    yield
    geomod.clear_search_cache()


def fake_ok(url, params=None, headers=None, timeout=None, **kwargs):
    return FakeResponse([{"lat": "12.9716", "lon": "77.5946",
                          "display_name": "Bengaluru, Karnataka, India"}])


def test_search_requires_authentication(client):
    resp = client.get("/api/geo/search?q=Bengaluru")
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "auth_required"


def test_route_and_reverse_require_authentication(client):
    assert client.get("/api/geo/route?points=1,2,3,4").status_code == 401
    assert client.get("/api/geo/reverse?lat=12.9&lng=77.5").status_code == 401


def test_search_biases_results_to_the_configured_country(client, rider, monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update({"url": url, "params": params, "headers": headers})
        return FakeResponse([
            {"lat": "12.9716", "lon": "77.5946",
             "display_name": "Bengaluru, Karnataka, India"},
            # Malformed rows must not wipe out the valid ones.
            {"lat": "not-a-number", "lon": "77.0", "display_name": "Broken row"},
            {"lon": "77.1", "display_name": "Missing latitude"},
            "not-an-object",
        ])

    monkeypatch.setitem(client.application.config, "GEO_COUNTRY_CODES", "in")
    monkeypatch.setattr(geomod.requests, "get", fake_get)

    resp = client.get("/api/geo/search?q=Ben&limit=6", headers=rider["auth"])

    assert resp.status_code == 200
    body = resp.get_json()
    assert [item["label"] for item in body["data"]] == ["Bengaluru, Karnataka"]
    assert body["data"][0]["lat"] == pytest.approx(12.9716)
    assert body["data"][0]["lng"] == pytest.approx(77.5946)
    assert seen["params"]["q"] == "Ben"
    assert seen["params"]["limit"] == 6
    assert seen["params"]["countrycodes"] == "in"
    assert seen["headers"]["User-Agent"].startswith("ridemate")


def test_repeated_prefix_is_served_from_the_cache(client, rider, monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return fake_ok(url, params=params)

    monkeypatch.setitem(client.application.config, "GEO_SEARCH_CACHE_TTL_SECONDS", 300)
    monkeypatch.setitem(client.application.config, "GEO_SEARCH_CACHE_MAX", 8)
    monkeypatch.setattr(geomod.requests, "get", fake_get)

    for _ in range(3):
        resp = client.get("/api/geo/search?q=Whitefield", headers=rider["auth"])
        assert resp.status_code == 200
        assert resp.get_json()["data"][0]["label"] == "Bengaluru, Karnataka"

    assert len(calls) == 1


def test_invalid_limit_is_a_validation_error(client, rider):
    resp = client.get("/api/geo/search?q=Ben&limit=abc", headers=rider["auth"])
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "validation_error"


def test_upstream_failure_surfaces_an_error_instead_of_an_empty_list(client, rider, monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("nominatim unreachable")

    monkeypatch.setattr(geomod.requests, "get", boom)

    resp = client.get("/api/geo/search?q=Ben", headers=rider["auth"])
    assert resp.status_code == 502
    assert resp.get_json()["error"]["code"] == "geo_unavailable"


def test_reverse_geocoding_failure_is_graceful(client, rider, monkeypatch):
    monkeypatch.setattr(geomod.requests, "get",
                        lambda *a, **k: FakeResponse({}, status=503))

    resp = client.get("/api/geo/reverse?lat=12.9&lng=77.5", headers=rider["auth"])
    assert resp.status_code == 200
    assert resp.get_json()["place"] is None


def test_reverse_rejects_malformed_coordinates(client, rider):
    resp = client.get("/api/geo/reverse?lat=north&lng=77.5", headers=rider["auth"])
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "validation_error"


def test_providers_reports_osm_geocoding(client, rider):
    resp = client.get("/api/geo/providers", headers=rider["auth"])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["geocoding"] == "osm"
    assert "geocoding_country_codes" in body


def _use_geoapify(app, monkeypatch, key="geoapify-test-key"):
    monkeypatch.setitem(app.config, "GEOCODING_PROVIDER", "geoapify")
    monkeypatch.setitem(app.config, "GEOAPIFY_API_KEY", key)


GEOAPIFY_FEATURE = {
    "properties": {
        "name": "Thanjavur New Bus Stand",
        "formatted": "Thanjavur New Bus Stand, New Bus Stand Road, Thanjavur, Tamil Nadu, India",
        "lat": 10.7867,
        "lon": 79.1378,
    },
    "geometry": {"coordinates": [79.1378, 10.7867]},
}


def test_geoapify_rows_are_mapped_and_the_key_stays_server_side(client, rider, app, monkeypatch):
    _use_geoapify(app, monkeypatch)
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        seen.update({"url": url, "params": params})
        return FakeResponse({"features": [GEOAPIFY_FEATURE]})

    monkeypatch.setattr(geomod.requests, "get", fake_get)

    resp = client.get("/api/geo/search?q=Thanjavur%20new%20bus%20stand",
                      headers=rider["auth"])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data"], body
    top = body["data"][0]
    assert top["label"].startswith("Thanjavur New Bus Stand")
    assert top["address"].endswith("India")
    assert seen["url"] == app.config["GEOAPIFY_SEARCH_URL"]
    assert seen["params"]["apiKey"] == "geoapify-test-key"
    assert seen["params"]["filter"] == "countrycode:in"
    # The browser must never receive the key.
    assert "geoapify-test-key" not in resp.get_data(as_text=True)


def test_geoapify_outage_falls_back_to_nominatim(client, rider, app, monkeypatch):
    _use_geoapify(app, monkeypatch)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        calls.append(url)
        if "geoapify" in url:
            raise ConnectionError("quota exceeded")
        return FakeResponse([{"lat": "10.7867", "lon": "79.1378",
                              "display_name": "Thanjavur New Bus Stand, Tamil Nadu, India"}])

    monkeypatch.setattr(geomod.requests, "get", fake_get)

    resp = client.get("/api/geo/search?q=Thanjavur%20new%20bus%20stand",
                      headers=rider["auth"])
    assert resp.status_code == 200
    assert resp.get_json()["data"]
    assert any("nominatim" in url for url in calls)


def test_reverse_uses_geoapify_when_configured(client, rider, app, monkeypatch):
    _use_geoapify(app, monkeypatch)
    monkeypatch.setattr(geomod.requests, "get",
                        lambda *a, **k: FakeResponse({"features": [GEOAPIFY_FEATURE]}))

    body = client.get("/api/geo/reverse?lat=10.78&lng=79.13",
                      headers=rider["auth"]).get_json()
    assert body["place"]["label"] == "Thanjavur New Bus Stand"
    assert body["place"]["address"].endswith("India")


def test_providers_reports_geoapify_when_configured(client, rider, app, monkeypatch):
    _use_geoapify(app, monkeypatch)
    body = client.get("/api/geo/providers", headers=rider["auth"]).get_json()
    assert body["geocoding"] == "geoapify"


def _responder(answers, calls):
    """Stub requests.get: return `answers[q]`, default to an empty list."""

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["q"])
        rows = answers.get(params["q"], [])
        return FakeResponse(rows)

    return fake_get


def test_glued_place_words_are_split_before_searching(client, rider, monkeypatch):
    calls = []
    answer = [{"lat": "10.7870", "lon": "79.1378",
               "display_name": "Thanjavur New Bus Stand, Thanjavur, Tamil Nadu, India"}]
    monkeypatch.setattr(geomod.requests, "get",
                        _responder({"Thanjavur, new bus stand": answer}, calls))

    resp = client.get("/api/geo/search?q=Thanjavur, new busstand", headers=rider["auth"])

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["query"] == "Thanjavur, new bus stand"
    assert body["relaxed"] is False
    assert body["data"][0]["lat"] == pytest.approx(10.787)
    # "busstand" is split, not dropped, and no extra request was needed.
    assert calls == ["Thanjavur, new bus stand"]


def test_zero_results_fall_back_to_the_town_itself(client, rider, monkeypatch):
    calls = []
    answer = [{"lat": "10.7870", "lon": "79.1378",
               "display_name": "Thanjavur, Tamil Nadu, India"}]
    monkeypatch.setattr(geomod.requests, "get", _responder({"Thanjavur": answer}, calls))

    resp = client.get("/api/geo/search?q=Thanjavur, new busstand", headers=rider["auth"])

    body = resp.get_json()
    assert [item["label"] for item in body["data"]] == ["Thanjavur, Tamil Nadu"]
    assert body["query"] == "Thanjavur"
    assert body["relaxed"] is True
    # Full query, commas-to-spaces, then the leading fragment: bounded, so a
    # typo-ahead field cannot turn into a burst of Nominatim calls.
    assert calls == ["Thanjavur, new bus stand", "Thanjavur new bus stand", "Thanjavur"]


def test_relaxed_answer_is_cached_with_the_query_that_produced_it(client, rider, monkeypatch):
    calls = []
    answer = [{"lat": "10.7870", "lon": "79.1378",
               "display_name": "Thanjavur, Tamil Nadu, India"}]
    monkeypatch.setitem(client.application.config, "GEO_SEARCH_CACHE_TTL_SECONDS", 300)
    monkeypatch.setitem(client.application.config, "GEO_SEARCH_CACHE_MAX", 8)
    monkeypatch.setattr(geomod.requests, "get", _responder({"Thanjavur": answer}, calls))

    first = client.get("/api/geo/search?q=Thanjavur, new busstand", headers=rider["auth"]).get_json()
    second = client.get("/api/geo/search?q=Thanjavur, new busstand", headers=rider["auth"]).get_json()

    assert first["query"] == second["query"] == "Thanjavur"
    assert second["relaxed"] is True
    assert len(calls) == 3  # one ladder, then the cache answers the repeat


def test_variants_are_bounded_and_deduplicated():
    assert geomod.search_variants("Bengaluru") == ["Bengaluru"]
    assert geomod.search_variants("  Whitefield ,  Bengaluru ") == [
        "Whitefield, Bengaluru", "Whitefield Bengaluru", "Whitefield", "Bengaluru"]
    # A one-word query has no simpler form to retry.
    assert geomod.search_variants("busstand") == ["bus stand"]
    # No commas: drop filler words, then fall back to the town. Descriptor
    # words ("stand") are never queried on their own.
    assert geomod.search_variants("thanjavur new bustand") == [
        "thanjavur new bus stand", "thanjavur bus stand", "thanjavur"]
    assert len(geomod.search_variants("anna nagar chennai airport road")) <= 4


def test_common_indian_misspellings_are_corrected():
    assert geomod.normalize_place_query("Kumbakonam bustand") == "Kumbakonam bus stand"
    assert geomod.normalize_place_query("trichy rlystn") == "trichy railway station"
    assert geomod.normalize_place_query("Chennai bustop") == "Chennai bus stop"


def test_single_phrase_with_a_typo_still_finds_the_landmark(client, rider, monkeypatch):
    calls = []
    answer = [{"lat": "10.7501", "lon": "79.1123",
               "display_name": "Thanjavur New Bus Stand, New Bus Stand Road, Thanjavur, Tamil Nadu, India"}]
    # Only the corrected spelling exists upstream.
    monkeypatch.setattr(geomod.requests, "get",
                        _responder({"thanjavur new bus stand": answer}, calls))

    body = client.get("/api/geo/search?q=thanjavur new bustand",
                      headers=rider["auth"]).get_json()

    assert body["data"][0]["label"].startswith("Thanjavur New Bus Stand")
    assert body["relaxed"] is False
    assert calls == ["thanjavur new bus stand"]


def test_single_phrase_falls_back_to_the_town_when_nothing_matches(client, rider, monkeypatch):
    calls = []
    answer = [{"lat": "10.7870", "lon": "79.1378",
               "display_name": "Thanjavur, Tamil Nadu, India"}]
    monkeypatch.setattr(geomod.requests, "get", _responder({"Thanjavur": answer}, calls))

    body = client.get("/api/geo/search?q=Thanjavur someunknownspot",
                      headers=rider["auth"]).get_json()

    assert [item["label"] for item in body["data"]] == ["Thanjavur, Tamil Nadu"]
    assert body["query"] == "Thanjavur"
    assert body["relaxed"] is True
    assert calls == ["Thanjavur someunknownspot", "Thanjavur"]



def test_repeated_rows_for_one_place_are_collapsed(client, rider, monkeypatch):
    rows = [
        {"lat": "10.9601", "lon": "79.3845",
         "display_name": "Kumbakonam, Thanjavur, Tamil Nadu, 612001, India"},
        {"lat": "10.9601", "lon": "79.3845",
         "display_name": "Kumbakonam, Thanjavur, Tamil Nadu, India"},
        # Same label, far away: a different place with a recycled name.
        {"lat": "11.5000", "lon": "78.1000",
         "display_name": "Kumbakonam, Thanjavur, Tamil Nadu, India"},
    ]
    monkeypatch.setattr(geomod.requests, "get",
                        lambda *a, **k: FakeResponse(rows))

    body = client.get("/api/geo/search?q=Kumbakonam", headers=rider["auth"]).get_json()
    assert len(body["data"]) == 2
    assert body["relaxed"] is False


LOCAL_KARAIKUDI = {
    "label": "Karaikkudi, Sivaganga",
    "address": "Karaikkudi, Sivaganga, Tamil Nadu, India",
    "lat": 10.0667, "lng": 78.7667, "_score": 1100,
}


def test_gazetteer_answers_without_calling_osm_for_an_exact_town(client, rider, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("upstream must not be called for an exact town")

    monkeypatch.setattr(geomod.requests, "get", fail)
    monkeypatch.setattr(geomod.places, "search_places",
                        lambda q, limit=8: [dict(LOCAL_KARAIKUDI)])
    monkeypatch.setattr(geomod.places, "has_confident_match", lambda q, r: True)

    body = client.get("/api/geo/search?q=Karaikudi", headers=rider["auth"]).get_json()

    assert body["data"][0]["label"] == "Karaikkudi, Sivaganga"
    assert body["relaxed"] is False
    assert calls == []


def test_gazetteer_carries_the_search_when_osm_is_down(client, rider, monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("nominatim unreachable")

    monkeypatch.setattr(geomod.requests, "get", boom)
    monkeypatch.setattr(geomod.places, "search_places",
                        lambda q, limit=8: [dict(LOCAL_KARAIKUDI)])
    monkeypatch.setattr(geomod.places, "has_confident_match", lambda q, r: False)

    resp = client.get("/api/geo/search?q=Karaikudi bus stand", headers=rider["auth"])
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["data"][0]["label"] == "Karaikkudi, Sivaganga"
    assert body["relaxed"] is True


def test_poi_matching_more_query_words_outranks_the_town(client, rider, monkeypatch):
    local = [{"label": "Thanjavur, Tamil Nadu",
              "address": "Thanjavur, Tamil Nadu, India",
              "lat": 10.785, "lng": 79.137, "_score": 460}]
    poi = [{"label": "Thanjavur New Bus Stand, New Bus Stand Road",
            "address": "Thanjavur New Bus Stand, New Bus Stand Road, Thanjavur, Tamil Nadu, India",
            "lat": 10.7501, "lng": 79.1123}]
    monkeypatch.setattr(geomod.requests, "get",
                        lambda *a, **k: FakeResponse([
                            {"lat": "10.7501", "lon": "79.1123",
                             "display_name": poi[0]["address"]}]))
    monkeypatch.setattr(geomod.places, "search_places", lambda q, limit=8: [dict(local[0])])
    monkeypatch.setattr(geomod.places, "has_confident_match", lambda q, r: False)

    body = client.get("/api/geo/search?q=thanjavur new bus stand",
                      headers=rider["auth"]).get_json()

    assert body["data"][0]["label"].startswith("Thanjavur New Bus Stand")
    assert any(item["label"] == "Thanjavur, Tamil Nadu" for item in body["data"])

