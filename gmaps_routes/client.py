"""Client for Google Maps Platform routing APIs, tuned for use by an LLM.

* Routes API  : computeRoutes, computeRouteMatrix (TRANSIT, BICYCLE, WALK, DRIVE, TWO_WHEELER)
* Places (New): searchText          * Geocoding: forward / reverse / place_id

Design:
* one keep-alive HTTP session (TLS handshake once; ~3x lower latency on repeat calls)
* field masks trimmed to what the output uses (no polylines unless asked) -> 4-10x smaller
  responses.  Field masks do not change the Routes SKU; features do (see README).
* every public function returns compact text (format="text"), normalised JSON ("json")
  or the untouched API response ("raw").
The API key comes from GOOGLE_MAPS_API_KEY and is never logged or echoed.
"""
from __future__ import annotations

import copy
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

from . import render as R
from . import summary as S

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

TRAVEL_MODES = ("TRANSIT", "BICYCLE", "WALK", "DRIVE", "TWO_WHEELER")
_MODE_ALIAS = {"BIKE": "BICYCLE", "CYCLING": "BICYCLE", "BICYCLING": "BICYCLE", "WALKING": "WALK",
               "FOOT": "WALK", "CAR": "DRIVE", "DRIVING": "DRIVE", "PUBLIC": "TRANSIT",
               "PUBLIC_TRANSPORT": "TRANSIT", "PT": "TRANSIT", "MOTORCYCLE": "TWO_WHEELER",
               "SCOOTER": "TWO_WHEELER"}
DEFAULT_COMPARE = ("TRANSIT", "BICYCLE", "WALK")  # DRIVE is opt-in: traffic-aware = Pro SKU


def norm_mode(m: str) -> str:
    u = str(m).strip().upper().replace("-", "_").replace(" ", "_")
    u = _MODE_ALIAS.get(u, u)
    if u not in TRAVEL_MODES:
        raise ValueError(f"travel_mode must be one of {TRAVEL_MODES}")
    return u
TRANSIT_MODES = ("BUS", "SUBWAY", "TRAIN", "LIGHT_RAIL", "RAIL")
TRANSIT_ROUTING = ("LESS_WALKING", "FEWER_TRANSFERS")
FORMATS = ("text", "json", "raw")
DETAILS = ("brief", "steps")

_ROUTE_BASE = [
    "routes.duration", "routes.staticDuration", "routes.distanceMeters", "routes.routeLabels",
    "routes.warnings", "routes.optimizedIntermediateWaypointIndex",
    "routes.travelAdvisory.transitFare", "routes.travelAdvisory.tollInfo",
    "routes.localizedValues.transitFare", "geocodingResults",
    "routes.legs.startLocation", "routes.legs.endLocation",
]
_STEP_CORE = ["routes.legs.steps.travelMode", "routes.legs.steps.staticDuration",
              "routes.legs.steps.distanceMeters", "routes.legs.steps.transitDetails"]
_STEP_NAV = ["routes.legs.steps.navigationInstruction"]
# for format="raw": everything except per-step polylines (those are huge and rarely useful)
FULL_ROUTE_FIELDS = ",".join([
    "routes.routeLabels", "routes.distanceMeters", "routes.duration", "routes.staticDuration",
    "routes.polyline", "routes.description", "routes.warnings", "routes.viewport",
    "routes.travelAdvisory", "routes.optimizedIntermediateWaypointIndex", "routes.localizedValues",
    "routes.routeToken", "routes.legs.distanceMeters", "routes.legs.duration",
    "routes.legs.staticDuration", "routes.legs.startLocation", "routes.legs.endLocation",
    "routes.legs.travelAdvisory", "routes.legs.localizedValues", "routes.legs.stepsOverview",
    "routes.legs.steps.distanceMeters", "routes.legs.steps.staticDuration",
    "routes.legs.steps.startLocation", "routes.legs.steps.endLocation",
    "routes.legs.steps.navigationInstruction", "routes.legs.steps.travelAdvisory",
    "routes.legs.steps.localizedValues", "routes.legs.steps.transitDetails",
    "routes.legs.steps.travelMode", "geocodingResults",
])
MATRIX_FIELDS = "originIndex,destinationIndex,status,condition,distanceMeters,duration"
FULL_MATRIX_FIELDS = MATRIX_FIELDS + ",staticDuration,travelAdvisory,fallbackInfo,localizedValues"
# Places Text Search: these are Pro-SKU fields; DETAILED adds Enterprise-SKU fields.
PLACES_BASIC_FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "shortFormattedAddress", "location", "primaryTypeDisplayName",
    "googleMapsUri", "businessStatus"))
PLACES_DETAILED_FIELDS = PLACES_BASIC_FIELDS + "," + ",".join("places." + f for f in (
    "rating", "userRatingCount", "currentOpeningHours.openNow",
    "regularOpeningHours.weekdayDescriptions", "nationalPhoneNumber", "websiteUri", "priceLevel"))


class GoogleMapsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


# --------------------------------------------------------------------------- config
def api_key() -> str:
    key = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GMAPS_API_KEY")
    if not key:
        raise GoogleMapsError(
            "No API key: set GOOGLE_MAPS_API_KEY (Routes API; plus Places API (New) / "
            "Geocoding API for those tools).")
    return key


def default_tz() -> str:
    return os.environ.get("GMAPS_TZ", "Europe/Berlin")


def default_lang() -> str | None:
    v = os.environ.get("GMAPS_LANG", "en")
    return v or None


# --------------------------------------------------------------------------- inputs
_LATLNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_REL = re.compile(r"^\+\s*(\d+)\s*(m|min|h|d)$", re.I)


_WALL = re.compile(r"(?:(today|heute|tomorrow|morgen)\s+)?(\d{1,2}):(\d{2})", re.I)


