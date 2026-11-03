"""Command line: python -m gmaps_routes {route,compare,matrix,places,geocode,decode} ...

Output is compact text by default; --json for normalised JSON, --raw for the API response."""
from __future__ import annotations

import argparse
import json
import sys

from . import client as C
from .summary import decode_polyline


def _fmt(p):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", dest="format", action="store_const", const="json")
    g.add_argument("--raw", dest="format", action="store_const", const="raw")
    p.set_defaults(format="text")
    p.add_argument("--lang", help="BCP-47, e.g. en, de, zh-CN (default: GMAPS_LANG or en)")
    p.add_argument("--region", help="ccTLD region code, e.g. de")


def _time(p):
    p.add_argument("--depart", help="ISO time (naive = GMAPS_TZ), 'HH:MM', '+45m', or 'now'")
    p.add_argument("--arrive", help="arrive-by time (TRANSIT; compare: all modes)")


def _opts(p):
    p.add_argument("--avoid", nargs="*", help="tolls highways ferries indoor")
    p.add_argument("--transit-modes", nargs="*", help="BUS SUBWAY TRAIN LIGHT_RAIL RAIL")
    p.add_argument("--transit-pref", choices=list(C.TRANSIT_ROUTING))
    p.add_argument("--routing-pref", help="DRIVE/TWO_WHEELER: TRAFFIC_UNAWARE|TRAFFIC_AWARE|TRAFFIC_AWARE_OPTIMAL")
    p.add_argument("--traffic-model", help="BEST_GUESS|PESSIMISTIC|OPTIMISTIC")
    p.add_argument("--units", default="METRIC", choices=["METRIC", "IMPERIAL"])
    p.add_argument("--extra-json", help="JSON deep-merged into the request body")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gmaps_routes", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("route", help="A -> B (computeRoutes)")
    r.add_argument("origin"); r.add_argument("destination")
    r.add_argument("--mode", default="TRANSIT", type=str.upper, choices=C.TRAVEL_MODES)
    r.add_argument("--via", nargs="*", help="intermediate stops (prefix 'via:' = pass through)")
    r.add_argument("--optimize", action="store_true", help="optimise intermediate order")
    r.add_argument("--no-alternatives", action="store_true")
    r.add_argument("--max", type=int, help="max routes shown")
    r.add_argument("--steps", action="store_true", help="turn-by-turn steps")
    r.add_argument("--polyline", action="store_true")
    r.add_argument("--geojson", action="store_true")
    r.add_argument("--extra-compute", nargs="*", help="TOLLS FUEL_CONSUMPTION ...")
    r.add_argument("--fields", help="override X-Goog-FieldMask")
    r.add_argument("--dry-run", action="store_true")
    _time(r); _opts(r); _fmt(r)

    c = sub.add_parser("compare", help="same trip across modes (parallel)")
    c.add_argument("origin"); c.add_argument("destination")
    c.add_argument("--modes", nargs="*", type=str.upper, default=["TRANSIT", "BICYCLE", "WALK", "DRIVE"])
    _time(c); _opts(c); _fmt(c)

    m = sub.add_parser("matrix", help="many-to-many travel times")
    m.add_argument("--origins", nargs="+", required=True)
    m.add_argument("--destinations", nargs="+", required=True)
    m.add_argument("--mode", default="TRANSIT", type=str.upper, choices=C.TRAVEL_MODES)
    m.add_argument("--fields"); m.add_argument("--dry-run", action="store_true")
    _time(m); _opts(m); _fmt(m)

    pl = sub.add_parser("places", help="Places (New) text search")
    pl.add_argument("query")
    pl.add_argument("--near", help="lat,lng"); pl.add_argument("--radius", type=float, default=3000)
    pl.add_argument("--max", type=int, default=8); pl.add_argument("--type")
    pl.add_argument("--open-now", action="store_true", default=None)
    pl.add_argument("--min-rating", type=float)
    pl.add_argument("--rank-by", choices=["RELEVANCE", "DISTANCE"])
    pl.add_argument("--detailed", action="store_true", help="rating/hours/phone/web (Enterprise SKU)")
    pl.add_argument("--fields"); pl.add_argument("--dry-run", action="store_true")
    _fmt(pl)

    g = sub.add_parser("geocode")
    g.add_argument("query", nargs="?"); g.add_argument("--latlng"); g.add_argument("--place-id")
    g.add_argument("--components"); g.add_argument("--dry-run", action="store_true")
    _fmt(g)

    d = sub.add_parser("decode", help="decode an encoded polyline")
    d.add_argument("polyline")
    return ap


def _opt_kwargs(a) -> dict:
    return dict(avoid=a.avoid, transit_modes=a.transit_modes, transit_routing=a.transit_pref,
                routing_preference=a.routing_pref, traffic_model=a.traffic_model, units=a.units,
                language=a.lang, region=a.region,
                extra_request=json.loads(a.extra_json) if a.extra_json else None)


def run(a):
    if a.cmd == "route":
        return C.compute_routes(
            a.origin, a.destination, intermediates=a.via, travel_mode=a.mode,
            departure_time=a.depart, arrival_time=a.arrive, alternatives=not a.no_alternatives,
            max_routes=a.max, detail="steps" if a.steps else "brief", format=a.format,
            fields=a.fields, dry_run=a.dry_run, optimize_waypoint_order=a.optimize,
            polyline=a.polyline, geojson=a.geojson, extra_computations=a.extra_compute,
            **_opt_kwargs(a))
    if a.cmd == "compare":
        return C.compare_modes(a.origin, a.destination, modes=a.modes, departure_time=a.depart,
                               arrival_time=a.arrive, format=a.format, **_opt_kwargs(a))
    if a.cmd == "matrix":
        return C.compute_route_matrix(a.origins, a.destinations, travel_mode=a.mode,
                                      departure_time=a.depart, arrival_time=a.arrive,
                                      fields=a.fields, format=a.format, dry_run=a.dry_run,
                                      **_opt_kwargs(a))
    if a.cmd == "places":
        return C.search_places(a.query, near=a.near, radius_m=a.radius, max_results=a.max,
                               included_type=a.type, open_now=a.open_now, min_rating=a.min_rating,
                               rank_by=a.rank_by, language=a.lang, region=a.region,
                               detailed=a.detailed, fields=a.fields, format=a.format,
                               dry_run=a.dry_run)
    if a.cmd == "geocode":
        return C.geocode(a.query, latlng=a.latlng, place_id=a.place_id, language=a.lang,
                         region=a.region, components=a.components, format=a.format,
                         dry_run=a.dry_run)
    if a.cmd == "decode":
        return decode_polyline(a.polyline)
    raise SystemExit(2)


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        result = run(a)
    except (C.GoogleMapsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    return 0
