"""Geocoding (Geoapify or Nominatim/OSM) and routing (OSRM), proxied server-side."""

import re
import threading
import time
from collections import OrderedDict

import requests
from flask import Blueprint, current_app, request

from .. import places
from ..errors import APIError
from ..placenames import is_descriptor, meaningful_tokens, normalize_place_query
from ..ratelimit import rate_limit
from ..security import require_auth
from ..validators import as_float, as_int

bp = Blueprint("geo", __name__, url_prefix="/api/geo")

# Best-effort suggestion cache. Process-local: several web workers each keep
# their own copy, which still collapses the burst of identical prefix queries
# one typing session produces (Nominatim asks for at most ~1 request/second).
_geo_cache = OrderedDict()
_geo_cache_lock = threading.Lock()


def _cache_ttl():
    return current_app.config.get("GEO_SEARCH_CACHE_TTL_SECONDS", 0) or 0


def _cache_get(key):
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _geo_cache_lock:
        hit = _geo_cache.get(key)
        if hit is None:
            return None
        stored_at, payload = hit
        if now - stored_at > ttl:
            _geo_cache.pop(key, None)
            return None
        _geo_cache.move_to_end(key)
        return payload


def _cache_put(key, payload):
    ttl = _cache_ttl()
    limit = current_app.config.get("GEO_SEARCH_CACHE_MAX", 0) or 0
    if ttl <= 0 or limit <= 0:
        return
    with _geo_cache_lock:
        _geo_cache[key] = (time.monotonic(), payload)
        _geo_cache.move_to_end(key)
        while len(_geo_cache) > limit:
            _geo_cache.popitem(last=False)


def clear_search_cache():
    """Drop cached suggestions (used by tests and admin helpers)."""
    with _geo_cache_lock:
        _geo_cache.clear()


def _headers():
    agent = current_app.config.get("OSM_USER_AGENT") or "ridemate/1.0"
    contact = (current_app.config.get("GEO_CONTACT_URL")
               or current_app.config.get("PUBLIC_BASE_URL") or "").strip()
    if contact and contact not in agent:
        agent = f"{agent} (+{contact})"
    return {
        "User-Agent": agent,
        "Accept-Language": "en",
    }


# Nominatim matches whole indexed words: it does not spell-correct and it does
# not split compounds. The normalization lives in backend/placenames.py because
# the offline gazetteer needs exactly the same corrections.


def _add_variant(variants, text):
    text = " ".join((text or "").split()).strip(" ,")
    if text and text.casefold() not in {v.casefold() for v in variants}:
        variants.append(text)


def search_variants(q):
    """Candidate queries, most specific first.

    A broad match beats "No places found" for a town that obviously exists, so
    when the query only fails because of an extra word, a typo or an unusual
    format we retry the simpler forms. The ladder is deliberately short (at
    most four upstream calls): Nominatim asks for roughly one request per
    second, and a typo-ahead field must not turn into a burst.
    """
    normalized = normalize_place_query(q)
    variants = []
    _add_variant(variants, normalized)

    parts = [part.strip() for part in re.split(r"[,\n]+", normalized) if part.strip()]
    if len(parts) > 1:
        # "Whitefield, Bengaluru" as one string.
        _add_variant(variants, " ".join(parts))
        # "area, city": the first fragment is the locality, the second the town
        # (later fragments are usually district/state and match the whole state).
        _add_variant(variants, parts[0])
        _add_variant(variants, parts[1])
        return variants[:4]

    words = normalized.split()
    if len(words) > 1:
        # Drop filler words that rarely appear in OSM names.
        meaningful = meaningful_tokens(normalized)
        if meaningful:
            _add_variant(variants, " ".join(meaningful))
        # Town guess. People type both "Thanjavur bus stand" and
        # "bus stand Thanjavur", so try both ends, skipping descriptor words
        # that would only match noise.
        for token in (words[0], words[-1]):
            if not is_descriptor(token):
                _add_variant(variants, token)
    return variants[:4]


def _timeout():
    return current_app.config["OUTBOUND_TIMEOUT_SECONDS"]


def _geocoding_provider():
    """`geoapify` when a key is configured, otherwise Nominatim (`osm`)."""
    provider = (current_app.config.get("GEOCODING_PROVIDER") or "osm").strip().lower()
    if provider == "geoapify" and not current_app.config.get("GEOAPIFY_API_KEY"):
        return "osm"
    return provider


def _geoapify_timeout():
    return (current_app.config.get("GEOAPIFY_TIMEOUT_SECONDS")
            or _timeout())