def is_wall_time(value: Any) -> bool:
    """True for local wall-clock input ('08:30', 'tomorrow 08:30', naive ISO): its meaning
    depends on the time zone of the place."""
    if not isinstance(value, str) or not value.strip() or value.strip().lower() == "now":
        return False
    v = value.strip()
    if _REL.match(v) or _WALL.fullmatch(v):
        return bool(_WALL.fullmatch(v))
    try:
        return datetime.fromisoformat(v).tzinfo is None
    except ValueError:
        return False


def parse_time(value: str | datetime | None, tz: str | None = None) -> datetime | None:
    """-> aware UTC datetime, or None for now.

    ISO-8601 with offset; naive ISO and 'HH:MM' (next occurrence) / 'today HH:MM' /
    'tomorrow HH:MM' are wall-clock times in `tz` (default GMAPS_TZ, Europe/Berlin);
    relative '+45m' / '+2h' / '+1d'; 'now'.
    """
    z = S.zone(tz or default_tz())
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        v = str(value).strip()
        if not v or v.lower() == "now":
            return None
        if m := _REL.match(v):
            n, unit = int(m.group(1)), m.group(2).lower()[0]
            return datetime.now(timezone.utc) + timedelta(
                **{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})
        if m := _WALL.fullmatch(v):
            now = datetime.now(z)
            day, h, mi = (m.group(1) or "").lower(), int(m.group(2)), int(m.group(3))
            dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if day in ("tomorrow", "morgen"):
                dt += timedelta(days=1)
            elif not day and dt < now - timedelta(minutes=5):
                dt += timedelta(days=1)  # bare HH:MM already past -> next occurrence
        else:
            try:
                dt = datetime.fromisoformat(v)
            except ValueError as e:
                raise ValueError(f"Unparseable time {value!r}; use ISO-8601 "
                                 "(2026-11-04T08:30), 'HH:MM', or '+45m'") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=z)
    return dt.astimezone(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def waypoint(spec: Any, *, via: bool = False) -> dict:
    """str : 'Bonn Hbf' | '50.7323,7.0969' | 'place_id:ChIJ...' | 'via:<any of these>'
    dict: {"lat","lng"[,"heading"]} | {"place_id"} | {"address"} | an API Waypoint
          (extra keys: via, vehicle_stopover, side_of_road)"""
    if isinstance(spec, dict):
        d = dict(spec.get("waypoint", spec))
        if any(k in d for k in ("location", "placeId", "address")):
            out = copy.deepcopy(d)
        elif "lat" in d and "lng" in d:
            loc: dict = {"latLng": {"latitude": float(d["lat"]), "longitude": float(d["lng"])}}
            if d.get("heading") is not None:
                loc["heading"] = int(d["heading"])
            out = {"location": loc}
        elif "place_id" in d:
            out = {"placeId": d["place_id"]}
        elif "address" in d:
            out = {"address": d["address"]}
        else:
            raise ValueError(f"Unrecognised waypoint dict: {spec!r}")
        if d.get("via") or via:
            out["via"] = True
        if d.get("vehicle_stopover"):
            out["vehicleStopover"] = True
        if d.get("side_of_road"):
            out["sideOfRoad"] = True
        return out
    s = str(spec).strip()
    if s.lower().startswith("via:"):
        via, s = True, s[4:].strip()
    if m := _LATLNG.match(s):
        out = {"location": {"latLng": {"latitude": float(m.group(1)), "longitude": float(m.group(2))}}}
    elif s.lower().startswith(("place_id:", "placeid:")):
        out = {"placeId": s.split(":", 1)[1].strip()}
    elif s:
        out = {"address": s}
    else:
        raise ValueError("Empty waypoint")
    if via:
        out["via"] = True
    return out


def _label(spec: Any) -> str:
    if isinstance(spec, dict):
        d = spec.get("waypoint", spec)
        return str(d.get("address") or d.get("place_id") or d.get("placeId")
                   or f"{d.get('lat')},{d.get('lng')}")
    s = str(spec).strip()
    return s[4:].strip() if s.lower().startswith("via:") else s


# Which avoid flags each mode accepts (the API rejects the others with HTTP 400)
_AVOID_OK = {"DRIVE": {"tolls", "highways", "ferries"}, "TWO_WHEELER": {"tolls", "highways", "ferries"},
             "WALK": {"indoor"}, "BICYCLE": set(), "TRANSIT": set()}


def _avoid_for(mode: str, avoid: list[str] | None, notes: list[str]) -> list[str]:
    avoid = [a.lower().strip() for a in (avoid or []) if a.strip()]
    _route_modifiers(avoid)  # validates names
    ok = [a for a in avoid if a in _AVOID_OK.get(mode, set())]
    dropped = [a for a in avoid if a not in ok]
    if dropped:
        notes.append(f"avoid {', '.join(dropped)} ignored: not supported for {mode}")
    return ok


def _route_modifiers(avoid: list[str] | None) -> dict | None:
    names = {"tolls": "avoidTolls", "highways": "avoidHighways", "ferries": "avoidFerries",
             "indoor": "avoidIndoor"}
    avoid = [a.lower().strip() for a in (avoid or []) if a.strip()]
    bad = set(avoid) - set(names)
    if bad:
        raise ValueError(f"Unknown avoid option(s) {sorted(bad)}; allowed: {sorted(names)}")
    return {names[a]: True for a in avoid} or None


def _transit_prefs(modes: list[str] | None, routing: str | None) -> dict | None:
    prefs: dict = {}
    if modes:
        ms = [m.upper() for m in modes]
        bad = [m for m in ms if m not in TRANSIT_MODES]
        if bad:
            raise ValueError(f"Unknown transit mode(s) {bad}; allowed: {TRANSIT_MODES}")
        prefs["allowedTravelModes"] = ms
    if routing:
        if routing.upper() not in TRANSIT_ROUTING:
            raise ValueError(f"transit_routing must be one of {TRANSIT_ROUTING}")
        prefs["routingPreference"] = routing.upper()
    return prefs or None


def _deep_merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _check_format(fmt: str) -> None:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")


# --------------------------------------------------------------------------- http
_lock = threading.Lock()
_sess: requests.Session | None = None


def _session() -> requests.Session:
    """One shared keep-alive session (urllib3's pool is thread-safe), so the parallel
    compare_modes requests and later calls all reuse warm TLS connections."""
    global _sess
    with _lock:
        if _sess is None:
            _sess = requests.Session()
            _sess.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))
        return _sess


