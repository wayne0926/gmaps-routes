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
