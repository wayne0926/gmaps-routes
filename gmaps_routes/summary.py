"""Normalise raw Routes / Places API JSON into compact dicts (the shape tools return as JSON)."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


# --------------------------------------------------------------------------- primitives
def parse_duration(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).rstrip("s"))


def fmt_duration(sec: float | None) -> str | None:
    if sec is None:
        return None
    m = round(sec / 60)
    if m < 1:
        return "<1 min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def fmt_distance(m: float | None) -> str | None:
    if m is None:
        return None
    return f"{round(m)} m" if m < 1000 else f"{m / 1000:.1f} km"


def parse_ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


_ALIASES = {"Europe/Kiev": "Europe/Kyiv", "Europe/Uzhgorod": "Europe/Kyiv",
            "Europe/Zaporozhye": "Europe/Kyiv", "Atlantic/Faeroe": "Atlantic/Faroe",
            "Asia/Istanbul": "Europe/Istanbul", "Europe/Nicosia": "Asia/Nicosia"}


def zone(tz: str | None, fallback: str = "Europe/Berlin") -> ZoneInfo:
    """ZoneInfo that never raises: Google still returns legacy names (e.g. 'Europe/Kiev')
    that some tz databases no longer ship."""
    for name in (tz, _ALIASES.get(tz or ""), fallback, "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except Exception:
            continue
    return ZoneInfo("UTC")


def local_iso(dt: datetime | None, tz: str) -> str | None:
    return dt.astimezone(zone(tz)).isoformat(timespec="minutes") if dt else None


def hhmm(dt: datetime | None, tz: str, ref: datetime | None = None, with_date: bool = False) -> str | None:
    """'10:50'; when the local date differs from ref's: 'Wed 08:30' (with_date: 'Wed 04.11. 08:30')."""
    if not dt:
        return None
    z = zone(tz)
    d = dt.astimezone(z)
    if ref is not None and ref.astimezone(z).date() != d.date():
        return d.strftime("%a %d.%m. %H:%M")  # weekday alone is ambiguous weeks ahead
    return d.strftime("%H:%M")


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def clean(s: str | None) -> str | None:
    """Drop zero-width chars Google puts in stop names; flatten multi-line instructions."""
    if s is None:
        return None
    return re.sub(r"\s*\n\s*", "; ", s.replace("\u200b", "").replace("\u00ad", "")).strip()


def _text(d: Any, *path: str) -> str | None:
    for p in path:
        d = (d or {}).get(p)
    return d.get("text") if isinstance(d, dict) else None


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


# --------------------------------------------------------------------------- routes
def _fare(route: dict) -> str | None:
    f = (route.get("travelAdvisory") or {}).get("transitFare") or {}
    if f.get("units") is None and f.get("nanos") is None:
        return _text(route.get("localizedValues"), "transitFare")
    amount = int(f.get("units", 0)) + f.get("nanos", 0) / 1e9
    return _text(route.get("localizedValues"), "transitFare") or f"{amount:.2f} {f.get('currencyCode', '')}".strip()


VEHICLE_CLASS = {  # Routes API vehicle type -> transitPreferences.allowedTravelModes value
    "BUS": "BUS", "INTERCITY_BUS": "BUS", "TROLLEYBUS": "BUS", "SHARE_TAXI": "BUS",
    "SUBWAY": "SUBWAY", "METRO_RAIL": "SUBWAY",
    "TRAM": "LIGHT_RAIL", "LIGHT_RAIL": "LIGHT_RAIL", "MONORAIL": "LIGHT_RAIL",
    "HEAVY_RAIL": "TRAIN", "COMMUTER_TRAIN": "TRAIN", "HIGH_SPEED_TRAIN": "TRAIN",
    "LONG_DISTANCE_TRAIN": "TRAIN", "RAIL": "TRAIN",
}


