"""Offline tests.  Fixtures in tests/fixtures/ are real Routes API responses captured on
2026-11-03 (Bonn area, slim field mask), so parsing is tested against what the API returns."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gmaps_routes import client as C
from gmaps_routes import render as R
from gmaps_routes import summary as S

FIX = Path(__file__).parent / "fixtures"
TZ = "Europe/Berlin"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def test_transit_fixture():
    raw = load("transit_bonn_dueren")
    dep = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc)
    routes = [S.summarize_route(r, tz=TZ, mode="TRANSIT", depart_ref=dep, arrive_ref=None)
              for r in raw["routes"]]
    assert len(routes) == 6
    for r in routes:
        transit = [s for s in r["segments"] if s["type"] == "transit"]
        assert r["transfers"] == len(transit) - 1
        assert r["leave"] <= transit[0]["_dep"] and r["arrive"] >= transit[-1]["_arr"]
        assert r["duration_s"] == (r["arrive"] - r["leave"]).total_seconds()
        assert r["wait_before_s"] >= 0
        # consecutive walk steps are merged
        assert all(not (a["type"] == b["type"] == "walk")
                   for a, b in zip(r["segments"], r["segments"][1:]))
        for s in transit[1:]:
            assert s["transfer_wait_s"] >= 0
    first = routes[0]
    assert [s["line"] for s in first["segments"] if s["type"] == "transit"] == ["RE5", "RE9"]
    out = S.finalize(first, TZ)
    assert out["leave"].endswith("+01:00") and "_tz" not in json.dumps(out)


def test_transit_text_render():
    raw = load("transit_bonn_dueren")
    now = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc)
    routes = [S.summarize_route(r, tz=TZ, mode="TRANSIT", depart_ref=now, arrive_ref=None)
              for r in raw["routes"]]
    res = {"travel_mode": "TRANSIT", "routes": routes, "notes": [], "maps_url": "u", "_tz": TZ,
           "_query": {"origin": "A", "destination": "B", "now": now, "depart_at": now,
                      "anchor": now}}
    txt = R.routes_text(res)
    assert "RE5 Bonn" in txt and "transfer" in txt and "​" not in txt
    assert len(txt) < 3000  # 6 options stay compact


def test_bike_fixture_steps():
    raw = load("bike_bonn_koeln")
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    r = S.summarize_route(raw["routes"][0], tz=TZ, mode="BICYCLE", depart_ref=now,
                          arrive_ref=None, with_steps=True)
    assert r["distance_m"] > 25000 and r["arrive"] > r["leave"]
    assert r["steps"] and all("\n" not in (s.get("instruction") or "") for s in r["steps"])
    assert "segments" not in r


def test_arrive_by_non_transit():
    raw = load("bike_bonn_koeln")
    arr = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    r = S.summarize_route(raw["routes"][0], tz=TZ, mode="BICYCLE", depart_ref=None, arrive_ref=arr)
    assert r["arrive"] == arr and (arr - r["leave"]).total_seconds() == r["duration_s"]


def test_time_parsing():
    assert C.parse_time("2026-11-04T08:30:00+01:00") == datetime(2026, 11, 4, 7, 30, tzinfo=timezone.utc)
    assert C.parse_time("2026-11-04T08:30") == datetime(2026, 11, 4, 7, 30, tzinfo=timezone.utc)
    assert C.parse_time("now") is None and C.parse_time(None) is None
    assert C.parse_time("+45m") > datetime.now(timezone.utc)
    hm = C.parse_time("08:30")
    assert hm is not None and hm > datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=6)


def test_waypoints():
    assert C.waypoint("50.1, 7.2")["location"]["latLng"] == {"latitude": 50.1, "longitude": 7.2}
    assert C.waypoint("place_id:XYZ") == {"placeId": "XYZ"}
    assert C.waypoint("via:Bonn") == {"address": "Bonn", "via": True}


def test_validation():
    with pytest.raises(ValueError):
        C.compute_routes("A", "B", travel_mode="BICYCLE", arrival_time="+30m", dry_run=True)
    with pytest.raises(ValueError):
        C.compute_routes("A", "B", intermediates=["C"], dry_run=True)  # TRANSIT + via
    with pytest.raises(ValueError):
        C.compute_routes("A", "B", intermediates=["via:C"], optimize_waypoint_order=True,
                         travel_mode="DRIVE", dry_run=True)
    with pytest.raises(ValueError):
        C.compute_route_matrix(["a"] * 11, ["b"] * 10, travel_mode="TRANSIT", dry_run=True)
    with pytest.raises(ValueError):
        C.compute_route_matrix(["a"] * 26, ["b"] * 25, travel_mode="BICYCLE", dry_run=True)


def test_request_shape():
    d = C.compute_routes("A", "B", travel_mode="DRIVE", dry_run=True)
    assert d["body"]["routingPreference"] == "TRAFFIC_AWARE"
    assert "polylineQuality" not in d["body"] and "routes.polyline" not in d["field_mask"]
    b = C.compute_routes("A", "B", travel_mode="BICYCLE", dry_run=True)
    assert "routingPreference" not in b["body"] and "steps" not in b["field_mask"]
    t = C.compute_routes("A", "B", dry_run=True)
    assert "routes.legs.steps.transitDetails" in t["field_mask"]
    p = C.compute_routes("A", "B", travel_mode="WALK", polyline=True, dry_run=True)
    assert p["body"]["polylineQuality"] == "OVERVIEW" and "routes.polyline" in p["field_mask"]


def test_polyline():
    assert S.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")[0] == [38.5, -120.2]


def test_walk_only_transit_route():
    route = {"duration": "300s", "distanceMeters": 370,
             "legs": [{"steps": [{"travelMode": "WALK", "distanceMeters": 370, "staticDuration": "300s"}]}]}
    arr = datetime(2026, 11, 4, 8, 15, tzinfo=timezone.utc)
    r = S.summarize_route(route, tz=TZ, mode="TRANSIT", depart_ref=None, arrive_ref=arr)
    assert r["walk_only"] and r["arrive"] == arr and (arr - r["leave"]).total_seconds() == 300
    assert "walk only" in R.route_line(r, "TRANSIT", TZ, arr, live=False)


def test_transit_mode_filter():
    raw = load("transit_bonn_dueren")
    rs = [S.summarize_route(r, tz=TZ, mode="TRANSIT", depart_ref=None, arrive_ref=None)
          for r in raw["routes"]]
    assert all(S.uses_only(r, {"RAIL"}) or not S.uses_only(r, {"TRAIN"}) for r in rs)
    assert any(S.uses_only(r, {"TRAIN"}) for r in rs)
    assert not any(S.uses_only(r, {"BUS"}) for r in rs if any(
        s["type"] == "transit" and s.get("vehicle") == "HEAVY_RAIL" for s in r["segments"]))


def test_mode_aliases_and_relative_days():
    assert C.norm_mode("bike") == "BICYCLE" and C.norm_mode("car") == "DRIVE"
    with pytest.raises(ValueError):
        C.norm_mode("teleport")
    t = C.parse_time("tomorrow 08:30")
    assert t is not None and t > datetime.now(timezone.utc)


def test_partial_match_detection():
    body = {"origin": {"address": "Bonn Hbf"}, "destination": {"address": "Köln Hbf"},
            "intermediates": [{"location": {}}, {"address": "Brühl", "via": True}]}
    raw = {"geocodingResults": {
        "origin": {"placeId": "x", "type": ["transit_station"]},
        "destination": {"placeId": "y", "type": ["locality"], "partialMatch": True},
        "intermediates": [{"intermediateWaypointRequestIndex": 1, "partialMatch": True}]}}
    assert C._partial_waypoints(raw, body) == [("destination", None, "Köln Hbf"),
                                                ("intermediates", 1, "Brühl")]


def test_legacy_zone_names_and_labels():
    assert S.zone("Europe/Kiev").key in ("Europe/Kiev", "Europe/Kyiv")
    assert S.zone("Not/AZone").key == "Europe/Berlin"
    t = datetime(2026, 11, 4, 12, 26, tzinfo=timezone.utc)
    assert R.tt(t, "Europe/Paris", None, "Europe/London") == "13:26 CET"
    assert R.tt(t, "Europe/Amsterdam", None, "Europe/Berlin") == "13:26"  # same offset: no label


def test_wall_time_detection_and_zone():
    assert C.is_wall_time("08:30") and C.is_wall_time("tomorrow 08:30")
    assert C.is_wall_time("2026-11-04T08:30")
    assert not C.is_wall_time("2026-11-04T08:30+00:00") and not C.is_wall_time("+30m")
    assert not C.is_wall_time("now") and not C.is_wall_time(None)
    assert C.parse_time("2026-11-04T08:30", "Europe/London") == datetime(2026, 11, 4, 8, 30, tzinfo=timezone.utc)


def test_cet_fast_path_needs_no_lookup(monkeypatch):
    monkeypatch.setattr(C, "_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert C.tz_at(50.73, 7.10) == C.default_tz()  # Bonn
    assert C.tz_at(48.86, 2.35) == C.default_tz()  # Paris (same CET rules)


def test_openboards_parser():
    from gmaps_routes import live as L
    raw = load("openboards_departures_sample")
    res = L.parse_db_board(raw, {"name": "Bonn Hbf"}, False, 10)
    res["_now"] = datetime(2026, 11, 3, 11, 0, tzinfo=timezone.utc)
    e = res["entries"]
    assert [x.get("cancelled") for x in e] == [True, None]  # sorted by scheduled time
    ice = e[1]
    assert ice["delay_min"] == 7 and ice["platform"] == "3" and ice["platform_planned"] == "2"
    assert ice["messages"] == ["Delay of a preceding train"] and "Frankfurt(Main)Hbf (cancelled)" in ice["via"]
    txt = L._board_text(res)
    assert "RE 5 (28516)" in txt and "CANCELLED" in txt and "Construction work" in txt


def test_live_ids_and_platforms():
    from gmaps_routes import live as L
    assert L._DHID.search("de-DELFI_de:05314:61101:7:73").group(1) == "de:05314:61101"
    assert L.find_stop("de:05314:61101")["dhid"] == "de:05314:61101"
    assert L.find_stop("8000044")["eva"] == "8000044"
    assert L._platform({"description": "Bahnsteig Gleis 2", "track": "80"}) == "2"
    assert L._platform({"track": "4a"}) == "4a"
