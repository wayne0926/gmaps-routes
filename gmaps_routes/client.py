"""Thin, complete client for Google Maps Platform routing-related REST APIs.

Covers:
  * Routes API  : computeRoutes, computeRouteMatrix (all travel modes, full field mask)
  * Geocoding   : address <-> lat/lng <-> place_id
  * Places (New): searchText

The API key is read from the environment (GOOGLE_MAPS_API_KEY) and is never
logged or echoed.  Every function accepts ``dry_run=True`` to return the exact
request that would be sent (without the key) instead of calling the network.
"""
from __future__ import annotations

import copy
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from . import summary as S

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

TRAVEL_MODES = ("TRANSIT", "BICYCLE", "WALK", "DRIVE", "TWO_WHEELER")
TRANSIT_MODES = ("BUS", "SUBWAY", "TRAIN", "LIGHT_RAIL", "RAIL")
TRANSIT_ROUTING = ("LESS_WALKING", "FEWER_TRANSFERS")

# Everything computeRoutes can return.  Parent paths (routes.legs, ...) include
# all of their children, so new sub-fields are picked up automatically.
FULL_ROUTE_FIELDS = ",".join([
    "routes.routeLabels", "routes.legs", "routes.distanceMeters", "routes.duration",
    "routes.staticDuration", "routes.polyline", "routes.description", "routes.warnings",
    "routes.viewport", "routes.travelAdvisory", "routes.optimizedIntermediateWaypointIndex",
    "routes.localizedValues", "routes.routeToken", "routes.polylineDetails",
    "geocodingResults",
])
FULL_MATRIX_FIELDS = ",".join([
    "originIndex", "destinationIndex", "status", "condition", "distanceMeters", "duration",
    "staticDuration", "travelAdvisory", "fallbackInfo", "localizedValues",
])
PLACES_BASIC_FIELDS = ",".join(f"places.{f}" for f in (
    "id", "displayName", "formattedAddress", "shortFormattedAddress", "location", "types",
    "primaryType", "googleMapsUri"))
PLACES_DETAILED_FIELDS = PLACES_BASIC_FIELDS + "," + ",".join(f"places.{f}" for f in (
    "rating", "userRatingCount", "regularOpeningHours", "currentOpeningHours", "websiteUri",
    "nationalPhoneNumber", "priceLevel", "businessStatus"))


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
            "No API key: set the GOOGLE_MAPS_API_KEY environment variable "
            "(Routes API, plus Geocoding API / Places API (New) if you use those tools, "
            "must be enabled for the key's project).")
    return key


def default_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("GMAPS_TZ", "Europe/Berlin"))


# --------------------------------------------------------------------------- inputs
_LATLNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_REL = re.compile(r"^\+(\d+)\s*([mhd])$")


