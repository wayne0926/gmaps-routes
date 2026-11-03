"""Offline tests: request building + summariser.

NOTE: the fixture below is hand-written from Google's documented Routes API v2 schema,
NOT a captured live response.  It checks my parsing logic only; it proves nothing about what
the live API returns.  Run live checks with a real key (see README).
"""
import pytest

from gmaps_routes import client as C
from gmaps_routes import summary as S

FIXTURE = {"routes": [{
    "duration": "1500s", "staticDuration": "1500s", "distanceMeters": 5200,
    "localizedValues": {"distance": {"text": "5.2 km"}, "duration": {"text": "25 mins"},
                        "transitFare": {"text": "€3.20"}},
    "legs": [{"distanceMeters": 5200, "duration": "1500s", "steps": [
        {"travelMode": "WALK", "distanceMeters": 300, "staticDuration": "240s",
         "navigationInstruction": {"maneuver": "DEPART", "instructions": "Walk to Hbf"}},
        {"travelMode": "TRANSIT", "distanceMeters": 4500, "staticDuration": "900s",
         "transitDetails": {
             "stopDetails": {"departureStop": {"name": "Bonn Hbf"}, "arrivalStop": {"name": "Poppelsdorf"},
                             "departureTime": "2026-11-04T07:05:00Z", "arrivalTime": "2026-11-04T07:20:00Z"},
             "localizedValues": {"departureTime": {"timeZone": "Europe/Berlin"},
                                 "arrivalTime": {"timeZone": "Europe/Berlin"}},
             "headsign": "Bad Godesberg", "stopCount": 5,
             "transitLine": {"nameShort": "66", "name": "Bonn-Siegburg", "vehicle": {"type": "SUBWAY"},
                             "agencies": [{"name": "SWB"}]}}},
        {"travelMode": "WALK", "distanceMeters": 400, "staticDuration": "360s"}]}]}]}


def test_summary_transit():
    r = S.summarize_routes(FIXTURE)[0]
    assert r["transfers"] == 0 and r["lines"] == ["66"] and r["fare"] == "€3.20"
    assert r["walking_m"] == 700 and r["first_departure"].endswith("08:05")  # UTC+1 in November
    assert "Subway 66 Bonn Hbf" in r["headline"]


def test_time_parsing():
    assert C.parse_time("2026-11-04T08:30:00+01:00") == "2026-11-04T07:30:00Z"
    assert C.parse_time("2026-11-04T08:30") == "2026-11-04T07:30:00Z"  # Europe/Berlin default
    assert C.parse_time("now") is None and C.parse_time(None) is None


def test_waypoints():
    assert C.waypoint("50.1, 7.2")["location"]["latLng"] == {"latitude": 50.1, "longitude": 7.2}
    assert C.waypoint("place_id:XYZ") == {"placeId": "XYZ"}
    assert C.waypoint("via:Bonn")["via"] is True


def test_validation():
    with pytest.raises(ValueError):
        C.compute_routes("A", "B", travel_mode="BICYCLE", arrival_time="+30m", dry_run=True)
    with pytest.raises(ValueError):
        C.compute_routes("A", "B", intermediates=["via:C"], optimize_waypoint_order=True,
                         travel_mode="DRIVE", dry_run=True)
    with pytest.raises(ValueError):
        C.compute_route_matrix(["a"] * 11, ["b"] * 10, travel_mode="TRANSIT", dry_run=True)


def test_drive_defaults_traffic_aware():
    b = C.compute_routes("A", "B", travel_mode="DRIVE", dry_run=True)["body"]
    assert b["routingPreference"] == "TRAFFIC_AWARE"
    assert "routingPreference" not in C.compute_routes("A", "B", travel_mode="BICYCLE", dry_run=True)["body"]


def test_polyline():
    assert S.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")[0] == [38.5, -120.2]
