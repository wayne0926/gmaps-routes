"""Turn raw Routes API JSON into compact, readable summaries (raw is kept separately)."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MODE_LABEL = {"WALK": "Walk", "BICYCLE": "Bike", "DRIVE": "Drive", "TWO_WHEELER": "Moto",
              "TRANSIT": "Transit"}


def parse_duration(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).rstrip("s"))


def fmt_duration(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = round(sec)
    if sec < 60:
        return "<1 min"
    h, rem = divmod(sec, 3600)
    m = round(rem / 60)
    if m == 60:
        h, m = h + 1, 0
    return f"{h} h {m} min" if h else f"{m} min"


def fmt_distance(m: float | None) -> str | None:
    if m is None:
        return None
    return f"{int(m)} m" if m < 1000 else f"{m / 1000:.1f} km"


def _latlng(loc: dict | None) -> list[float] | None:
    ll = (loc or {}).get("latLng")
    return [ll["latitude"], ll["longitude"]] if ll else None


def _local_time(iso: str | None, tz: str) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(tz)).strftime("%a %H:%M")
    except Exception:
        return iso


def _text(d: dict | None, *path: str) -> str | None:
    cur: Any = d
    for p in path:
        cur = (cur or {}).get(p)
    return cur.get("text") if isinstance(cur, dict) else None


def summarize_step(step: dict, tz: str) -> dict:
    nav = step.get("navigationInstruction") or {}
    lv = step.get("localizedValues") or {}
    dur = parse_duration(step.get("staticDuration"))
    out: dict = {
        "mode": step.get("travelMode"),
        "instruction": nav.get("instructions"),
        "maneuver": nav.get("maneuver"),
        "distance_m": step.get("distanceMeters", 0),
        "distance_text": _text(lv, "distance"),
        "duration_s": dur,
        "duration_text": _text(lv, "staticDuration") or fmt_duration(dur),
        "start": _latlng(step.get("startLocation")),
        "end": _latlng(step.get("endLocation")),
    }
    td = step.get("transitDetails")
    if td:
        sd = td.get("stopDetails") or {}
        line = td.get("transitLine") or {}
        veh = line.get("vehicle") or {}
        tlv = td.get("localizedValues") or {}
        dep_tz = (tlv.get("departureTime") or {}).get("timeZone") or tz
        arr_tz = (tlv.get("arrivalTime") or {}).get("timeZone") or tz
        out["transit"] = {
            "line": line.get("nameShort") or line.get("name"),
            "line_name": line.get("name"),
            "vehicle": veh.get("type"),
            "vehicle_name": _text(veh, "name"),
            "headsign": td.get("headsign"),
            "headway_s": parse_duration(td.get("headway")),
            "trip_short_text": td.get("tripShortText"),
            "agencies": [a.get("name") for a in line.get("agencies", []) if a.get("name")],
            "line_url": line.get("uri"),
            "color": line.get("color"),
            "stop_count": td.get("stopCount"),
            "departure_stop": (sd.get("departureStop") or {}).get("name"),
            "departure_stop_location": _latlng((sd.get("departureStop") or {}).get("location")),
            "departure_time": sd.get("departureTime"),
            "departure_local": _local_time(sd.get("departureTime"), dep_tz),
            "arrival_stop": (sd.get("arrivalStop") or {}).get("name"),
            "arrival_stop_location": _latlng((sd.get("arrivalStop") or {}).get("location")),
            "arrival_time": sd.get("arrivalTime"),
            "arrival_local": _local_time(sd.get("arrivalTime"), arr_tz),
        }
    adv = step.get("travelAdvisory")
    if adv:
        out["travel_advisory"] = adv
    return out


def _step_label(s: dict) -> str:
    t = s.get("transit")
    if t:
        veh = (t.get("vehicle") or "TRANSIT").replace("_", " ").title()
        stops = f", {t['stop_count']} stops" if t.get("stop_count") else ""
        return (f"{veh} {t.get('line') or ''} {t.get('departure_stop')} {t.get('departure_local') or ''}"
                f" -> {t.get('arrival_stop')} {t.get('arrival_local') or ''}{stops}").replace("  ", " ")
    return f"{MODE_LABEL.get(s['mode'], s['mode'] or 'Move')} {s['duration_text'] or ''}".strip()


def summarize_route(route: dict, index: int, tz: str) -> dict:
    lv = route.get("localizedValues") or {}
    dur = parse_duration(route.get("duration"))
    static = parse_duration(route.get("staticDuration"))
    legs_out, steps_flat = [], []
    for leg in route.get("legs", []):
        steps = [summarize_step(s, tz) for s in leg.get("steps", [])]
        steps_flat.extend(steps)
        ldur = parse_duration(leg.get("duration")) or parse_duration(leg.get("staticDuration"))
        legs_out.append({
            "start": _latlng(leg.get("startLocation")), "end": _latlng(leg.get("endLocation")),
            "distance_m": leg.get("distanceMeters", 0), "distance_text": fmt_distance(leg.get("distanceMeters", 0)),
            "duration_s": ldur, "duration_text": fmt_duration(ldur),
            "travel_advisory": leg.get("travelAdvisory"), "steps": steps,
        })
    transit_steps = [s for s in steps_flat if s.get("transit")]
    walk_m = sum(s["distance_m"] for s in steps_flat if s["mode"] == "WALK")
    walk_s = sum(s["duration_s"] or 0 for s in steps_flat if s["mode"] == "WALK")
    dist = route.get("distanceMeters", 0)
    out: dict = {
        "index": index,
        "labels": route.get("routeLabels"),
        "description": route.get("description"),
        "duration_s": dur if dur is not None else static,
        "duration_text": _text(lv, "duration") or fmt_duration(dur if dur is not None else static),
        "static_duration_s": static,
        "distance_m": dist,
        "distance_text": _text(lv, "distance") or fmt_distance(dist),
    }
    if transit_steps:
        first, last = transit_steps[0]["transit"], transit_steps[-1]["transit"]
        out.update({
            "first_departure": first["departure_local"], "first_departure_time": first["departure_time"],
            "last_arrival": last["arrival_local"], "last_arrival_time": last["arrival_time"],
            "transfers": len(transit_steps) - 1,
            "walking_m": walk_m, "walking_text": fmt_distance(walk_m),
            "walking_duration_text": fmt_duration(walk_s),
            "lines": [t["transit"]["line"] for t in transit_steps],
            "fare": _text(lv, "transitFare"),
        })
        out["headline"] = " | ".join(_step_label(s) for s in steps_flat)
    else:
        out["headline"] = f"{MODE_LABEL.get(steps_flat[0]['mode'], steps_flat[0]['mode']) if steps_flat else 'Route'} " \
                          f"{out['distance_text']} · {out['duration_text']}"
    if route.get("warnings"):
        out["warnings"] = route["warnings"]
    if route.get("optimizedIntermediateWaypointIndex") is not None:
        out["optimized_intermediate_order"] = route["optimizedIntermediateWaypointIndex"]
    if route.get("travelAdvisory"):
        out["travel_advisory"] = route["travelAdvisory"]
    if route.get("routeToken"):
        out["has_route_token"] = True
    out["legs"] = legs_out
    return out


def summarize_routes(raw: dict, default_tz: str = "Europe/Berlin") -> list[dict]:
    return [summarize_route(r, i, default_tz) for i, r in enumerate(raw.get("routes", []))]


def _label(spec: Any) -> str:
    if isinstance(spec, dict):
        return str(spec.get("address") or spec.get("place_id") or f"{spec.get('lat')},{spec.get('lng')}")
    return str(spec)


def summarize_matrix(raw: list, origins: list, destinations: list) -> list[dict]:
    rows = []
    for el in sorted(raw or [], key=lambda e: (e.get("originIndex", 0), e.get("destinationIndex", 0))):
        oi, di = el.get("originIndex", 0), el.get("destinationIndex", 0)
        dur = parse_duration(el.get("duration"))
        st = el.get("status") or {}
        ok = not st.get("code") and el.get("condition", "ROUTE_EXISTS") == "ROUTE_EXISTS"
        lv = el.get("localizedValues") or {}
        rows.append({
            "origin": _label(origins[oi]), "destination": _label(destinations[di]),
            "origin_index": oi, "destination_index": di, "ok": ok,
            "condition": el.get("condition"), "status": st or None,
            "duration_s": dur, "duration_text": _text(lv, "duration") or fmt_duration(dur),
            "static_duration_s": parse_duration(el.get("staticDuration")),
            "distance_m": el.get("distanceMeters", 0),
            "distance_text": _text(lv, "distance") or fmt_distance(el.get("distanceMeters", 0)),
            "travel_advisory": el.get("travelAdvisory"), "fallback_info": el.get("fallbackInfo"),
        })
    return rows


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