def uses_only(route: dict, allowed: set[str]) -> bool:
    """True if every vehicle in a summarised TRANSIT route is in the allowed classes.
    'RAIL' allows TRAIN, SUBWAY and LIGHT_RAIL (as in the API)."""
    if "RAIL" in allowed:
        allowed = allowed | {"TRAIN", "SUBWAY", "LIGHT_RAIL"}
    return all(VEHICLE_CLASS.get(s.get("vehicle", ""), "OTHER") in allowed
               for s in route.get("segments", []) if s["type"] == "transit")


def _transit_segment(step: dict, tz: str) -> dict:
    td = step["transitDetails"]
    sd = td.get("stopDetails") or {}
    line = td.get("transitLine") or {}
    veh = line.get("vehicle") or {}
    tlv = td.get("localizedValues") or {}
    short, long_ = line.get("nameShort") or "", line.get("name") or ""
    rail_name = re.match(r"^(S|RB|RE|IC|ICE|EC|IRE|MEX|RS|FEX|TER|IR)\s?\d", short.upper())
    sev = ("SEV" in long_.upper().split() or "ERSATZ" in long_.upper()
           or ((veh.get("type") in ("BUS", "INTERCITY_BUS")) and bool(rail_name)))
    return _drop_none({
        "type": "transit",
        "replacement_bus": True if sev else None,
        "line": line.get("nameShort") or line.get("name"),
        "line_name": line.get("name") if line.get("nameShort") else None,
        "vehicle": veh.get("type"),
        "vehicle_name": _text(veh, "name"),
        "agency": ", ".join(a["name"] for a in line.get("agencies", []) if a.get("name")) or None,
        "headsign": clean(td.get("headsign")),
        "trip": td.get("tripShortText"),
        "from": clean((sd.get("departureStop") or {}).get("name")),
        "to": clean((sd.get("arrivalStop") or {}).get("name")),
        "_dep": parse_ts(sd.get("departureTime")),
        "_arr": parse_ts(sd.get("arrivalTime")),
        "_tz": (tlv.get("departureTime") or {}).get("timeZone") or tz,
        "_atz": (tlv.get("arrivalTime") or {}).get("timeZone")
                or (tlv.get("departureTime") or {}).get("timeZone") or tz,
        "stops": td.get("stopCount"),
        "headway_min": round(parse_duration(td["headway"]) / 60) if td.get("headway") else None,
        "instruction": clean((step.get("navigationInstruction") or {}).get("instructions")),
    })


def _segments(steps: list[dict], tz: str) -> list[dict]:
    """Collapse steps into segments: consecutive non-transit steps of one mode merge into one."""
    segs: list[dict] = []
    for s in steps:
        if s.get("transitDetails"):
            segs.append(_transit_segment(s, tz))
            continue
        mode = (s.get("travelMode") or "WALK").lower()
        dist = s.get("distanceMeters", 0) or 0
        dur = parse_duration(s.get("staticDuration")) or 0
        if segs and segs[-1]["type"] == mode:
            segs[-1]["distance_m"] += dist
            segs[-1]["duration_s"] += dur
        else:
            segs.append({"type": mode, "distance_m": dist, "duration_s": dur})
    return segs


def _step(s: dict) -> dict:
    nav = s.get("navigationInstruction") or {}
    return _drop_none({
        "mode": s.get("travelMode"),
        "instruction": clean(nav.get("instructions")),
        "maneuver": nav.get("maneuver"),
        "distance_m": s.get("distanceMeters"),
        "duration_s": parse_duration(s.get("staticDuration")),
    })