def _geoapify_item(feature):
    """Map one Geoapify GeoJSON feature to our suggestion shape."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    try:
        lng = float(props["lon"])
        lat = float(props["lat"])
    except (KeyError, TypeError, ValueError):
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except (IndexError, TypeError, ValueError):
            return None
    formatted = (props.get("formatted") or "").strip()
    if not formatted:
        name = (props.get("name") or props.get("address_line1") or "").strip()
        region = ", ".join(
            part for part in (props.get("city") or props.get("district"),
                              props.get("state"), "India") if part)
        formatted = ", ".join(part for part in (name, region) if part)
    if not formatted:
        return None
    # Keep the same "Name, Town" short form as the Nominatim rows so both
    # providers render identically in the dropdown.
    short = ", ".join(part.strip() for part in formatted.split(",")[:2] if part.strip())
    return {"label": short or formatted, "address": formatted, "lat": lat, "lng": lng}


def _geoapify_search(term, limit, countries):
    params = {
        "text": term,
        "limit": max(1, min(int(limit), 10)),
        "lang": "en",
        "apiKey": current_app.config["GEOAPIFY_API_KEY"],
    }
    codes = [code.strip().lower() for code in (countries or "").split(",") if code.strip()]
    if codes:
        # Same bias as the Nominatim `countrycodes` parameter: a short prefix
        # must not match a place on another continent.
        params["filter"] = "countrycode:" + ",".join(codes)
    resp = requests.get(
        current_app.config["GEOAPIFY_SEARCH_URL"],
        params=params,
        timeout=_geoapify_timeout(),
    )
    resp.raise_for_status()
    body = resp.json()
    features = body.get("features") if isinstance(body, dict) else None
    if not isinstance(features, list):
        current_app.logger.warning(
            "Geoapify search returned %s, expected features", type(body).__name__)
        return []
    return _dedupe([mapped for mapped in (_geoapify_item(f) for f in features)
                    if mapped is not None])


def _nominatim_search(term, limit, countries):
    params = {"q": term, "format": "json", "limit": limit, "addressdetails": 0}
    if countries:
        # Keep suggestions in the deployment's region. Without this a short
        # prefix such as "Mum" matches places in Japan, Bosnia, Spain, ...
        params["countrycodes"] = countries
    resp = requests.get(
        current_app.config["NOMINATIM_SEARCH"],
        params=params,
        headers=_headers(),
        timeout=_timeout(),
    )
    resp.raise_for_status()
    raw = resp.json()
    if not isinstance(raw, list):
        current_app.logger.warning(
            "Geocoding search returned %s, expected a list", type(raw).__name__)
        raw = []
    return _dedupe([mapped for mapped in (_map_item(it) for it in raw)
                    if mapped is not None])


def route_distance_minutes(a, b):
    """Server-side (distance_km, duration_min) for two {lat,lng} points.

    Uses the configured OSRM service. Raises on any failure so callers
    can fall back to the haversine straight-line estimate.
    """
    service_url = current_app.config["OSRM_ROUTE"]
    resp = requests.get(
        f"{service_url}/{b['lng']},{b['lat']};{a['lng']},{a['lat']}",
        params={"overview": "false", "alternatives": "false"},
        headers=_headers(),
        timeout=_timeout(),
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("routes"):
        raise ValueError("no route")
    r = body["routes"][0]
    return round(r["distance"] / 1000, 1), round(r["duration"] / 60)


def _map_item(item):
    """Map one Nominatim row to our shape, or None when it is unusable.

    A single malformed row must not discard the whole result set, so callers
    filter these out instead of failing the request.
    """
    if not isinstance(item, dict):
        return None
    try:
        lat = float(item["lat"])
        lng = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    display = item.get("display_name") or item.get("name") or ""
    if not isinstance(display, str):
        return None
    # Nominatim's display_name has no space after commas except in the middle
    # of a name ("Bengaluru,Karnataka" vs "Bengaluru, Karnataka"), so strip
    # each segment before joining to avoid a doubled space.
    short = ", ".join(part.strip() for part in display.split(",")[:2] if part.strip())
    if not short:
        return None
    return {"label": short, "address": display, "lat": lat, "lng": lng}


def _dedupe(items):
    """Drop rows that name the same place at the same point.

    Nominatim returns the same locality twice when it exists both as a node and
    as a boundary (e.g. "Kumbakonam, Thanjavur" with and without a postcode),
    which burns a third of a six-row suggestion list.
    """
    seen = set()
    unique = []
    for item in items:
        key = (item["label"].casefold(), round(item["lat"], 4), round(item["lng"], 4))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _public_items(items):
    """Drop the internal ranking fields before sending suggestions out."""
    return [{"label": item["label"], "address": item["address"],
             "lat": item["lat"], "lng": item["lng"]} for item in items]


def _folded_words(text):
    return re.findall(r"[0-9a-z]+", " ".join(str(text or "").casefold().split()))


def _upstream_score(item, tokens):
    """How much of the typed query the upstream POI row actually covers."""
    words = _folded_words(item.get("label", "")) + _folded_words(item.get("address", ""))
    if not words:
        return 380
    matched = sum(1 for token in tokens
                  if any(word.startswith(token) for word in words))
    if not matched:
        # The row came back from the (possibly relaxed) upstream query, so keep
        # it — the local gazetteer should not silently discard POIs.
        return 380
    return 480 + 260 * (matched / len(tokens))


def _same_place(a, b):
    name_a = " ".join(str(a.get("label", "")).split(",")[0].casefold().split())
    name_b = " ".join(str(b.get("label", "")).split(",")[0].casefold().split())
    if name_a != name_b:
        return False
    try:
        return (abs(float(a["lat"]) - float(b["lat"])) <= 0.05
                and abs(float(a["lng"]) - float(b["lng"])) <= 0.05)
    except (TypeError, ValueError):
        return True


def _merge_results(local_items, upstream_items, q, limit):
    """Rank gazetteer towns and OSM POIs together, dropping duplicates.

    The typed words decide the order: a query that a POI matches in full
    ("Thanjavur new bus stand") outranks the town that shares one word, while a
    town name itself outranks the POIs around it.
    """
    tokens = meaningful_tokens(normalize_place_query(q)) or [q.casefold()]
    candidates = [(item.get("_score", 0), item) for item in local_items]
    for index, item in enumerate(upstream_items):
        score = _upstream_score(item, tokens)
        # Nominatim's own order is meaningful for equal coverage.
        candidates.append((score + max(0, 25 - index * 3), item))

    candidates.sort(key=lambda pair: -pair[0])
    chosen = []
    for _score, item in candidates:
        if any(_same_place(item, other) for other in chosen):
            continue
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return _public_items(chosen)


@bp.get("/search")
@require_auth
@rate_limit("default")
def search():
    q = (request.args.get("q") or "").strip()
    limit = min(as_int(request.args.get("limit"), "limit", minimum=1) or 6, 10)
    if not q:
        return {"ok": True, "data": [], "query": "", "relaxed": False}

    countries = (current_app.config.get("GEO_COUNTRY_CODES") or "").strip()
    provider = _geocoding_provider()
    cache_key = (q.casefold(), limit, countries, provider)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"ok": True, "data": [dict(item) for item in cached["data"]],
                "query": cached["query"], "relaxed": cached["relaxed"]}

    # The offline gazetteer knows every district, taluk and settlement in India,
    # including the ones OSM has never mapped. It answers instantly, so an exact
    # town name never needs the upstream POI search at all.
    local_items = places.search_places(q, limit=limit)
    if places.has_confident_match(q, local_items):
        payload = {"data": _public_items(local_items),
                   "query": q, "relaxed": False}
        _cache_put(cache_key, payload)
        return {"ok": True, **payload}

    variants = search_variants(q)
    used = variants[0]
    upstream_items = []
    try:
        for term in variants:
            try:
                if provider == "geoapify":
                    upstream_items = _geoapify_search(term, limit, countries)
                else:
                    upstream_items = _nominatim_search(term, limit, countries)
            except Exception:  # noqa: BLE001 - upstream is outside our control
                if provider != "geoapify":
                    raise
                # Paid geocoder down, over quota or key rotated: the free one
                # still answers, and a suggestions box must not go blank.
                current_app.logger.warning(
                    "Geoapify search failed for %r; falling back to Nominatim", term,
                    exc_info=True)
                upstream_items = _nominatim_search(term, limit, countries)
            used = term
            if upstream_items:
                break
    except Exception as exc:  # noqa: BLE001 - upstream is outside our control
        if not local_items:
            current_app.logger.warning("Geocoding search failed for %r: %s", q, exc)
            raise APIError(
                "Place search is temporarily unavailable. Please try again.",
                502,
                code="geo_unavailable",
            ) from exc
        # The gazetteer still has the towns; degrade to local suggestions
        # instead of failing the field.
        current_app.logger.warning(
            "Geocoding search failed for %r (%s); serving local gazetteer only",
            q, exc)
        used = q
        upstream_items = []

    relaxed = bool(upstream_items) and used.casefold() != variants[0].casefold()
    if relaxed:
        current_app.logger.info("Geocoding search for %r relaxed to %r", q, used)

    items = _merge_results(local_items, upstream_items, q, limit)
    if not upstream_items and local_items:
        # Partial local match ("Thanjavur someunknownspot" -> Thanjavur): the
        # dropdown says which part actually matched.
        relaxed = True
        used = items[0]["label"].split(",")[0] if items else q

    payload = {"data": items, "query": used, "relaxed": relaxed}
    _cache_put(cache_key, payload)
    return {"ok": True, **payload}


@bp.get("/reverse")
@require_auth
@rate_limit("default")
def reverse():
    lat = as_float(request.args.get("lat"), "lat", minimum=-90, maximum=90, required=True)
    lng = as_float(request.args.get("lng"), "lng", minimum=-180, maximum=180, required=True)
    try:
        if _geocoding_provider() == "geoapify":
            resp = requests.get(
                current_app.config["GEOAPIFY_REVERSE_URL"],
                params={"lat": lat, "lon": lng, "lang": "en",
                        "apiKey": current_app.config["GEOAPIFY_API_KEY"]},
                timeout=_geoapify_timeout(),
            )
            resp.raise_for_status()
            body = resp.json()
            features = body.get("features") if isinstance(body, dict) else None
            feature = features[0] if isinstance(features, list) and features else None
            place = _geoapify_item(feature) if feature else None
            if place is None:
                return {"ok": True, "place": None}
            props = feature.get("properties") or {}
            return {"ok": True, "place": {
                "label": (props.get("name") or place["label"]),
                "address": place["address"],
                "lat": place["lat"],
                "lng": place["lng"]}}
        resp = requests.get(
            current_app.config["NOMINATIM_REVERSE"],
            params={"lat": lat, "lon": lng, "format": "jsonv2"},
            headers=_headers(),
            timeout=_timeout(),
        )
        resp.raise_for_status()
        item = resp.json()
        return {"ok": True, "place": {"label": item.get("name") or item.get("display_name", ""),
                                      "address": item.get("display_name", ""),
                                      "lat": float(item.get("lat", lat)),
                                      "lng": float(item.get("lon", lng))}}
    except Exception as exc:  # noqa: BLE001
        current_app.logger.warning("Reverse geocoding failed for %s,%s: %s", lat, lng, exc)
        return {"ok": True, "place": None}


@bp.get("/route")
@require_auth
@rate_limit("default")
def route():
    """points=lat,lng,lat,lng (two waypoints) -> distance, duration, geometry."""

    points = (request.args.get("points") or "").strip().split(",")
    coords = []
    try:
        pts = [float(p) for p in points]
        coords = [(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)]
    except (ValueError, IndexError):
        pass
    if len(coords) < 2:
        raise APIError("Provide at least two lat,lng points.", 422, code="validation_error")

    a, b = coords[0], coords[1]
    service_url = current_app.config["OSRM_ROUTE"]
    try:
        resp = requests.get(
            f"{service_url}/{b[1]},{b[0]};{a[1]},{a[0]}",
            params={"overview": "full", "geometries": "geojson"},
            headers=_headers(),
            timeout=_timeout(),
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("routes"):
            raise ValueError("no route")
        r = body["routes"][0]
        geom = r.get("geometry", {})
        raw_coords = geom.get("coordinates", []) if isinstance(geom, dict) else []
        route = [[lat, lon] for lon, lat in raw_coords]
        return {
            "ok": True,
            "distance_km": round(r["distance"] / 1000, 1),
            "duration_min": round(r["duration"] / 60),
            "route": route,
            "source": "osrm",
        }
    except Exception as exc:  # noqa: BLE001 - fallback to straight line
        current_app.logger.info("Routing fallback: %s", exc)
        distance_km = round(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 * 111.0, 1)
        return {
            "ok": True,
            "distance_km": distance_km,
            "duration_min": round(distance_km / 35 * 60),
            "route": [a, b],
            "source": "straight",
        }


@bp.get("/providers")
@require_auth
@rate_limit("default")
def providers():
    cfg = current_app.config
    return {
        "ok": True,
        # Whichever upstream the deployment actually calls: Geoapify when a key
        # is configured, Nominatim otherwise (see _geocoding_provider).
        "geocoding": _geocoding_provider(),
        "geocoding_country_codes": cfg.get("GEO_COUNTRY_CODES") or "",
        "routing": "osrm",
        "maps": "leaflet-osm",
        "payments": "razorpay" if cfg.get("RAZORPAY_KEY_ID") else "demo",
    }