def parse_time(value: str | datetime | None) -> str | None:
    """-> RFC3339 UTC string.  None/'now' -> None (API uses request time).

    Accepts ISO-8601 with offset, naive ISO (interpreted in GMAPS_TZ, default
    Europe/Berlin), or relative '+45m' / '+2h' / '+1d'.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        v = str(value).strip()
        if not v or v.lower() == "now":
            return None
        m = _REL.match(v)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            dt = datetime.now(timezone.utc) + timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})
        else:
            try:
                dt = datetime.fromisoformat(v)
            except ValueError as e:
                raise ValueError(f"Unparseable time {value!r}; use ISO-8601 like 2026-11-04T08:30 or +45m") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz())
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def waypoint(spec: Any, *, via: bool = False) -> dict:
    """Build a Waypoint from a flexible spec.

    str : 'Bonn Hbf' (address/name) | '50.7323,7.0969' | 'place_id:ChIJ...' | 'via:<any of these>'
    dict: {"lat":..,"lng":..,"heading":..} | {"place_id":..} | {"address":..} | full API Waypoint
          (extra keys: via, vehicle_stopover, side_of_road)
    """
    if isinstance(spec, dict):
        d = dict(spec)
        out: dict = {}
        if "waypoint" in d:  # already wrapped (matrix) -> unwrap
            d = d["waypoint"]
        if any(k in d for k in ("location", "placeId", "address")):
            out = copy.deepcopy(d)
        elif "lat" in d and "lng" in d:
            loc: dict = {"latLng": {"latitude": float(d["lat"]), "longitude": float(d["lng"])}}
            if d.get("heading") is not None:
                loc["heading"] = int(d["heading"])
            out["location"] = loc
        elif "place_id" in d:
            out["placeId"] = d["place_id"]
        elif "address" in d:
            out["address"] = d["address"]
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
    out = {}
    m = _LATLNG.match(s)
    if m:
        out["location"] = {"latLng": {"latitude": float(m.group(1)), "longitude": float(m.group(2))}}
    elif s.lower().startswith(("place_id:", "placeid:")):
        out["placeId"] = s.split(":", 1)[1].strip()
    elif s:
        out["address"] = s
    else:
        raise ValueError("Empty waypoint")
    if via:
        out["via"] = True
    return out


def _route_modifiers(avoid: list[str] | None, extra: dict | None = None) -> dict | None:
    avoid = [a.lower() for a in (avoid or [])]
    mods: dict = {}
    for flag, key in (("tolls", "avoidTolls"), ("highways", "avoidHighways"),
                      ("ferries", "avoidFerries"), ("indoor", "avoidIndoor"),
                      ("tunnels", "avoidTunnels")):
        if flag in avoid:
            mods[key] = True
    unknown = set(avoid) - {"tolls", "highways", "ferries", "indoor", "tunnels"}
    if unknown:
        raise ValueError(f"Unknown avoid option(s): {sorted(unknown)}")
    if extra:
        mods.update(extra)
    return mods or None


def _transit_prefs(modes: list[str] | None, routing: str | None) -> dict | None:
    prefs: dict = {}
    if modes:
        bad = [m for m in (x.upper() for x in modes) if m not in TRANSIT_MODES]
        if bad:
            raise ValueError(f"Unknown transit mode(s) {bad}; allowed: {TRANSIT_MODES}")
        prefs["allowedTravelModes"] = [m.upper() for m in modes]
    if routing:
        if routing.upper() not in TRANSIT_ROUTING:
            raise ValueError(f"transit routing must be one of {TRANSIT_ROUTING}")
        prefs["routingPreference"] = routing.upper()
    return prefs or None


def _deep_merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# --------------------------------------------------------------------------- http
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
            r = requests.request(method, url, headers=headers, json=json, params=params, timeout=45)
        except requests.RequestException as e:  # may contain the URL (and key) -> scrub
            last = GoogleMapsError(_scrub(f"{type(e).__name__}: {e}", key))
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 503) and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw_text": r.text[:2000]}
        if r.status_code >= 400:
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise GoogleMapsError(_scrub(f"HTTP {r.status_code} {err.get('status', '') if isinstance(err, dict) else ''}: {msg}", key),
                                  r.status_code, payload)
        return payload
    raise last or GoogleMapsError("request failed")


# --------------------------------------------------------------------------- routes
def _maps_url(origin: Any, destination: Any, intermediates: list | None, mode: str) -> str:
    def txt(s: Any) -> tuple[str, str | None]:
        if isinstance(s, dict):
            if "place_id" in s:
                return s["place_id"], s["place_id"]
            if "lat" in s:
                return f"{s['lat']},{s['lng']}", None
            return str(s.get("address", "")), None
        s = str(s).strip()
        if s.lower().startswith("via:"):
            s = s[4:].strip()
        if s.lower().startswith(("place_id:", "placeid:")):
            pid = s.split(":", 1)[1].strip()
            return pid, pid
        return s, None

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
                        traffic_model=None, avoid=None, route_modifiers=None, units="METRIC",
                        language=None, region=None, optimize_waypoint_order=False,
                        polyline_quality="HIGH_QUALITY", geojson=False, extra_computations=None,
                        reference_routes=None, extra_request=None) -> tuple[dict, list[str]]:
    mode = travel_mode.upper()
    if mode not in TRAVEL_MODES:
        raise ValueError(f"travel_mode must be one of {TRAVEL_MODES}")
    notes: list[str] = []
    dep, arr = parse_time(departure_time), parse_time(arrival_time)
    if dep and arr:
        raise ValueError("departure_time and arrival_time are mutually exclusive")
    if arr and mode != "TRANSIT":
        raise ValueError("arrival_time is only supported for TRANSIT; use departure_time for other modes")

    body: dict = {
        "origin": waypoint(origin),
        "destination": waypoint(destination),
        "travelMode": mode,
        "polylineQuality": polyline_quality,
        "polylineEncoding": "GEO_JSON_LINESTRING" if geojson else "ENCODED_POLYLINE",
        "units": units.upper(),
    }
    if intermediates:
        body["intermediates"] = [waypoint(i) for i in intermediates]
        if alternatives:
            notes.append("alternatives disabled: the API returns none when intermediate waypoints are used")
        alternatives = False
        if mode == "TRANSIT":
            notes.append("TRANSIT with intermediate waypoints is not supported by the API and will likely be rejected")
    if optimize_waypoint_order:
        if not intermediates:
            raise ValueError("optimize_waypoint_order needs at least one intermediate")
        if any(w.get("via") for w in body["intermediates"]):
            raise ValueError("optimize_waypoint_order cannot be combined with 'via' intermediates")
        body["optimizeWaypointOrder"] = True
    body["computeAlternativeRoutes"] = bool(alternatives)

    if mode in ("DRIVE", "TWO_WHEELER"):
        body["routingPreference"] = (routing_preference or "TRAFFIC_AWARE").upper()
        if traffic_model:
            body["trafficModel"] = traffic_model.upper()
    elif routing_preference or traffic_model:
        notes.append(f"routing_preference/traffic_model ignored: only valid for DRIVE/TWO_WHEELER (mode is {mode})")

    if mode == "TRANSIT":
        tp = _transit_prefs(transit_modes, transit_routing)
        if tp:
            body["transitPreferences"] = tp
    elif transit_modes or transit_routing:
        notes.append(f"transit preferences ignored: mode is {mode}")

    mods = _route_modifiers(avoid, route_modifiers)
    if mods:
        body["routeModifiers"] = mods
    if dep:
        body["departureTime"] = dep
    if arr:
        body["arrivalTime"] = arr
    if language:
        body["languageCode"] = language
    if region:
        body["regionCode"] = region
    if extra_computations:
        body["extraComputations"] = [e.upper() for e in extra_computations]
    if reference_routes:
        body["requestedReferenceRoutes"] = [r.upper() for r in reference_routes]
    if extra_request:
        _deep_merge(body, extra_request)
    return body, notes


def compute_routes(origin, destination, *, intermediates=None, travel_mode="TRANSIT",
                   departure_time=None, arrival_time=None, alternatives=True,
                   transit_modes=None, transit_routing=None, routing_preference=None,
                   traffic_model=None, avoid=None, route_modifiers=None, units="METRIC",
                   language=None, region=None, optimize_waypoint_order=False,
                   polyline_quality="HIGH_QUALITY", geojson=False, extra_computations=None,
                   reference_routes=None, extra_request=None, fields=None,
                   output="both", dry_run=False) -> dict:
    """Compute routes.  output: 'both' (summary + raw), 'summary', or 'raw'."""
    if output not in ("both", "summary", "raw"):
        raise ValueError("output must be both|summary|raw")
    body, notes = build_route_request(
        origin, destination, intermediates=intermediates, travel_mode=travel_mode,
        departure_time=departure_time, arrival_time=arrival_time, alternatives=alternatives,
        transit_modes=transit_modes, transit_routing=transit_routing,
        routing_preference=routing_preference, traffic_model=traffic_model, avoid=avoid,
        route_modifiers=route_modifiers, units=units, language=language, region=region,
        optimize_waypoint_order=optimize_waypoint_order, polyline_quality=polyline_quality,
        geojson=geojson, extra_computations=extra_computations, reference_routes=reference_routes,
        extra_request=extra_request)
    mask = fields or FULL_ROUTE_FIELDS
    if dry_run:
        return {"url": ROUTES_URL, "field_mask": mask, "body": body, "notes": notes}
    raw = _send("POST", ROUTES_URL, json=body,
                headers={"X-Goog-FieldMask": mask, "Content-Type": "application/json"})
    mode = body["travelMode"]
    out: dict = {"travel_mode": mode, "notes": notes,
                 "maps_url": _maps_url(origin, destination, intermediates, mode)}
    if output in ("both", "summary"):
        out["routes"] = S.summarize_routes(raw, default_tz=default_tz().key)
        if mode == "TRANSIT" and not body.get("arrivalTime"):
            # API 'duration' excludes waiting for the vehicle; add request-time -> final arrival
            start = (datetime.fromisoformat(body["departureTime"].replace("Z", "+00:00"))
                     if body.get("departureTime") else datetime.now(timezone.utc))
            for r in out["routes"]:
                if r.get("last_arrival_time"):
                    end = datetime.fromisoformat(r["last_arrival_time"].replace("Z", "+00:00"))
                    secs = max(0.0, (end - start).total_seconds())
                    r["door_to_door_s"], r["door_to_door_text"] = secs, S.fmt_duration(secs)
        if not raw.get("routes"):
            out["warning"] = "No route found for this request."
    if output in ("both", "raw"):
        out["raw"] = raw
    return out


def compare_modes(origin, destination, *, modes=("TRANSIT", "BICYCLE", "WALK", "DRIVE"),
                  departure_time=None, arrival_time=None, output="summary", **kwargs) -> dict:
    """Same origin/destination across several modes; one best-route headline per mode."""
    results: dict = {}
    notes: list[str] = []
    for m in modes:
        m = m.upper()
        try:
            r = compute_routes(
                origin, destination, travel_mode=m, alternatives=False, output=output,
                departure_time=departure_time,
                arrival_time=arrival_time if m == "TRANSIT" else None, **kwargs)
            if arrival_time and m != "TRANSIT":
                notes.append(f"{m}: arrival_time only applies to TRANSIT; computed for departure now")
            results[m] = {"best": (r.get("routes") or [None])[0], "maps_url": r["maps_url"],
                          **({"raw": r["raw"]} if "raw" in r else {}), **({"notes": r["notes"]} if r["notes"] else {})}
        except (GoogleMapsError, ValueError) as e:
            results[m] = {"error": str(e)}
    def _total(b: dict) -> float | None:  # transit: include waiting time
        return b.get("door_to_door_s", b.get("duration_s"))

    ranked = sorted(((m, _total(v["best"])) for m, v in results.items()
                     if v.get("best") and _total(v["best"]) is not None), key=lambda x: x[1])
    return {"fastest_first": [m for m, _ in ranked], "modes": results, "notes": notes}


# --------------------------------------------------------------------------- matrix
def compute_route_matrix(origins: list, destinations: list, *, travel_mode="TRANSIT",
                         departure_time=None, arrival_time=None, transit_modes=None,
                         transit_routing=None, routing_preference=None, traffic_model=None,
                         avoid=None, units="METRIC", language=None, region=None,
                         extra_computations=None, extra_request=None, fields=None,
                         output="both", dry_run=False) -> dict:
    mode = travel_mode.upper()
    if mode not in TRAVEL_MODES:
        raise ValueError(f"travel_mode must be one of {TRAVEL_MODES}")
    dep, arr = parse_time(departure_time), parse_time(arrival_time)
    if dep and arr:
        raise ValueError("departure_time and arrival_time are mutually exclusive")
    if arr and mode != "TRANSIT":
        raise ValueError("arrival_time is only supported for TRANSIT")
    notes: list[str] = []
    n = len(origins) * len(destinations)
    limit = 100 if mode == "TRANSIT" else 625
    if n > limit:
        raise ValueError(f"{len(origins)}x{len(destinations)}={n} elements exceeds the {limit}-element limit for {mode}")
    body: dict = {
        "origins": [{"waypoint": waypoint(o)} for o in origins],
        "destinations": [{"waypoint": waypoint(d)} for d in destinations],
        "travelMode": mode, "units": units.upper(),
    }
    mods = _route_modifiers(avoid)
    if mods:
        for o in body["origins"]:
            o["routeModifiers"] = mods
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
    if dep:
        body["departureTime"] = dep
    if arr:
        body["arrivalTime"] = arr
    if language:
        body["languageCode"] = language
    if region:
        body["regionCode"] = region
    if extra_computations:
        body["extraComputations"] = [e.upper() for e in extra_computations]
    if extra_request:
        _deep_merge(body, extra_request)
    mask = fields or FULL_MATRIX_FIELDS
    if dry_run:
        return {"url": MATRIX_URL, "field_mask": mask, "body": body, "notes": notes}
    raw = _send("POST", MATRIX_URL, json=body,
                headers={"X-Goog-FieldMask": mask, "Content-Type": "application/json"})
    out: dict = {"travel_mode": mode, "notes": notes}
    if output in ("both", "summary"):
        out["matrix"] = S.summarize_matrix(raw, origins, destinations)
    if output in ("both", "raw"):
        out["raw"] = raw
    return out


# --------------------------------------------------------------------------- geocode
def geocode(query: str | None = None, *, latlng: str | None = None, place_id: str | None = None,
            language=None, region=None, components=None, dry_run=False) -> dict:
    """Forward (query), reverse (latlng='lat,lng') or place_id lookup."""
    if sum(x is not None for x in (query, latlng, place_id)) != 1:
        raise ValueError("give exactly one of query, latlng, place_id")
    params: dict = {}
    if query is not None:
        params["address"] = query
    if latlng is not None:
        params["latlng"] = latlng
    if place_id is not None:
        params["place_id"] = place_id
    if language:
        params["language"] = language
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
    return raw


# --------------------------------------------------------------------------- places
def search_places(text: str, *, near: str | None = None, radius_m: float = 5000,
                  max_results: int = 10, included_type: str | None = None,
                  open_now: bool | None = None, min_rating: float | None = None,
                  language=None, region=None, detailed=False, fields=None,
                  extra_request=None, dry_run=False) -> dict:
    """Places API (New) Text Search.  `near` = 'lat,lng' biases results (radius_m)."""
    body: dict = {"textQuery": text, "pageSize": max(1, min(int(max_results), 20))}
    if near:
        m = _LATLNG.match(near)
        if not m:
            raise ValueError("near must be 'lat,lng'")
        body["locationBias"] = {"circle": {"center": {"latitude": float(m.group(1)),
                                                      "longitude": float(m.group(2))},
                                           "radius": float(radius_m)}}
    if included_type:
        body["includedType"] = included_type
    if open_now is not None:
        body["openNow"] = open_now
    if min_rating is not None:
        body["minRating"] = min_rating
    if language:
        body["languageCode"] = language
    if region:
        body["regionCode"] = region
    if extra_request:
        _deep_merge(body, extra_request)
    mask = fields or (PLACES_DETAILED_FIELDS if detailed else PLACES_BASIC_FIELDS)
    if dry_run:
        return {"url": PLACES_SEARCH_URL, "field_mask": mask, "body": body}
    return _send("POST", PLACES_SEARCH_URL, json=body,
                 headers={"X-Goog-FieldMask": mask, "Content-Type": "application/json"})