def summarize_route(route: dict, *, tz: str, mode: str, depart_ref: datetime | None,
                    arrive_ref: datetime | None, with_steps: bool = False,
                    otz: str | None = None, dtz: str | None = None) -> dict:
    """Normalise one route.

    depart_ref: requested departure (or request time); used to compute waiting time and, for
    non-transit modes, absolute leave/arrive times.  arrive_ref: requested arrival (TRANSIT only).
    otz/dtz: time zone at origin/destination (leave/arrive are shown there); transit routes
    take them from their first/last vehicle instead.
    """
    dur = parse_duration(route.get("duration"))
    static = parse_duration(route.get("staticDuration"))
    dist = route.get("distanceMeters", 0) or 0
    steps = [s for leg in route.get("legs", []) for s in leg.get("steps", [])]
    out: dict = {
        "duration_s": dur if dur is not None else static,
        "distance_m": dist,
    }
    if static is not None and dur is not None and abs(static - dur) >= 60:
        out["no_traffic_duration_s"] = static
    labels = [l for l in route.get("routeLabels") or []
              if l not in ("DEFAULT_ROUTE", "DEFAULT_ROUTE_ALTERNATE", "ROUTE_LABEL_UNSPECIFIED")]
    if labels:
        out["labels"] = labels
    if mode == "TRANSIT":
        segs = _segments(steps, tz)
        transit = [s for s in segs if s["type"] == "transit"]
        if transit:
            i0 = segs.index(transit[0])
            i1 = segs.index(transit[-1])
            walk_before = sum(s.get("duration_s", 0) for s in segs[:i0])
            walk_after = sum(s.get("duration_s", 0) for s in segs[i1 + 1:])
            leave = transit[0]["_dep"] - timedelta(seconds=walk_before) if transit[0].get("_dep") else None
            arrive = transit[-1]["_arr"] + timedelta(seconds=walk_after) if transit[-1].get("_arr") else None
            # idle time at each transfer = gap between vehicles minus the walk in between
            prev_arr, walked = None, 0.0
            for s in segs:
                if s["type"] == "transit":
                    if prev_arr and s.get("_dep"):
                        s["transfer_wait_s"] = max(0.0, (s["_dep"] - prev_arr).total_seconds() - walked)
                    prev_arr, walked = s.get("_arr"), 0.0
                else:
                    walked += s.get("duration_s", 0)
            out.update({
                "_ltz": transit[0].get("_tz") or otz or tz,
                "_atz": transit[-1].get("_atz") or dtz or otz or tz,
                "leave": leave, "arrive": arrive,
                "transfers": len(transit) - 1,
                "walk_m": sum(s.get("distance_m", 0) for s in segs if s["type"] == "walk"),
                "fare": _fare(route),
            })
            if leave and arrive:
                out["duration_s"] = (arrive - leave).total_seconds()
            if leave and depart_ref and not arrive_ref:
                out["wait_before_s"] = max(0.0, (leave - depart_ref).total_seconds())
        else:  # walking beats every vehicle: Google returns a walk-only "transit" route
            out["walk_only"] = True
            out["walk_m"] = dist
        out["segments"] = segs
    out.setdefault("_ltz", otz or tz)
    out.setdefault("_atz", dtz or otz or tz)
    if mode != "TRANSIT" or out.get("walk_only"):
        if arrive_ref is not None and out["duration_s"] is not None:
            out["arrive"] = arrive_ref
            out["leave"] = arrive_ref - timedelta(seconds=out["duration_s"])
        elif depart_ref is not None and out["duration_s"] is not None:
            out["leave"] = depart_ref
            out["arrive"] = depart_ref + timedelta(seconds=out["duration_s"])
    if route.get("warnings"):
        out["warnings"] = [clean(w) for w in route["warnings"]]
    adv = route.get("travelAdvisory") or {}
    if adv.get("tollInfo"):
        out["toll"] = adv["tollInfo"]
    if route.get("optimizedIntermediateWaypointIndex") is not None:
        out["optimized_order"] = route["optimizedIntermediateWaypointIndex"]
    if with_steps:
        out["steps"] = [_step(s) for s in steps]
    if route.get("polyline"):
        out["polyline"] = route["polyline"]
    return out