def _scrub(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key and key in text else text


def _send(method: str, url: str, *, headers: dict | None = None, json: Any = None,
          params: dict | None = None, key_in: str = "header", retries: int = 2) -> Any:
    key = api_key()
    headers = dict(headers or {})
    params = dict(params or {})
    if key_in == "header":
        headers["X-Goog-Api-Key"] = key
    else:
        params["key"] = key
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = _session().request(method, url, headers=headers, json=json, params=params,
                                   timeout=(5, 25))
        except requests.RequestException as e:  # message may contain the URL (and key)
            last = GoogleMapsError(_scrub(f"network error: {type(e).__name__}: {e}", key))
            time.sleep(0.5 * 2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            ra = r.headers.get("Retry-After", "")
            time.sleep(min(float(ra), 5.0) if ra.replace(".", "", 1).isdigit() else 0.5 * 2 ** attempt)
            continue
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw_text": r.text[:2000]}
        if r.status_code >= 400:
            err = payload.get("error", payload) if isinstance(payload, dict) else {}
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            st = err.get("status", "") if isinstance(err, dict) else ""
            raise GoogleMapsError(_scrub(f"HTTP {r.status_code} {st}: {msg}", key),
                                  r.status_code, payload)
        return payload
    raise last or GoogleMapsError("request failed")


# --------------------------------------------------------------------------- routes
def maps_url(origin: Any, destination: Any, intermediates: list | None, mode: str,
             depart: datetime | None = None, arrive: datetime | None = None) -> str:
    def txt(s: Any) -> tuple[str, str | None]:
        w = waypoint(s)
        if "placeId" in w:
            return _label(s), w["placeId"]
        if "location" in w:
            ll = w["location"]["latLng"]
            return f"{ll['latitude']},{ll['longitude']}", None
        return w.get("address", ""), None

    o, opid = txt(origin)
    d, dpid = txt(destination)
    q = {"api": "1", "origin": o, "destination": d,
         "travelmode": {"TRANSIT": "transit", "BICYCLE": "bicycling", "WALK": "walking",
                        "DRIVE": "driving", "TWO_WHEELER": "two-wheeler"}[mode]}
    if opid:
        q["origin_place_id"] = opid
    if dpid:
        q["destination_place_id"] = dpid
    if intermediates:
        q["waypoints"] = "|".join(txt(i)[0] for i in intermediates)
    return "https://www.google.com/maps/dir/?" + urlencode(q)


def build_route_request(origin, destination, *, intermediates=None, travel_mode="TRANSIT",
                        departure_time=None, arrival_time=None, alternatives=True,
                        transit_modes=None, transit_routing=None, routing_preference=None,
                        traffic_model=None, avoid=None, units="METRIC", language=None,
                        region=None, optimize_waypoint_order=False, polyline=False,
                        polyline_quality=None, geojson=False, extra_computations=None,
                        reference_routes=None, extra_request=None, tz_dep=None,
                        tz_arr=None) -> tuple[dict, list[str]]:
    """tz_dep / tz_arr: zone for wall-clock departure / arrival input (default GMAPS_TZ)."""
    mode = norm_mode(travel_mode)
    notes: list[str] = []
    dep = parse_time(departure_time, tz_dep)
    arr = parse_time(arrival_time, tz_arr)
    if dep and arr:
        raise ValueError("departure_time and arrival_time are mutually exclusive")
    if arr and mode != "TRANSIT":
        raise ValueError("arrival_time is only supported for TRANSIT (for other modes, leave "
                         "by arrival minus travel time; compare_modes does this for you)")
    if intermediates and mode == "TRANSIT":
        raise ValueError("TRANSIT does not support intermediate waypoints; route each leg "
                         "separately")

    body: dict = {"origin": waypoint(origin), "destination": waypoint(destination),
                  "travelMode": mode, "units": units.upper()}
    if intermediates:
        body["intermediates"] = [waypoint(i) for i in intermediates]
        if alternatives:
            notes.append("no alternatives: the API returns none when intermediates are used")
        alternatives = False
        if len(intermediates) > 25:
            raise ValueError("at most 25 intermediate waypoints")
    if optimize_waypoint_order:
        if not intermediates:
            raise ValueError("optimize_waypoint_order needs at least one intermediate")
        if any(w.get("via") for w in body["intermediates"]):
            raise ValueError("optimize_waypoint_order cannot be combined with 'via' intermediates")
        body["optimizeWaypointOrder"] = True
    if alternatives:
        body["computeAlternativeRoutes"] = True
    if polyline or polyline_quality or geojson:
        body["polylineQuality"] = (polyline_quality or "OVERVIEW").upper()
        body["polylineEncoding"] = "GEO_JSON_LINESTRING" if geojson else "ENCODED_POLYLINE"

    if mode in ("DRIVE", "TWO_WHEELER"):
        body["routingPreference"] = (routing_preference or "TRAFFIC_AWARE").upper()
        if traffic_model:
            body["trafficModel"] = traffic_model.upper()
    elif routing_preference or traffic_model:
        notes.append(f"routing_preference/traffic_model ignored for {mode}")

    if mode == "TRANSIT":
        tp = _transit_prefs(transit_modes, transit_routing)
        if tp:
            body["transitPreferences"] = tp
    elif transit_modes or transit_routing:
        notes.append(f"transit preferences ignored for {mode}")

    mods = _route_modifiers(_avoid_for(mode, avoid, notes))
    if mods:
        body["routeModifiers"] = mods
    # A departure time at/before 'now' is rejected for traffic-aware modes; treat as 'now'.
    if dep and (mode == "TRANSIT" or dep > datetime.now(timezone.utc) + timedelta(seconds=60)):
        body["departureTime"] = _rfc3339(dep)
    if arr:
        body["arrivalTime"] = _rfc3339(arr)
    lang = language or default_lang()
    if lang:
        body["languageCode"] = lang
    if region:
        body["regionCode"] = region
    if extra_computations:
        body["extraComputations"] = [e.upper() for e in extra_computations]
    if reference_routes:
        body["requestedReferenceRoutes"] = [r.upper() for r in reference_routes]
    if extra_request:
        _deep_merge(body, extra_request)
    return body, notes


def _route_mask(mode: str, detail: str, body: dict) -> str:
    f = list(_ROUTE_BASE)
    if mode == "TRANSIT" or detail == "steps":
        f += _STEP_CORE
    if detail == "steps":
        f += _STEP_NAV
    if "polylineQuality" in body:
        f.append("routes.polyline")
    return ",".join(f)


# ------------------------------------------------------------------ waypoint repair
_place_cache: dict[tuple[str, str | None], dict | None] = {}


_place_locks: dict = {}
_place_locks_guard = threading.Lock()


def resolve_place(text: str, language: str | None = None) -> dict | None:
    """Best Places (New) match for a free-text place -> {id, name, address}; cached in-process.
    A per-text lock makes parallel callers (compare_modes, matrix) share one lookup."""
    key = (text.strip().lower(), language)
    with _place_locks_guard:
        lock = _place_locks.setdefault(key, threading.Lock())
    with lock:
        return _resolve_locked(key, text, language)


def _resolve_locked(key, text, language):
    if key not in _place_cache:
        body = {"textQuery": text, "pageSize": 1}
        lang = language or default_lang()
        if lang:
            body["languageCode"] = lang
        raw = _send("POST", PLACES_SEARCH_URL, json=body, headers={
            "X-Goog-FieldMask": "places.id,places.displayName,places.shortFormattedAddress,"
                                "places.formattedAddress", "Content-Type": "application/json"})
        p = (raw.get("places") or [None])[0]
        _place_cache[key] = None if not p else {
            "id": p["id"], "name": (p.get("displayName") or {}).get("text"),
            "address": p.get("shortFormattedAddress") or p.get("formattedAddress")}
    return _place_cache[key]


def _partial_waypoints(raw: dict, body: dict) -> list[tuple[str, int | None, str]]:
    """Address waypoints that Google's route geocoder only partially matched.  It resolves
    e.g. 'Köln Hbf' to the city of Köln (partialMatch, type locality) and routes to the city
    centre, which silently gives a wrong trip."""
    gr = raw.get("geocodingResults") or {}
    out = []
    for role in ("origin", "destination"):
        g = gr.get(role) or {}
        if g.get("partialMatch") and "address" in body.get(role, {}):
            out.append((role, None, body[role]["address"]))
    for g in gr.get("intermediates") or []:
        i = g.get("intermediateWaypointRequestIndex", 0)
        w = (body.get("intermediates") or [])[i] if i < len(body.get("intermediates") or []) else {}
        if g.get("partialMatch") and "address" in w:
            out.append(("intermediates", i, w["address"]))
    return out


_known_partial: dict[tuple[str, str | None], dict] = {}


def _fix_note(text: str, p: dict) -> str:
    return (f"'{text}' → {p['name']}, {p['address']} (route geocoder matched it only "
            "partially; resolved via Places)")


def _apply_known(body: dict, lang: str | None) -> tuple[dict, list[str]]:
    if not _known_partial:
        return body, []
    body, notes = copy.deepcopy(body), []
    ws = [body["origin"], body["destination"], *(body.get("intermediates") or [])]
    for w in ws:
        p = _known_partial.get((w.get("address", "").strip().lower(), lang))
        if p:
            notes.append(_fix_note(w.pop("address"), p))
            w["placeId"] = p["id"]
    return body, notes


def _send_route(body: dict, mask: str) -> tuple[dict, list[str]]:
    """POST computeRoutes; if an address was only partially matched, re-resolve it with
    Places and retry once with the place_id."""
    hdr = {"X-Goog-FieldMask": mask, "Content-Type": "application/json"}
    lang = body.get("languageCode")
    # texts already known to be mis-geocoded: substitute their place_id before the first request
    body, pre = _apply_known(body, lang)
    raw = _send("POST", ROUTES_URL, json=body, headers=hdr)
    if "geocodingResults" not in mask:
        return raw, pre
    bad = _partial_waypoints(raw, body)
    if not bad:
        return raw, pre
    notes, body2 = list(pre), copy.deepcopy(body)
    for role, i, text in bad:
        p = resolve_place(text, body.get("languageCode"))
        if not p:
            notes.append(f"'{text}' matched only partially (Google may route to a whole city/area); "
                         "be more specific")
            continue
        w = {"placeId": p["id"], **({"via": True} if (body2[role][i] if i is not None else body2[role]).get("via") else {})}
        if i is None:
            body2[role] = w
        else:
            body2[role][i] = w
        _known_partial[(text.strip().lower(), lang)] = p
        notes.append(_fix_note(text, p))
    if body2 == body:
        return raw, notes
    return _send("POST", ROUTES_URL, json=body2, headers=hdr), notes


# ------------------------------------------------------------------ time zones
TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json"
# Zones sharing CET/CEST rules.  Points inside the boxes below are all on CET/CEST, so when
# home is one of these no lookup is needed.  Box 1: lat 42.5-57.5, lng 1.8-16 (east of the UK's
# easternmost point 1.76E: Benelux, eastern FR, DE, CH, AT, CZ, DK, S-NO/SE, W-PL, N-IT).
# Box 2: lat 37.6-42.5, lng 6.5-19 (IT incl. Sardinia, Corsica, HR/BA/ME coast; north of
# Tunisia, west of Greece).
_CET = {"Europe/Berlin", "Europe/Paris", "Europe/Amsterdam", "Europe/Brussels", "Europe/Vienna",
        "Europe/Zurich", "Europe/Rome", "Europe/Madrid", "Europe/Prague", "Europe/Warsaw",
        "Europe/Copenhagen", "Europe/Oslo", "Europe/Stockholm", "Europe/Luxembourg",
        "Europe/Budapest", "Europe/Ljubljana", "Europe/Zagreb", "Europe/Bratislava",
        "Europe/Monaco", "Europe/Andorra", "Europe/Vaduz", "Europe/Malta", "Europe/Belgrade"}
_tz_cache: dict[tuple[float, float], str | None] = {}
_tz_api = {"ok": True}


def tz_at(lat: float, lng: float) -> str | None:
    """IANA zone at a point (Time Zone API, cached per ~10 km cell; free fast path for the
    CET core when home is CET).  None if unknown or the API is not enabled for the key."""
    home = default_tz()
    if home in _CET and ((42.5 <= lat <= 57.5 and 1.8 <= lng <= 16.0)
                         or (37.6 <= lat < 42.5 and 6.5 <= lng <= 19.0)):
        return home
    key = (round(lat, 1), round(lng, 1))
    if key in _tz_cache:
        return _tz_cache[key]
    if not _tz_api["ok"] or os.environ.get("GMAPS_TZ_LOOKUP", "1") == "0":
        return None
    try:
        raw = _send("GET", TIMEZONE_URL, params={"location": f"{lat},{lng}",
                                                  "timestamp": int(time.time())},
                    key_in="param", retries=1)
    except GoogleMapsError:
        return None
    st = raw.get("status")
    if st == "OK":
        _tz_cache[key] = raw.get("timeZoneId")
    elif st in ("REQUEST_DENIED", "OVER_DAILY_LIMIT"):
        _tz_api["ok"] = False  # API not enabled: stop trying, fall back to GMAPS_TZ
        return None
    else:
        _tz_cache[key] = None
    return _tz_cache[key]


def _route_zones(raw: dict) -> tuple[str | None, str | None]:
    """(origin zone, destination zone) of the first route: from its vehicles for transit
    (free), else by looking up the leg endpoints."""
    routes = raw.get("routes") or []
    if not routes:
        return None, None
    r = routes[0]
    legs = r.get("legs") or []
    td = [s["transitDetails"] for leg in legs for s in leg.get("steps", []) if s.get("transitDetails")]
    otz = ((td[0].get("localizedValues") or {}).get("departureTime") or {}).get("timeZone") if td else None
    dtz = ((td[-1].get("localizedValues") or {}).get("arrivalTime") or {}).get("timeZone") if td else None

    def at(loc):
        ll = (loc or {}).get("latLng")
        return tz_at(ll["latitude"], ll["longitude"]) if ll else None
    if not otz and legs:
        otz = at(legs[0].get("startLocation"))
    if not dtz and legs:
        dtz = at(legs[-1].get("endLocation"))
    return otz, dtz or otz


def _differs(z: str | None, home: str, when_str: Any) -> bool:
    if not z or not is_wall_time(when_str):
        return False
    t = parse_time(when_str, home) or datetime.now(timezone.utc)
    return t.astimezone(S.zone(z)).utcoffset() != t.astimezone(S.zone(home)).utcoffset()


def _redo_zones(departure_time, arrival_time, otz, dtz, home) -> tuple[str | None, str | None, str | None]:
    """If wall-clock input was read in the wrong zone, return the zones to re-read it in."""
    if _differs(otz, home, departure_time):
        return otz, None, f"departure time read as local time at the origin ({otz})"
    if _differs(dtz, home, arrival_time):
        return None, dtz, f"arrival time read as local time at the destination ({dtz})"
    return None, None, None


def compute_routes(origin, destination, *, intermediates=None, travel_mode="TRANSIT",
                   departure_time=None, arrival_time=None, alternatives=True, max_routes=None,
                   detail="brief", format="text", fields=None, dry_run=False, **opts):
    """Route A->B.  detail: brief (default; transit legs always included) | steps (turn-by-turn).
    format: text | json | raw.  Other options: see build_route_request."""
    _check_format(format)
    if detail not in DETAILS:
        raise ValueError(f"detail must be one of {DETAILS}")
    now = datetime.now(timezone.utc)
    body, notes = build_route_request(
        origin, destination, intermediates=intermediates, travel_mode=travel_mode,
        departure_time=departure_time, arrival_time=arrival_time, alternatives=alternatives,
        **opts)
    mode = body["travelMode"]
    mask = fields or (FULL_ROUTE_FIELDS if format == "raw" else _route_mask(mode, detail, body))
    if dry_run:
        return {"url": ROUTES_URL, "field_mask": mask, "body": body, "notes": notes}
    raw, fixed = _send_route(body, mask)
    tz = default_tz()
    otz, dtz = _route_zones(raw)
    tz_dep, tz_arr, why = _redo_zones(departure_time, arrival_time, otz, dtz, tz)
    if why:  # wall-clock time meant local time there: ask again with the right instant
        body, notes = build_route_request(
            origin, destination, intermediates=intermediates, travel_mode=travel_mode,
            departure_time=departure_time, arrival_time=arrival_time,
            alternatives=alternatives, tz_dep=tz_dep, tz_arr=tz_arr, **opts)
        raw, fixed = _send_route(body, mask)
        otz, dtz = (_route_zones(raw) if raw.get("routes") else (otz, dtz))
        notes.append(why)
    notes += fixed
    if format == "raw":
        return raw
    dep = parse_time(departure_time, tz_dep)
    arr = parse_time(arrival_time, tz_arr)
    anchor = arr or dep or now
    routes, more = _postprocess(raw, mode, tz=tz, now=now, dep=dep, arr=arr,
                                with_steps=detail == "steps",
                                transit_modes=opts.get("transit_modes"), otz=otz, dtz=dtz)
    notes += more
    if max_routes:
        routes = routes[:max_routes]
    res = {"travel_mode": mode, "routes": routes, "notes": notes,
           "maps_url": maps_url(origin, destination, intermediates, mode),
           "_tz": tz,
           "_query": {"origin": _label(origin), "destination": _label(destination),
                      "via": [_label(i) for i in intermediates or []], "now": now,
                      "depart_at": dep, "arrive_by": arr, "anchor": anchor,
                      "tz_o": otz or tz, "tz_d": dtz or otz or tz, "tz_known": bool(otz)}}
    return R.routes_text(res) if format == "text" else _json(res)


def _postprocess(raw: dict, mode: str, *, tz: str, now: datetime, dep, arr, with_steps=False,
                 transit_modes=None, otz=None, dtz=None) -> tuple[list[dict], list[str]]:
    """Summarise, enforce transit modes, sort best-first, explain empty results."""
    notes: list[str] = []
    routes = [S.summarize_route(r, tz=tz, mode=mode, depart_ref=None if arr else (dep or now),
                                arrive_ref=arr, with_steps=with_steps, otz=otz, dtz=dtz)
              for r in raw.get("routes", [])]
    if mode == "TRANSIT" and transit_modes:
        # Google treats allowedTravelModes as a preference and still returns other vehicles
        allowed = {m.upper() for m in transit_modes}
        ok = [r for r in routes if S.uses_only(r, allowed)]
        if len(ok) < len(routes):
            if ok:
                notes.append(f"dropped {len(routes) - len(ok)} option(s) using vehicles other than "
                             f"{'/'.join(sorted(allowed))}")
                routes = ok
            else:
                notes.append(f"no option uses only {'/'.join(sorted(allowed))}; showing what "
                             "Google returned")
    if mode != "TRANSIT":  # Google sometimes returns the same route twice
        seen, uniq = set(), []
        for r in routes:
            k = (round((r.get("duration_s") or 0) / 60), round((r.get("distance_m") or 0) / 100))
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        routes = uniq
    if routes and not otz:
        notes.append(f"origin time zone unknown (Time Zone API unavailable); clock times are {tz}")
    if mode == "TRANSIT":
        # API order is not by arrival; sort so option 1 is the best one
        routes.sort(key=(lambda r: -(r.get("leave") or now).timestamp()) if arr else
                    (lambda r: _by_arrival(r, now)))
    if not routes:
        geo = S.geocoding_problems(raw)
        notes += geo
        if geo:
            notes.append("tip: make the place specific (add the city, e.g. 'Hauptbahnhof, Bonn') "
                         "or find it with search_places and pass 'place_id:...'")
        elif mode == "TRANSIT" and (arr or dep or now) > now + timedelta(days=30):
            notes.append("no transit data that far ahead; timetables are usually only published "
                         "a few weeks in advance")
        elif raw.get("geocodingResults", {}).get("origin", {}).get("placeId") and \
                raw["geocodingResults"]["origin"].get("placeId") == \
                raw["geocodingResults"].get("destination", {}).get("placeId"):
            notes.append("origin and destination resolve to the same place")
        elif mode == "TRANSIT":
            notes.append("no transit connection found for that time (no service, or out of the "
                         "covered area); try another time or compare_modes")
        elif mode == "TWO_WHEELER":
            notes.append("TWO_WHEELER routing is only offered in some countries (not most of "
                         "Europe); use DRIVE")
        elif mode == "BICYCLE":
            notes.append("no bicycle route: Google may not offer cycling directions here, or no "
                         "cyclable connection exists (islands, motorway-only links)")
        else:
            notes.append(f"no {mode} route between these places")
    return routes, notes


def _by_arrival(r: dict, now: datetime) -> tuple:
    """Earliest arrival first; on ties, the later (shorter) departure first."""
    far = now + timedelta(days=9)
    return ((r.get("arrive") or far).timestamp(), -(r.get("leave") or now).timestamp())


def _json(res: dict) -> dict:
    tz = res.get("_tz", default_tz())
    out = S.finalize({k: v for k, v in res.items() if not k.startswith("_")}, tz)
    if "_query" in res:
        q = res["_query"]
        q = {k: v for k, v in q.items()
             if k not in ("anchor", "tz_o", "tz_d", "tz_known") and v not in (None, [])}
        out["query"] = S.finalize(q, res["_query"].get("tz_o") or tz)
        for k in ("tz_o", "tz_d"):
            if res["_query"].get(k):
                out["query"]["origin_tz" if k == "tz_o" else "destination_tz"] = res["_query"][k]
    return out


def compare_modes(origin, destination, *, modes=DEFAULT_COMPARE,
                  departure_time=None, arrival_time=None, format="text", **opts):
    """Same trip in several modes (requests run in parallel).  Ranked by earliest arrival,
    or, with arrival_time, by latest possible departure."""
    _check_format(format)
    modes = list(dict.fromkeys(norm_mode(m) for m in modes))
    now = datetime.now(timezone.utc)
    tz = default_tz()
    notes: list[str] = []

    def one(m: str, tz_dep, tz_arr):
        body, n = build_route_request(origin, destination, travel_mode=m, alternatives=m == "TRANSIT",
                                      departure_time=departure_time,
                                      arrival_time=arrival_time if m == "TRANSIT" else None,
                                      tz_dep=tz_dep, tz_arr=tz_arr, **opts)
        raw, fixed = _send_route(body, _route_mask(m, "brief", body))
        return raw, n + fixed

    def run_all(tz_dep=None, tz_arr=None) -> dict:
        out = {}
        with ThreadPoolExecutor(max_workers=len(modes)) as ex:
            futs = {m: ex.submit(one, m, tz_dep, tz_arr) for m in modes}
            for m, f in futs.items():
                try:
                    out[m] = f.result()
                except (GoogleMapsError, ValueError) as e:
                    out[m] = e
        return out

    got = run_all()
    # zones: prefer the transit result (free), else any mode's leg endpoints
    order = sorted(got, key=lambda m: m != "TRANSIT")
    otz = dtz = None
    for m in order:
        if not isinstance(got[m], Exception) and got[m][0].get("routes"):
            otz, dtz = _route_zones(got[m][0])
            if otz:
                break
    tz_dep, tz_arr, why = _redo_zones(departure_time, arrival_time, otz, dtz, tz)
    if why:
        got = run_all(tz_dep, tz_arr)
        notes.append(why)
    dep = parse_time(departure_time, tz_dep)
    arr = parse_time(arrival_time, tz_arr)
    if format == "raw":
        return {m: (v if isinstance(v, Exception) else v[0]) for m, v in got.items()}
    results: dict = {}
    for m in modes:
        v = got[m]
        if isinstance(v, Exception):
            results[m] = {"error": str(v)}
            continue
        raw, n = v
        rs, more = _postprocess(raw, m, tz=tz, now=now, dep=dep, arr=arr,
                                transit_modes=opts.get("transit_modes"), otz=otz, dtz=dtz)
        results[m] = {"best": rs[0] if rs else None,
                      "maps_url": maps_url(origin, destination, None, m)}
        for x in n + more:  # the same waypoint repair happens in every mode: say it once
            t = x if "resolved via Places" in x else f"{m}: {x}"
            if t not in notes:
                notes.append(t)
    if arr and any(m in ("DRIVE", "TWO_WHEELER") for m in modes):
        notes.append("DRIVE leave time = arrival minus current traffic estimate (approximate)")

    def score(m):
        b = results[m].get("best") or {}
        if arr:
            return -(b["leave"].timestamp()) if b.get("leave") else None
        return b["arrive"].timestamp() if b.get("arrive") else b.get("duration_s")

    ranking = sorted((m for m in results if score(m) is not None), key=score)
    res = {"ranking": ranking, "modes": results, "notes": notes, "_tz": tz,
           "_query": {"origin": _label(origin), "destination": _label(destination), "now": now,
                      "depart_at": dep, "arrive_by": arr, "anchor": arr or dep or now,
                      "tz_o": otz or tz, "tz_d": dtz or otz or tz, "tz_known": bool(otz)}}
    return R.compare_text(res) if format == "text" else _json(res)


# --------------------------------------------------------------------------- matrix
def compute_route_matrix(origins: list, destinations: list, *, travel_mode="TRANSIT",
                         departure_time=None, arrival_time=None, transit_modes=None,
                         transit_routing=None, routing_preference=None, traffic_model=None,
                         avoid=None, units="METRIC", language=None, region=None,
                         extra_computations=None, extra_request=None, fields=None,
                         format="text", dry_run=False, resolve_addresses=True):
    _check_format(format)
    mode = norm_mode(travel_mode)
    dep, arr = parse_time(departure_time), parse_time(arrival_time)
    if dep and arr:
        raise ValueError("departure_time and arrival_time are mutually exclusive")
    if arr and mode != "TRANSIT":
        raise ValueError("arrival_time is only supported for TRANSIT")
    notes: list[str] = []
    n = len(origins) * len(destinations)
    rp = (routing_preference or "TRAFFIC_AWARE").upper() if mode in ("DRIVE", "TWO_WHEELER") else None
    limit = 100 if mode == "TRANSIT" or rp == "TRAFFIC_AWARE_OPTIMAL" else 625
    if n > limit:
        raise ValueError(f"{len(origins)}x{len(destinations)}={n} elements exceeds the "
                         f"{limit}-element limit for this request")
    body: dict = {
        "origins": [{"waypoint": waypoint(o)} for o in origins],
        "destinations": [{"waypoint": waypoint(d)} for d in destinations],
        "travelMode": mode, "units": units.upper(),
    }
    # The matrix endpoint geocodes addresses like computeRoutes does ('Köln Hbf' -> city of
    # Köln) but never reports partial matches, so resolve addresses with Places up front.
    if not dry_run and resolve_addresses:
        items = [e["waypoint"] for e in body["origins"] + body["destinations"]
                 if "address" in e["waypoint"]]
        texts = list(dict.fromkeys(w["address"] for w in items))
        if texts:
            with ThreadPoolExecutor(max_workers=min(8, len(texts))) as ex:
                found = dict(zip(texts, ex.map(lambda t: resolve_place(t, language), texts)))
            for w in items:
                p = found.get(w["address"])
                if p:
                    w.pop("address")
                    w["placeId"] = p["id"]
            notes.append("resolved: " + "; ".join(
                f"'{t}' → {found[t]['name']}" if found[t] else f"'{t}' → (no match, used as address)"
                for t in texts))
    named = sum(1 for w in body["origins"] + body["destinations"]
                if "address" in w["waypoint"] or "placeId" in w["waypoint"])
    if named > 50:
        raise ValueError(f"{named} origins+destinations given as address/place_id; the API "
                         "allows 50 (use lat,lng for the rest)")
    mods = _route_modifiers(_avoid_for(mode, avoid, notes))
    if mods:
        for o in body["origins"]:
            o["routeModifiers"] = mods
    if rp:
        body["routingPreference"] = rp
        if traffic_model:
            body["trafficModel"] = traffic_model.upper()
    elif routing_preference or traffic_model:
        notes.append(f"routing_preference/traffic_model ignored for {mode}")
    if mode == "TRANSIT":
        tp = _transit_prefs(transit_modes, transit_routing)
        if tp:
            body["transitPreferences"] = tp
    if dep and (mode == "TRANSIT" or dep > datetime.now(timezone.utc) + timedelta(seconds=60)):
        body["departureTime"] = _rfc3339(dep)
    if arr:
        body["arrivalTime"] = _rfc3339(arr)
    lang = language or default_lang()
    if lang:
        body["languageCode"] = lang
    if region:
        body["regionCode"] = region
    if extra_computations:
        body["extraComputations"] = [e.upper() for e in extra_computations]
    if extra_request:
        _deep_merge(body, extra_request)
    mask = fields or (FULL_MATRIX_FIELDS if format == "raw" else MATRIX_FIELDS)
    if dry_run:
        return {"url": MATRIX_URL, "field_mask": mask, "body": body, "notes": notes}
    raw = _send("POST", MATRIX_URL, json=body,
                headers={"X-Goog-FieldMask": mask, "Content-Type": "application/json"})
    if format == "raw":
        return raw
    if mode == "TRANSIT":
        notes.append("TRANSIT matrix durations exclude the initial wait for the vehicle")
    if is_wall_time(departure_time) or is_wall_time(arrival_time):
        notes.append(f"wall-clock time read in {default_tz()}; give an ISO offset "
                     "(e.g. 2026-11-04T08:30+00:00) for places in other time zones")
    res = {"travel_mode": mode, "matrix": S.summarize_matrix(raw, origins, destinations),
           "notes": notes, "_origins": [_label(o) for o in origins],
           "_destinations": [_label(d) for d in destinations]}
    if format == "text":
        return R.matrix_text(res)
    out = _json(res)
    out["origins"], out["destinations"] = res["_origins"], res["_destinations"]
    return out


# --------------------------------------------------------------------------- geocode
def geocode(query: str | None = None, *, latlng: str | None = None, place_id: str | None = None,
            language=None, region=None, components=None, format="text", dry_run=False):
    """Forward (query), reverse (latlng='lat,lng') or place_id lookup (Geocoding API)."""
    _check_format(format)
    if sum(x is not None for x in (query, latlng, place_id)) != 1:
        raise ValueError("give exactly one of query, latlng, place_id")
    params: dict = {}
    if query is not None:
        params["address"] = query
    if latlng is not None:
        params["latlng"] = latlng
    if place_id is not None:
        params["place_id"] = place_id
    lang = language or default_lang()
    if lang:
        params["language"] = lang
    if region:
        params["region"] = region
    if components:
        params["components"] = components
    if dry_run:
        return {"url": GEOCODE_URL, "params": params}
    raw = _send("GET", GEOCODE_URL, params=params, key_in="param")
    st = raw.get("status")
    if st not in ("OK", "ZERO_RESULTS"):
        raise GoogleMapsError(f"Geocoding {st}: {raw.get('error_message', '')}", 200, raw)
    if format == "raw":
        return raw
    results = [{"address": r.get("formatted_address"),
                "lat": r["geometry"]["location"]["lat"], "lng": r["geometry"]["location"]["lng"],
                "place_id": r.get("place_id"), "types": r.get("types"),
                "precision": r["geometry"].get("location_type"),
                **({"partial_match": True} if r.get("partial_match") else {})}
               for r in raw.get("results", [])[:5]]
    res = {"results": results}
    return R.geocode_text(res) if format == "text" else res


# --------------------------------------------------------------------------- places
def search_places(text: str, *, near: str | None = None, radius_m: float = 3000,
                  max_results: int = 8, included_type: str | None = None,
                  open_now: bool | None = None, min_rating: float | None = None,
                  rank_by: str | None = None, language=None, region=None, detailed=False,
                  fields=None, extra_request=None, format="text", dry_run=False):
    """Places API (New) Text Search.  near='lat,lng' biases results to a circle (radius_m)
    and adds straight-line distance.  detailed adds rating/hours/phone/web (Enterprise SKU)."""
    _check_format(format)
    body: dict = {"textQuery": text, "pageSize": max(1, min(int(max_results), 20))}
    center = None
    if near:
        m = _LATLNG.match(near)
        if not m:
            raise ValueError("near must be 'lat,lng' (geocode an address first)")
        center = (float(m.group(1)), float(m.group(2)))
        body["locationBias"] = {"circle": {"center": {"latitude": center[0], "longitude": center[1]},
                                           "radius": float(min(radius_m, 50000))}}
    if included_type:
        body["includedType"] = included_type
    if open_now is not None:
        body["openNow"] = open_now
    if min_rating is not None:
        body["minRating"] = min_rating
    if rank_by:
        body["rankPreference"] = rank_by.upper()
    lang = language or default_lang()
    if lang:
        body["languageCode"] = lang
    if region:
        body["regionCode"] = region
    if extra_request:
        _deep_merge(body, extra_request)
    mask = fields or (PLACES_DETAILED_FIELDS if detailed else PLACES_BASIC_FIELDS)
    if dry_run:
        return {"url": PLACES_SEARCH_URL, "field_mask": mask, "body": body}
    raw = _send("POST", PLACES_SEARCH_URL, json=body,
                headers={"X-Goog-FieldMask": mask, "Content-Type": "application/json"})
    if format == "raw":
        return raw
    res = {"query": text, "places": [S.summarize_place(p, center) for p in raw.get("places", [])],
           **({"open_now": True} if open_now else {})}
    return R.places_text(res, default_tz()) if format == "text" else res
