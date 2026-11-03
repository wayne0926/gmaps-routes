"""Live public-transport data (real-time delays, platforms, cancellations).

* DB OpenBoards (official, DB InfraGO; free plan, 10 req/s): station departure/arrival boards
  for German rail with predicted times, platform changes, cancellations, delay reasons and
  disruption texts.  Needs DB_CLIENT_ID + DB_API_KEY (DB API Marketplace, "OpenBoards" Free plan).
* Transitous (community MOTIS instance, DELFI GTFS/GTFS-RT for Germany + feeds Europe-wide;
  free, no key, NON-COMMERCIAL use, attribution https://transitous.org/sources/): journey
  planning with real-time data, and boards for all modes (bus/tram too).  Used for
  live_connections, and for boards when no DB keys are configured or outside Germany.
Neither source has fares.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import client as C
from . import summary as S

TRANSITOUS = "https://api.transitous.org/api"
OPENBOARDS = "https://apis.deutschebahn.com/db-api-marketplace/apis/open-boards/v1"
UA = "gmaps-routes (+https://github.com/wayne0926/gmaps-routes)"
ATTRIB = "data: Transitous (transitous.org/sources)"
_DHID = re.compile(r"(?<![a-z])([a-z]{2}:\d{5}:\d+)")  # ids look like 'de-DELFI_de:05314:61101:7:73'
_EVA = re.compile(r"^\d{6,7}$")


class LiveError(C.GoogleMapsError):
    pass


def db_keys() -> tuple[str, str] | None:
    cid, key = os.environ.get("DB_CLIENT_ID", "").strip(), os.environ.get("DB_API_KEY", "").strip()
    return (cid, key) if cid and key else None


def _http(method: str, url: str, *, params=None, json=None, headers=None, secret=None) -> Any:
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    try:
        r = C._session().request(method, url, params=params, json=json, headers=h, timeout=(5, 20))
    except Exception as e:  # network
        raise LiveError(C._scrub(f"network error: {type(e).__name__}: {e}", secret)) from None
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:500]}
    if r.status_code >= 400:
        msg = (body.get("detail") or body.get("title") or body.get("moreInformation")
               or body.get("httpMessage") or body.get("raw") or "") if isinstance(body, dict) else ""
        if r.status_code in (401, 403) and "deutschebahn" in url:
            msg = ("DB API rejected the credentials (check DB_CLIENT_ID / DB_API_KEY and that the "
                   "app is subscribed to OpenBoards)")
        raise LiveError(C._scrub(f"HTTP {r.status_code}: {msg}", secret), r.status_code, body)
    return body


# --------------------------------------------------------------------------- stops
_stop_cache: dict[tuple, dict] = {}


_GENERIC = re.compile(r"\b(hbf|hauptbahnhof|bahnhof|bf|station|stasjon|stazione|gare|"
                      r"estaci[oó]n|dworzec|nádraží|central|centraal|zentrum)\b\.?", re.I)


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def match_score(query: str, name: str) -> float:
    """0..1 similarity of a typed place and a resolved stop name (diacritics/case-insensitive;
    the query's significant words must appear in the name)."""
    from difflib import SequenceMatcher
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0.0
    words = [w for w in _norm(_GENERIC.sub(" ", query)).split() if len(w) > 2] or q.split()
    hit = sum(1 for w in words if w in n) / len(words)
    # significant words dominate; raw string similarity only breaks ties ('Falkensee, Bahnhof'
    # is ~0.56 similar to 'Fantasiebahnhof Nirgendwo' but shares no real word)
    return round(0.8 * hit + 0.2 * SequenceMatcher(None, q, n).ratio(), 3)


def _area(r: dict) -> str | None:
    for a in r.get("areas") or []:
        if a.get("default") or a.get("matched"):
            return a.get("name")
    return None


def _rerank(results: list[dict], score) -> list[dict]:
    """Keep the geocoder's order (it knows prominence: Frankfurt = am Main) unless another
    of the top hits scores clearly better (> 0.2), e.g. a rail station for a rail board."""
    if not results:
        return results
    top = results[:10]
    best = max(top, key=score)
    if score(best) > score(top[0]) + 0.2:
        return [best] + [r for r in results if r is not best]
    return results


def _score(query: str, r: dict) -> float:
    """Match against name + town, so 'Bonn, Kaiserplatz' prefers Bonn's Kaiserplatz over
    Düren's; a small bonus for real stops over streets/places of the same name."""
    return match_score(query, f"{r.get('name', '')} {_area(r) or ''}") \
        + (0.05 if r.get("type") == "STOP" else 0)


def find_stop(query: str, near: str | None = None, prefer_rail: bool = False) -> dict:
    """-> {name, id (Transitous stop id), dhid, eva, lat, lon, tz, area, confidence}.
    Accepts a name, a DHID ('de:05314:61101'), an EVA number ('8000044') or 'lat,lng'
    (nearest stop).  prefer_rail: rank stops served by trains first (for rail boards)."""
    q = str(query).strip()
    if _EVA.match(q):
        return {"name": q, "eva": q, "confidence": 1.0, "by_id": True}
    if m := _DHID.fullmatch(q):
        return {"name": q, "dhid": m.group(1), "id": f"de-DELFI_{m.group(1)}", "confidence": 1.0,
                "by_id": True}
    key = (q.lower(), near, prefer_rail)
    if key in _stop_cache:
        return _stop_cache[key]
    params = {"text": q, "type": "STOP"}
    if near:
        params["place"] = near
    ll = C._LATLNG.match(q)
    if ll:  # nearest stop to a coordinate: reverse geocode
        res = _http("GET", f"{TRANSITOUS}/v1/reverse-geocode",
                    params={"place": f"{ll.group(1)},{ll.group(2)}", "type": "STOP"})
    else:
        res = _http("GET", f"{TRANSITOUS}/v1/geocode", params=params)
    stops = [r for r in res if r.get("type") == "STOP"] or res
    if not stops:
        raise LiveError(f"no stop found for {q!r}")
    if not ll:
        def sc(r):
            rail = bool(set(r.get("modes") or []) & _RAIL)
            return _score(q, r) + (0.3 if prefer_rail and rail else 0)
        stops = _rerank(stops, sc)
    best = stops[0]
    # DB boards need a German DHID; the top hit can be a foreign feed's copy of the station
    # (e.g. Bonn Hbf from SNCB): take the DELFI twin nearby if there is one.
    if not _DHID.search(best.get("id", "")):
        for s in stops[1:8]:
            if _DHID.search(s.get("id", "")) and S.haversine_m(
                    (best["lat"], best["lon"]), (s["lat"], s["lon"])) < 400:
                best = {**s, "name": best["name"]}
                break
    m = _DHID.search(best.get("id", ""))
    out = {"name": best.get("name"), "id": best.get("id"), "dhid": m.group(1) if m else None,
           "lat": best.get("lat"), "lon": best.get("lon"), "tz": best.get("tz"),
           "area": _area(best), "query": q,
           "confidence": 1.0 if ll else round(min(1.0, _score(q, best)), 2),
           "alternatives": [] if ll else ambiguous_alternatives(q, stops, stops[0])}
    if not out["dhid"] and (best.get("tz") or "").startswith("Europe/Berlin"):
        out["dhid"] = _dhid_via_stoptimes(out["id"])
    if out["dhid"] and not out["id"].startswith("de-DELFI_"):
        out["delfi_id"] = f"de-DELFI_{out['dhid']}"
    _stop_cache[key] = out
    return out


def ambiguous_alternatives(query: str, results: list[dict], best: dict) -> list[str]:
    """Other candidates that match the query as well as `best` but are far away
    ('Hauptbahnhof' -> Stuttgart, Köln, Berlin ...).  Empty if the choice is clear."""
    if not best.get("lat"):
        return []
    top = _score(query, best)
    imp = best.get("importance") or 0
    if not imp:  # towns/addresses carry no prominence score: trust the geocoder's first pick
        return []
    alts = []
    for r in results[:10]:
        if r is best or not r.get("lat"):
            continue
        # comparable name match AND comparable prominence (a street called 'Narvik' in
        # Magdeburg does not make the town ambiguous; Hamburg vs Köln 'Hauptbahnhof' does)
        if _score(query, r) >= top - 0.05 \
                and (r.get("importance") or 0) >= 0.4 * imp and S.haversine_m(
                (best["lat"], best["lon"]), (r["lat"], r["lon"])) > 30000:
            label = r.get("name", "?") + (f" ({_area(r)})" if _area(r) and _area(r) not in r.get("name", "") else "")
            if label not in alts:
                alts.append(label)
    return alts[:4]


def resolved_note(stop: dict) -> str | None:
    """'X' -> resolved name (area); flags low-confidence matches."""
    if stop.get("by_id") or not stop.get("query"):
        return None
    name = stop.get("name") or "?"
    where = f" ({stop['area']})" if stop.get("area") and stop["area"] not in name else ""
    conf = stop.get("confidence", 1.0)
    if stop.get("alternatives"):
        return (f"⚠ '{stop['query']}' is ambiguous → picked {name}{where}; also: "
                f"{'; '.join(stop['alternatives'])}. Add the city.")
    if conf < 0.5:
        return (f"⚠ '{stop['query']}' matched only weakly → {name}{where}; probably not what you "
                "meant: give the full station name with the city")
    kind = stop.get("kind")
    if _norm(stop["query"]) != _norm(name) or where or kind:
        return f"'{stop['query']}' → {name}{where}{kind or ''}"
    return None


def _dhid_via_stoptimes(stop_id: str) -> str | None:
    """Transitous merges a station's copies from several feeds under one id (Bonn Hbf is
    'be-sncb_8015485'); its stop times still name the German DELFI platforms, whose ids carry
    the station's DHID."""
    try:
        raw = _http("GET", f"{TRANSITOUS}/v5/stoptimes", params={"stopId": stop_id, "n": 12})
    except LiveError:
        return None
    seen: dict[str, int] = {}
    for st in raw.get("stopTimes", []):
        m = _DHID.search((st.get("place") or {}).get("stopId", ""))
        if m:
            seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    return max(seen, key=seen.get) if seen else None


# --------------------------------------------------------------------------- helpers
def _ts(s: str | None) -> datetime | None:
    return S.parse_ts(s) if s else None


def _delay_min(real: datetime | None, sched: datetime | None) -> int | None:
    if not real or not sched:
        return None
    return round((real - sched).total_seconds() / 60)


def _platform(place: dict) -> str | None:
    """Transitous: 'description' carries the real platform ('Bahnsteig Gleis 2'); 'track' is
    often an internal number."""
    d = place.get("description") or ""
    m = re.search(r"Gleis\s*([0-9]+[a-zA-Z]?(?:\s*[a-zA-Z]-?[a-zA-Z]?)?)", d)
    if m:
        return m.group(1).strip()
    t = place.get("track")
    return t if t and len(t) <= 3 else None


def _clock(dt: datetime | None, tz: str, ref: datetime) -> str:
    return S.hhmm(dt, tz, ref, with_date=True) or "?"


def _delay_txt(d: int | None) -> str:
    if d is None:
        return ""
    return " (on time)" if d == 0 else f" ({'+' if d > 0 else ''}{d})"


# --------------------------------------------------------------------------- boards
def station_board(station: str, *, time: str | None = None, arrivals: bool = False,
                  duration_min: int = 60, max_results: int = 15, all_modes: bool = False,
                  source: str = "auto", language: str = "eng", format: str = "text"):
    """Live departure (or arrival) board.  source: auto | db | transitous.
    all_modes: include bus/tram/subway (Transitous; DB OpenBoards covers rail only)."""
    C._check_format(format)
    if source not in ("auto", "db", "transitous"):
        raise ValueError("source must be auto, db or transitous")
    now = datetime.now(timezone.utc)
    t0 = C.parse_time(time) or now
    stop = find_stop(station, prefer_rail=not all_modes)
    if stop.get("alternatives"):
        raise LiveError(f"{station!r} is ambiguous: {stop.get('name')}"
                        f"{' (' + stop['area'] + ')' if stop.get('area') else ''}, "
                        f"{', '.join(stop['alternatives'])}... add the city name")
    if stop.get("confidence", 1.0) < 0.35:
        raise LiveError(f"no station matching {station!r} (closest: {stop.get('name')}"
                        f"{', ' + stop['area'] if stop.get('area') else ''}); give the full "
                        "station name with the city, a DHID or an EVA number")
    keys = db_keys()
    use_db = source == "db" or (source == "auto" and keys and not all_modes
                                and (stop.get("dhid") or stop.get("eva")))
    if use_db:
        if not keys:
            raise LiveError("DB OpenBoards needs DB_CLIENT_ID and DB_API_KEY (DB API Marketplace)")
        if not (stop.get("dhid") or stop.get("eva")):
            raise LiveError(f"{stop.get('name')!r} is not a German rail station (no DHID/EVA)")
        res = _db_board(stop, t0, arrivals, duration_min, max_results, language, keys)
    else:
        res = _transitous_board(stop, t0, arrivals, max_results, all_modes)
        if source == "auto" and not keys and not all_modes:
            res["notes"].append("DB keys not configured: board from Transitous (DELFI real-time)")
    rn = resolved_note(stop)
    if rn:
        res["notes"].insert(0, rn)
    if C.parse_time(time) and t0 < now - timedelta(minutes=10):
        res["notes"].append("requested time is in the past")
    res["_now"] = now
    if format == "raw":
        return res["_raw"]
    if format == "json":
        return S.finalize({k: v for k, v in res.items() if not k.startswith("_")},
                          res.get("tz") or C.default_tz())
    return _board_text(res)


def _db_board(stop, t0, arrivals, duration_min, max_results, language, keys) -> dict:
    sp = {"evaNumbers": [stop["eva"]]} if stop.get("eva") else {"dhids": [stop["dhid"]]}
    end = t0 + timedelta(minutes=max(10, min(int(duration_min), 480)))
    body = {"stopPlaceOrStation": sp,
            "timeWindow": {"timeStart": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "timeEnd": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "maxViaStops": 4, "languages": [language, "deu"] if language != "deu" else ["deu"],
            "maxEntriesPerTransportType": 50}
    raw = _http("POST", f"{OPENBOARDS}/{'arrivals' if arrivals else 'departures'}", json=body,
                headers={"DB-Client-ID": keys[0], "DB-Api-Key": keys[1],
                         "Accept": "application/vnd.de.db.ris+json, application/json",
                         "Content-Type": "application/json"}, secret=keys[1])
    return parse_db_board(raw, stop, arrivals, max_results, language)


def _loc(items: list | None, language: str, field="name") -> str | None:
    if not items:
        return None
    for it in items:
        if it.get("language") == language:
            return it.get(field) or it.get("text")
    return items[0].get(field) or items[0].get("text")


def parse_db_board(raw: dict, stop: dict, arrivals: bool, max_results: int,
                   language: str = "eng") -> dict:
    names = {sp["evaNumber"]: _loc(sp.get("localizedNames"), language)
             for sp in raw.get("stopPlaces", [])}
    msgs: dict[int, str] = {}
    m = raw.get("messages") or {}
    for coll in ("risCauseCodes", "disruptions", "notes", "risQualityDeviations"):
        for x in m.get(coll) or []:
            d = x.get("descriptions") or []
            txt = None
            for it in d:
                if it.get("language") == language:
                    txt = it.get("textShort") or it.get("text")
            txt = txt or (d[0].get("textShort") or d[0].get("text") if d else None)
            if txt and x.get("code") != "00":
                msgs[x["messageID"]] = S.clean(txt)
    entries = raw.get("arrivals" if arrivals else "departures") or []
    out = []
    for e in entries:
        tr = e.get("transport") or {}
        ji = e.get("journeyInfo") or {}
        sched, best = _ts(e.get("timeSchedule")), _ts(e.get("time"))
        if arrivals:
            ends = (ji.get("differingOrigin") or ji.get("origin") or {}).get("evaNumber")
        else:
            ends = (ji.get("differingDestination") or ji.get("destination") or {}).get("evaNumber")
        direction = ((tr.get("direction") or {}).get("texts") or [None])[0] or names.get(ends)
        plat = (e.get("platform") or {}).get("name")
        plat_s = (e.get("platformSchedule") or {}).get("name")
        line = (tr.get("line") or {}).get("name")
        if line and line[0].isdigit() and tr.get("category"):  # FlixTrain line '10' -> 'FLX 10'
            line = f"{tr['category']} {line}"
        desc = tr.get("journeyDescription") or " ".join(
            str(x) for x in (tr.get("category"), tr.get("journeyNumber")) if x)
        out.append(S._drop_none({
            "line": line or desc,
            "train": str(tr["journeyNumber"]) if line and tr.get("journeyNumber")
            and str(tr["journeyNumber"]) not in line else None,
            "label": tr.get("label"), "type": tr.get("type"),
            "to" if not arrivals else "from": direction,
            "via": [names.get(v["evaNumber"]) + (" (cancelled)" if v.get("cancelled") else "")
                    for v in (tr.get("via") or []) if names.get(v.get("evaNumber"))][:4] or None,
            "_sched": sched, "_time": best,
            "delay_min": _delay_min(best, sched) if e.get("timeType") in ("PREVIEW", "REAL") else None,
            "realtime": e.get("timeType") in ("PREVIEW", "REAL"),
            "platform": plat, "platform_planned": plat_s if plat_s and plat_s != plat else None,
            "cancelled": bool(e.get("cancelled")) or None,
            "additional": bool(e.get("additional")) or None,
            "replacement": bool(tr.get("replacementTransport")) or None,
            "messages": [msgs[i] for i in e.get("messages") or [] if i in msgs] or None,
        }))
    out.sort(key=lambda e: e.get("_sched") or e.get("_time"))
    out = out[:max_results]
    station_msgs = [msgs[i] for i in raw.get("disruptionCommunications") or [] if i in msgs]
    station = stop.get("name")
    if stop.get("by_id") or not station:  # EVA/DHID input: use DB's name for the station
        eva = stop.get("eva") or next((e.get("evaNumber") for e in raw.get(
            "arrivals" if arrivals else "departures", []) if e.get("evaNumber")), None)
        station = names.get(eva) or station
    return {"station": station or stop.get("name"), "kind": "arrivals" if arrivals else "departures",
            "source": "DB OpenBoards", "tz": "Europe/Berlin", "entries": out,
            "station_messages": station_msgs or None, "notes": [], "_raw": raw}


_RAIL = {"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL", "REGIONAL_RAIL",
         "SUBURBAN", "RAIL"}


def _transitous_board(stop, t0, arrivals, max_results, all_modes) -> dict:
    sid = stop.get("delfi_id") or stop.get("id") or (f"de-DELFI_{stop['dhid']}" if stop.get("dhid") else None)
    if not sid:
        raise LiveError("Transitous needs a stop name or DHID (EVA numbers only work with DB keys)")
    params = {"stopId": sid, "time": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "n": min(max_results + 5, 100), "arriveBy": str(bool(arrivals)).lower()}
    if not all_modes:
        params["mode"] = ",".join(sorted(_RAIL - {"RAIL"}))
    raw = _http("GET", f"{TRANSITOUS}/v5/stoptimes", params=params)
    tz = (raw.get("place") or {}).get("tz") or stop.get("tz") or C.default_tz()
    out = []
    for st in raw.get("stopTimes", []):
        if not all_modes and st.get("mode") not in _RAIL:
            continue
        p = st.get("place") or {}
        key = "Arrival" if arrivals else "Departure"
        sched, best = _ts(p.get("scheduled" + key)), _ts(p.get(key.lower()))
        plat = _platform(p)
        out.append(S._drop_none({
            "line": st.get("displayName") or st.get("routeShortName"),
            "train": st.get("tripShortName").lstrip("0") if st.get("tripShortName") else None,
            "type": st.get("mode"), "agency": st.get("agencyName"),
            ("from" if arrivals else "to"): ((st.get("tripFrom") or {}).get("name") if arrivals
                                            else st.get("headsign") or (st.get("tripTo") or {}).get("name")),
            "_sched": sched, "_time": best,
            "delay_min": _delay_min(best, sched) if st.get("realTime") else None,
            "realtime": bool(st.get("realTime")),
            "platform": plat,
            "cancelled": bool(st.get("cancelled") or st.get("tripCancelled")) or None,
        }))
    # board order = scheduled time; drop stale rows (> 1 h before the window, feed artefacts)
    out = [e for e in out if (e.get("_sched") or e.get("_time") or t0) >= t0 - timedelta(hours=1)
           or (e.get("realtime") and (e.get("_time") or t0) >= t0)]
    out.sort(key=lambda e: e.get("_sched") or e.get("_time") or t0)
    out = out[:max_results]
    return {"station": (raw.get("place") or {}).get("name") or stop.get("name"),
            "kind": "arrivals" if arrivals else "departures", "source": "Transitous", "tz": tz,
            "entries": out, "notes": [ATTRIB], "_raw": raw}


def _board_text(res: dict) -> str:
    tz, now = res["tz"], res["_now"]
    lines = [f"{res['station']} · {res['kind']} · live: {res['source']}"]
    foot: list[str] = []  # long notices (construction etc.) repeat across trains: list once

    def ref(m: str) -> str:
        if len(m) <= 90:
            return m
        if m not in foot:
            foot.append(m)
        return f"[{foot.index(m) + 1}]"
    for m in res.get("station_messages") or []:
        lines.append(f"⚠ {m}")
    if not res["entries"]:
        lines.append("(no entries in this time window)")
    for e in res["entries"]:
        sched = _clock(e.get("_sched") or e.get("_time"), tz, now)
        d = e.get("delay_min")
        t = sched + (f" +{d}" if d and d > 0 else f" {d}" if d and d < 0 else "") \
            + ("" if e.get("realtime") else " [sched]")
        if e.get("cancelled"):
            t = f"{sched} ✗ CANCELLED"
        name = e.get("line") or "?"
        if e.get("train") and e["train"] not in name:
            name += f" ({e['train']})"
        where = e.get("to") or e.get("from") or ""
        bits = [t, f"{name} {'→' if 'to' in e else '←'} {where}".strip()]
        if e.get("platform"):
            rail = e.get("type") in _RAIL or str(e.get("type", "")).endswith("TRAIN")
            bits.append(f"{'Gl.' if rail else 'stop'} {e['platform']}"
                        + (f" (planned {e['platform_planned']}) ⚠" if e.get("platform_planned") else ""))
        if e.get("additional"):
            bits.append("extra train")
        if e.get("replacement"):
            bits.append("replacement service")
        lines.append(" · ".join(bits))
        if e.get("via"):
            lines.append(f"      via {', '.join(e['via'])}")
        if e.get("messages"):
            lines.append("      ↳ " + "; ".join(ref(m) for m in e["messages"]))
    for i, m in enumerate(foot, 1):
        lines.append(f"[{i}] {m}")
    for n in res.get("notes", []):
        lines.append(f"note: {n}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- journeys
def _place(spec: str, bias: str | None = None) -> tuple:
    """-> (Transitous place param, label, resolution note, 'lat,lon').  'lat,lng'/DHID as is; else
    geocode (any type), picking the best name match among the top hits."""
    s = str(spec).strip()
    if m := C._LATLNG.match(s):
        return f"{m.group(1)},{m.group(2)}", s, None, f"{m.group(1)},{m.group(2)}"
    if m := _DHID.fullmatch(s):
        return f"de-DELFI_{m.group(1)}", s, None, None
    params = {"text": s}
    if bias:  # e.g. the origin's position when resolving the destination
        params["place"] = bias
    res = _http("GET", f"{TRANSITOUS}/v1/geocode", params=params)
    if not res:
        raise LiveError(f"place not found: {s!r}")
    ranked = _rerank(res, lambda r: _score(s, r))
    r = ranked[0]
    name = r.get("name") or s
    kind = None if r.get("type") == "STOP" else " (a place, not a stop: routed from/to its location)"
    note = resolved_note({"query": s, "name": name, "area": _area(r), "kind": kind,
                          "confidence": min(1.0, _score(s, r)),
                          "alternatives": ambiguous_alternatives(s, ranked, r)})
    label = f"{name} ({_area(r)})" if _area(r) and _area(r) not in name else name
    pos = f"{r['lat']},{r['lon']}"
    if r.get("type") == "STOP" and r.get("id"):
        return r["id"], label, note, pos
    return pos, label, note, pos


# Everything but flights (MOTIS mode names); pass modes=[..., "AIRPLANE"] to allow them.
GROUND_MODES = ("RAIL", "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL",
                "REGIONAL_RAIL", "SUBURBAN", "METRO", "SUBWAY", "TRAM", "BUS", "COACH", "FERRY",
                "FUNICULAR", "AERIAL_LIFT", "CABLE_CAR", "OTHER")
_MODE_GROUPS = {"TRAIN": ("RAIL", "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL",
                          "REGIONAL_FAST_RAIL", "REGIONAL_RAIL", "SUBURBAN"),
                "LOCAL_TRAIN": ("REGIONAL_FAST_RAIL", "REGIONAL_RAIL", "SUBURBAN"),
                "LIGHT_RAIL": ("TRAM", "SUBWAY", "METRO"), "FLIGHT": ("AIRPLANE",)}


def live_connections(origin: str, destination: str, *, departure_time: str | None = None,
                     arrival_time: str | None = None, max_results: int = 4,
                     max_transfers: int | None = None, modes: list[str] | None = None,
                     format: str = "text"):
    """Journey planning with real-time delays and platforms (Transitous / MOTIS).
    modes: MOTIS modes or groups TRAIN / LOCAL_TRAIN / LIGHT_RAIL / BUS / FLIGHT ...;
    default = every ground mode (no flights)."""
    C._check_format(format)
    if departure_time and arrival_time:
        raise ValueError("departure_time and arrival_time are mutually exclusive")
    o, olab, on, opos = _place(origin)
    d, dlab, dn, _ = _place(destination, bias=opos)
    t = C.parse_time(arrival_time or departure_time)
    now0 = datetime.now(timezone.utc)
    tm: list[str] = []
    for m_ in (modes or GROUND_MODES):
        tm += list(_MODE_GROUPS.get(m_.upper(), (m_.upper(),)))
    params = {"fromPlace": o, "toPlace": d, "numItineraries": max(1, min(int(max_results), 10)),
              "transitModes": ",".join(dict.fromkeys(tm))}
    if t:
        params["time"] = t.strftime("%Y-%m-%dT%H:%M:%SZ")
    if arrival_time:
        params["arriveBy"] = "true"
    if max_transfers is not None:
        params["maxTransfers"] = int(max_transfers)
    raw = _http("GET", f"{TRANSITOUS}/v5/plan", params=params)
    if format == "raw":
        return raw
    now = datetime.now(timezone.utc)
    its = []
    for it in raw.get("itineraries", [])[:max_results]:
        legs = []
        for lg in it.get("legs", []):
            f, to = lg.get("from") or {}, lg.get("to") or {}
            if lg.get("mode") == "WALK":
                legs.append({"type": "walk", "duration_s": lg.get("duration"),
                             "distance_m": lg.get("distance")})
                continue
            dep, dep_s = _ts(f.get("departure")), _ts(f.get("scheduledDeparture"))
            arr, arr_s = _ts(to.get("arrival")), _ts(to.get("scheduledArrival"))
            legs.append(S._drop_none({
                "type": "transit", "line": lg.get("displayName") or lg.get("routeShortName"),
                "train": lg.get("tripShortName").lstrip("0") if lg.get("tripShortName") else None,
                "mode": lg.get("mode"), "agency": lg.get("agencyName"), "headsign": lg.get("headsign"),
                "from": f.get("name"), "to": to.get("name"),
                "_dep": dep, "_dep_s": dep_s, "_arr": arr, "_arr_s": arr_s,
                "_tz": f.get("tz"), "_atz": to.get("tz"),
                "dep_delay_min": _delay_min(dep, dep_s) if lg.get("realTime") else None,
                "arr_delay_min": _delay_min(arr, arr_s) if lg.get("realTime") else None,
                "platform": _platform(f), "arr_platform": _platform(to),
                "realtime": bool(lg.get("realTime")),
                "cancelled": bool(lg.get("cancelled")) or None,
            }))
            x = legs[-1]
            # Real-time feeds sometimes predict impossible early arrivals (seen: tram 66
            # Bonn->Siegburg 'arriving' 20 min early = a 5-min ride for a 25-min trip); MOTIS
            # then plans transfers on it.  Flag instead of silently trusting it.
            ride_s = (arr_s - dep_s).total_seconds() if arr_s and dep_s else None
            ride_rt = (arr - dep).total_seconds() if arr and dep else None
            if x.get("realtime") and ride_s and ride_rt is not None and ride_s >= 300 \
                    and ride_rt < 0.6 * ride_s and ride_s - ride_rt >= 300:
                x["suspect_realtime"] = True
        suspect = any(lg.get("suspect_realtime") for lg in legs)
        its.append({"_start": _ts(it.get("startTime")), "_end": _ts(it.get("endTime")),
                    "duration_s": it.get("duration"), "transfers": it.get("transfers"),
                    "legs": legs, **({"suspect_realtime": True} if suspect else {})})
    its.sort(key=lambda i: bool(i.get("suspect_realtime")))  # doubtful options last
    notes = [n for n in (on, dn) if n]
    ref = t or now0
    if t and t < now0 - timedelta(minutes=10):
        notes.append("requested time is in the past")
    if its:
        gap = ((its[0]["_start"] - ref) if not arrival_time else (ref - its[0]["_end"]))
        limit = max(timedelta(hours=4), timedelta(seconds=0.15 * (its[0]["duration_s"] or 0)))
        if gap > limit:  # long-distance trips legitimately start hours later
            notes.append(f"⚠ first option is {S.fmt_duration(gap.total_seconds())} "
                         f"{'after' if not arrival_time else 'before'} the requested time: the "
                         "feed may lack trips for that day (e.g. after a timetable change); "
                         "cross-check with compute_route")
    if t and t > now0 + timedelta(days=21):
        notes.append("⚠ far ahead: Transitous only has timetables the operators already "
                     "published (DB data typically ends at the December timetable change); "
                     "compute_route (Google) is more complete for future dates")
    notes += [ATTRIB, "no fares (use compute_route for Google's fare estimate)"]
    res = {"origin": olab, "destination": dlab, "itineraries": its, "notes": notes, "_now": now}
    if format == "json":
        def fin(o):
            if isinstance(o, list):
                return [fin(x) for x in o]
            if isinstance(o, dict):
                r = {}
                for k, v in o.items():
                    if k in ("_tz", "_atz", "_now"):
                        continue
                    tzk = o.get("_atz" if k.startswith("_arr") or k == "_end" else "_tz") or C.default_tz()
                    r[k.lstrip("_")] = S.local_iso(v, tzk) if isinstance(v, datetime) else fin(v)
                return r
            return o
        return fin({k: v for k, v in res.items() if k != "_now"})
    return _conn_text(res)


def _conn_text(res: dict) -> str:
    now = res["_now"]
    lines = [f"LIVE {res['origin']} → {res['destination']} (real-time where available)"]
    if not res["itineraries"]:
        lines.append("No connection found.")
    for i, it in enumerate(res["itineraries"], 1):
        tr = [lg for lg in it["legs"] if lg["type"] == "transit"]
        tz0 = (tr[0].get("_tz") if tr else None) or C.default_tz()
        tz1 = (tr[-1].get("_atz") if tr else None) or tz0
        flags = []
        if any(lg.get("cancelled") for lg in tr):
            flags.append("✗ contains a CANCELLED leg")
        if it.get("suspect_realtime"):
            flags.append("⚠ relies on an implausible real-time prediction; may not work")
        lines.append(f"{i}. {_clock(it['_start'], tz0, now)} → {_clock(it['_end'], tz1, now)} "
                     f"({S.fmt_duration(it['duration_s'])}) · {it['transfers']} transfer"
                     f"{'s' if it['transfers'] != 1 else ''}" + (" · " + " · ".join(flags) if flags else ""))
        parts = []
        for lg in it["legs"]:
            if lg["type"] == "walk":
                if (lg.get("duration_s") or 0) >= 60:
                    parts.append(f"walk {S.fmt_duration(lg['duration_s'])}")
                continue
            dz, az = lg.get("_tz") or tz0, lg.get("_atz") or lg.get("_tz") or tz0
            dep = _clock(lg.get("_dep_s") or lg.get("_dep"), dz, now)
            arr = _clock(lg.get("_arr_s") or lg.get("_arr"), az, now)
            dd, ad = lg.get("dep_delay_min"), lg.get("arr_delay_min")
            dep += f" +{dd}" if dd and dd > 0 else f" {dd}" if dd and dd < 0 else ""
            arr += f" +{ad}" if ad and ad > 0 else f" {ad}" if ad and ad < 0 else ""
            if lg.get("suspect_realtime"):
                arr += " ⚠implausible"
            name = lg.get("line") or "?"
            if lg.get("train") and lg["train"] not in name:
                name += f" ({lg['train']})"
            plat = f" Gl. {lg['platform']}" if lg.get("platform") else ""
            rt = "" if lg.get("realtime") else " [sched]"
            x = " ✗CANCELLED" if lg.get("cancelled") else ""
            parts.append(f"{name}{x} {lg.get('from')} {dep}{plat} → {lg.get('to')} {arr}{rt}")
        lines.append("   " + " → ".join(parts))
    for n in res["notes"]:
        lines.append(f"note: {n}")
    return "\n".join(lines)
