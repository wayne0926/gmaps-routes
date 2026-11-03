"""Compact plain-text rendering (token-cheap; what the MCP tools return by default)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .summary import fmt_distance as D, fmt_duration as T, hhmm

VEH = {"HEAVY_RAIL": "Train", "COMMUTER_TRAIN": "Train", "HIGH_SPEED_TRAIN": "Train",
       "LONG_DISTANCE_TRAIN": "Train", "RAIL": "Train", "METRO_RAIL": "Metro", "SUBWAY": "Subway",
       "TRAM": "Tram", "LIGHT_RAIL": "Light rail", "MONORAIL": "Monorail", "BUS": "Bus",
       "INTERCITY_BUS": "Bus", "TROLLEYBUS": "Trolleybus", "SHARE_TAXI": "Share taxi",
       "FERRY": "Ferry", "CABLE_CAR": "Cable car", "GONDOLA_LIFT": "Gondola", "FUNICULAR": "Funicular"}


def _when(q: dict, tz: str) -> str:
    now = q["now"]
    if q.get("arrive_by"):
        return f"arrive by {hhmm(q['arrive_by'], tz, now, with_date=True)}"
    if q.get("depart_at"):
        return f"depart {hhmm(q['depart_at'], tz, now, with_date=True)}"
    return f"depart now ({hhmm(now, tz)})"


def line_label(s: dict) -> str:
    """'RE5', 'IC 2049', 'S19' are self-explanatory; bare numbers ('16', '602') get the vehicle."""
    veh = VEH.get(s.get("vehicle", ""), (s.get("vehicle_name") or "").strip() or "Transit")
    line = s.get("line") or ""
    return line if line and not line[0].isdigit() else f"{veh} {line}".strip()


def _seg(s: dict, tz: str, ref: datetime | None) -> str:
    if s["type"] != "transit":
        verb = {"walk": "walk", "bicycle": "bike"}.get(s["type"], s["type"])
        return f"{verb} {T(s.get('duration_s'))}"
    label = line_label(s)
    extra = [f"→{s['headsign']}" if s.get("headsign") else None,
             f"{s['stops']} stop{'s' if s['stops'] != 1 else ''}" if s.get("stops") else None,
             f"every {s['headway_min']} min" if s.get("headway_min") else None]
    extra = ", ".join(x for x in extra if x)
    tz_s = s.get("_tz", tz)
    wait = f"wait {T(s['transfer_wait_s'])} → " if s.get("transfer_wait_s", 0) >= 60 else ""
    return (f"{wait}{label} {s.get('from')} {hhmm(s.get('_dep'), tz_s, ref)} → {s.get('to')} "
            f"{hhmm(s.get('_arr'), tz_s, ref)}" + (f" ({extra})" if extra else ""))


def route_line(r: dict, mode: str, tz: str, ref: datetime | None, live: bool = True) -> str:
    """live: the query is 'depart now', so the wait before leaving is worth showing."""
    bits = []
    if r.get("leave") and r.get("arrive"):
        bits.append(f"{hhmm(r['leave'], tz, ref)} → {hhmm(r['arrive'], tz, ref)} ({T(r['duration_s'])})")
    else:
        bits.append(T(r.get("duration_s")) or "?")
    if mode != "TRANSIT" or r.get("walk_only"):
        bits.append(D(r.get("distance_m")))
    if live and r.get("wait_before_s", 0) >= 60:
        bits.append(f"leave in {T(r['wait_before_s'])}")
    if r.get("walk_only"):
        bits.append("walk only (faster than any connection)")
    if "transfers" in r:
        bits.append(f"{r['transfers']} transfer{'s' if r['transfers'] != 1 else ''}")
        bits.append(f"walk {D(r['walk_m'])}")
    if r.get("fare"):
        bits.append(f"fare {r['fare']}")
    if r.get("no_traffic_duration_s") is not None:
        bits.append(f"{T(r['no_traffic_duration_s'])} without traffic")
    if r.get("labels"):
        bits.append("/".join(r["labels"]).lower())
    return " · ".join(b for b in bits if b)


def routes_text(res: dict) -> str:
    tz, q, mode = res["_tz"], res["_query"], res["travel_mode"]
    ref = q["anchor"]
    head = f"{mode} {q['origin']} → {q['destination']}"
    if q.get("via"):
        head += f" (via {', '.join(q['via'])})"
    live = not (q.get("depart_at") or q.get("arrive_by"))
    lines = [f"{head} · {_when(q, tz)}"]
    if not res["routes"]:
        lines.append("No route found.")
    for i, r in enumerate(res["routes"], 1):
        lines.append(f"{i}. {route_line(r, mode, tz, ref, live)}")
        if r.get("segments") and not r.get("walk_only"):
            segs = [s for s in r["segments"] if s["type"] == "transit" or s.get("duration_s", 0) >= 30]
            lines.append("   " + " → ".join(_seg(s, tz, ref) for s in segs))
        if r.get("optimized_order") is not None:
            lines.append(f"   optimized stop order: {r['optimized_order']}")
        for w in r.get("warnings", []):
            lines.append(f"   ⚠ {w}")
        for s in (r.get("steps", []) if i == 1 else []):  # steps for the best option only
            if s.get("instruction") or s.get("mode") == "TRANSIT":
                lines.append(f"   - {s.get('instruction') or s.get('mode')} ({D(s.get('distance_m'))}, {T(s.get('duration_s'))})")
        if i > 1 and r.get("steps"):
            lines.append("   (steps: see format=json)")
        if r.get("polyline"):
            lines.append(f"   polyline: {r['polyline'].get('encodedPolyline', '(geojson in JSON output)')}")
    for n in res.get("notes", []):
        lines.append(f"note: {n}")
    lines.append(f"map: {res['maps_url']}")
    return "\n".join(lines)


def compare_text(res: dict) -> str:
    tz, q = res["_tz"], res["_query"]
    ref = q["anchor"]
    crit = "latest departure" if q.get("arrive_by") else "earliest arrival"
    live = not (q.get("depart_at") or q.get("arrive_by"))
    lines = [f"{q['origin']} → {q['destination']} · {_when(q, tz)} · ranked by {crit}"]
    for m in res["ranking"] + [m for m in res["modes"] if m not in res["ranking"]]:
        v = res["modes"][m]
        if v.get("error"):
            lines.append(f"{m:8s} error: {v['error']}")
            continue
        best = v.get("best")
        if not best:
            lines.append(f"{m:8s} no route")
            continue
        line = f"{m:8s} {route_line(best, m, tz, ref, live)}"
        if best.get("segments") and not best.get("walk_only"):
            line += " · " + " + ".join(line_label(s) for s in best["segments"] if s["type"] == "transit")
        lines.append(line)
        for w in best.get("warnings", []):
            lines.append(f"         ⚠ {w}")
    for n in res.get("notes", []):
        lines.append(f"note: {n}")
    return "\n".join(lines)


def matrix_text(res: dict) -> str:
    o, d = res["_origins"], res["_destinations"]
    lines = [f"{res['travel_mode']} matrix ({len(o)}×{len(d)})"]
    for r in res["matrix"]:
        val = f"{T(r['duration_s'])}, {D(r['distance_m'])}" if r["ok"] else f"no route ({r.get('error')})"
        lines.append(f"{o[r['o']]} → {d[r['d']]}: {val}")
    for n in res.get("notes", []):
        lines.append(f"note: {n}")
    return "\n".join(lines)


def places_text(res: dict, tz: str = "Europe/Berlin") -> str:
    if not res["places"]:
        return f"No places found for {res['query']!r}."
    lines = ["(filtered: open now)"] if res.get("open_now") else []
    for i, p in enumerate(res["places"], 1):
        bits = [f"{i}. {p.get('name')}", p.get("address")]
        if p.get("rating") is not None:
            bits.append(f"{p['rating']}★ ({p.get('ratings', 0)})")
        if p.get("open_now") is not None:
            bits.append("open now" if p["open_now"] else "closed now")
        if p.get("distance_m") is not None:
            bits.append(D(p["distance_m"]))
        if p.get("status"):
            bits.append(p["status"].lower())
        bits.append(f"place_id:{p['place_id']}")
        lines.append(" · ".join(b for b in bits if b))
        for k in ("phone", "website"):
            if p.get(k):
                lines.append(f"   {k}: {p[k]}")
        if p.get("hours"):  # weekdayDescriptions: Monday first; text shows today only
            today = p["hours"][datetime.now(ZoneInfo(tz)).weekday() % len(p["hours"])]
            lines.append(f"   today: {today.split(': ', 1)[-1]}")
    return "\n".join(lines)


def geocode_text(res: dict) -> str:
    if not res["results"]:
        return "No results."
    return "\n".join(f"{i}. {r['address']} · {r['lat']},{r['lng']} · place_id:{r['place_id']}"
                     + (f" · {r['precision']}" if r.get("precision") else "")
                     for i, r in enumerate(res["results"], 1))