def finalize(obj: Any, tz: str, ref: datetime | None = None) -> Any:
    """Convert internal datetimes to local ISO strings and drop private keys (recursively)."""
    if isinstance(obj, list):
        return [finalize(x, tz, ref) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("_dep", "_arr"):
                out[k[1:]] = local_iso(v, obj.get("_tz" if k == "_dep" else "_atz", tz))
                continue
            if k in ("leave", "arrive") and isinstance(v, datetime):
                out[k] = local_iso(v, obj.get("_ltz" if k == "leave" else "_atz", tz))
                continue
            if k.startswith("_"):
                continue
            if v is None:
                continue
            if isinstance(v, datetime):
                out[k] = local_iso(v, tz)
            elif isinstance(v, float) and k.endswith("_s"):
                out[k] = round(v)
            else:
                out[k] = finalize(v, tz, ref)
        return out
    return obj


# --------------------------------------------------------------------------- matrix
def geocoding_problems(raw: dict) -> list[str]:
    """Routes API reports unresolvable address waypoints in geocodingResults."""
    gr = raw.get("geocodingResults") or {}
    items = [("origin", gr.get("origin")), ("destination", gr.get("destination"))]
    items += [(f"intermediate {g.get('intermediateWaypointRequestIndex', i)}", g)
              for i, g in enumerate(gr.get("intermediates") or [])]
    out = []
    for name, g in items:
        st = (g or {}).get("geocoderStatus") or {}
        if st.get("code"):
            out.append(f"{name}: could not geocode ({st.get('message') or st.get('code')})")
    return out


def summarize_matrix(raw: list, origins: list, destinations: list) -> list[dict]:
    rows = []
    for el in sorted(raw or [], key=lambda e: (e.get("originIndex", 0), e.get("destinationIndex", 0))):
        oi, di = el.get("originIndex", 0), el.get("destinationIndex", 0)
        st = el.get("status") or {}
        ok = not st.get("code") and el.get("condition", "ROUTE_EXISTS") == "ROUTE_EXISTS"
        row = {"o": oi, "d": di, "ok": ok}
        if ok:
            row["duration_s"] = parse_duration(el.get("duration"))
            row["distance_m"] = el.get("distanceMeters", 0)
            if el.get("travelAdvisory", {}).get("transitFare"):
                row["fare"] = el["travelAdvisory"]["transitFare"]
        else:
            row["error"] = st.get("message") or el.get("condition")
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- places
def summarize_place(p: dict, near: tuple[float, float] | None = None) -> dict:
    loc = p.get("location") or {}
    out = {
        "name": _text(p, "displayName") or (p.get("displayName") or {}).get("text"),
        "address": p.get("shortFormattedAddress") or p.get("formattedAddress"),
        "place_id": p.get("id"),
        "type": _text(p, "primaryTypeDisplayName") or p.get("primaryType"),
        "lat_lng": [loc.get("latitude"), loc.get("longitude")] if loc else None,
        "rating": p.get("rating"),
        "ratings": p.get("userRatingCount"),
        "open_now": (p.get("currentOpeningHours") or {}).get("openNow"),
        "hours": (p.get("regularOpeningHours") or {}).get("weekdayDescriptions"),
        "price_level": p.get("priceLevel"),
        "phone": p.get("nationalPhoneNumber"),
        "website": p.get("websiteUri"),
        "status": p.get("businessStatus") if p.get("businessStatus") not in (None, "OPERATIONAL") else None,
        "maps_url": p.get("googleMapsUri"),
    }
    if near and loc:
        out["distance_m"] = round(haversine_m(near, (loc["latitude"], loc["longitude"])))
    return _drop_none(out)


# --------------------------------------------------------------------------- polyline
def decode_polyline(encoded: str, precision: int = 5) -> list[list[float]]:
    """Decode a Google encoded polyline to [[lat, lng], ...]."""
    coords, index, lat, lng = [], 0, 0, 0
    factor = 10 ** precision
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lng += delta
        coords.append([lat / factor, lng / factor])
    return coords
