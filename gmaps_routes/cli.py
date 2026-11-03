"""Command line: python -m gmaps_routes {route,compare,matrix,geocode,places,decode} ..."""
from __future__ import annotations

import argparse
import json
import sys

from . import client as C
from .summary import decode_polyline


def _common_time(p):
    p.add_argument("--depart", help="ISO time (naive = GMAPS_TZ, default Europe/Berlin), '+45m', or 'now'")
    p.add_argument("--arrive", help="TRANSIT only; mutually exclusive with --depart")


def _common(p):
    p.add_argument("--units", default="METRIC", choices=["METRIC", "IMPERIAL"])
    p.add_argument("--lang", help="BCP-47 language code, e.g. en, de, zh-CN")
    p.add_argument("--region", help="ccTLD region code, e.g. de")
    p.add_argument("--avoid", nargs="*", default=[], help="tolls highways ferries indoor tunnels")
    p.add_argument("--transit-modes", nargs="*", help="BUS SUBWAY TRAIN LIGHT_RAIL RAIL")
    p.add_argument("--transit-pref", choices=list(C.TRANSIT_ROUTING))
    p.add_argument("--routing-pref", help="DRIVE/TWO_WHEELER: TRAFFIC_UNAWARE|TRAFFIC_AWARE|TRAFFIC_AWARE_OPTIMAL")
    p.add_argument("--traffic-model", help="BEST_GUESS|PESSIMISTIC|OPTIMISTIC")
    p.add_argument("--extra-compute", nargs="*", help="TOLLS FUEL_CONSUMPTION TRAFFIC_ON_POLYLINE ...")
    p.add_argument("--extra-json", help="JSON merged into the request body (escape hatch)")
    p.add_argument("--output", default="both", choices=["both", "summary", "raw"])
    p.add_argument("--dry-run", action="store_true", help="print the request, do not call the API")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gmaps_routes", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("route", help="computeRoutes")
    r.add_argument("origin"); r.add_argument("destination")
    r.add_argument("--via", nargs="*", help="intermediate waypoints (prefix 'via:' to pass without stopping)")
    r.add_argument("--mode", default="TRANSIT", choices=C.TRAVEL_MODES)
    r.add_argument("--no-alternatives", action="store_true")
    r.add_argument("--optimize", action="store_true", help="optimizeWaypointOrder")
    r.add_argument("--polyline-quality", default="HIGH_QUALITY", choices=["HIGH_QUALITY", "OVERVIEW"])
    r.add_argument("--geojson", action="store_true")
    r.add_argument("--reference-routes", nargs="*", help="FUEL_EFFICIENT SHORTER_DISTANCE")
    r.add_argument("--fields", help="override X-Goog-FieldMask")
    _common_time(r); _common(r)

    c = sub.add_parser("compare", help="same trip across several modes")
    c.add_argument("origin"); c.add_argument("destination")
    c.add_argument("--modes", nargs="*", default=["TRANSIT", "BICYCLE", "WALK", "DRIVE"])
    _common_time(c); _common(c)

    m = sub.add_parser("matrix", help="computeRouteMatrix")
    m.add_argument("--origins", nargs="+", required=True)
    m.add_argument("--destinations", nargs="+", required=True)
    m.add_argument("--mode", default="TRANSIT", choices=C.TRAVEL_MODES)
    m.add_argument("--fields")
    _common_time(m); _common(m)

    g = sub.add_parser("geocode")
    g.add_argument("query", nargs="?"); g.add_argument("--latlng"); g.add_argument("--place-id")
    g.add_argument("--lang"); g.add_argument("--region"); g.add_argument("--components")
    g.add_argument("--dry-run", action="store_true")

    pl = sub.add_parser("places", help="Places API (New) text search")
    pl.add_argument("query")
    pl.add_argument("--near", help="lat,lng"); pl.add_argument("--radius", type=float, default=5000)
    pl.add_argument("--max", type=int, default=10); pl.add_argument("--type")
    pl.add_argument("--open-now", action="store_true", default=None)
    pl.add_argument("--min-rating", type=float); pl.add_argument("--lang"); pl.add_argument("--region")
    pl.add_argument("--detailed", action="store_true", help="add rating/hours/phone/website (higher SKU)")
    pl.add_argument("--fields"); pl.add_argument("--dry-run", action="store_true")

    d = sub.add_parser("decode", help="decode an encoded polyline")
    d.add_argument("polyline")
    return ap


def _json_arg(s):
    return json.loads(s) if s else None


def run(args) -> dict | list:
    if args.cmd == "route":
        return C.compute_routes(
            args.origin, args.destination, intermediates=args.via, travel_mode=args.mode,
            departure_time=args.depart, arrival_time=args.arrive,
            alternatives=not args.no_alternatives, transit_modes=args.transit_modes,
            transit_routing=args.transit_pref, routing_preference=args.routing_pref,
            traffic_model=args.traffic_model, avoid=args.avoid, units=args.units,
            language=args.lang, region=args.region, optimize_waypoint_order=args.optimize,
            polyline_quality=args.polyline_quality, geojson=args.geojson,
            extra_computations=args.extra_compute, reference_routes=args.reference_routes,
            extra_request=_json_arg(args.extra_json), fields=args.fields, output=args.output,
            dry_run=args.dry_run)
    if args.cmd == "compare":
        if args.dry_run:
            return {m: C.compute_routes(args.origin, args.destination, travel_mode=m,
                                        departure_time=args.depart,
                                        arrival_time=args.arrive if m.upper() == "TRANSIT" else None,
                                        alternatives=False, dry_run=True) for m in args.modes}
        return C.compare_modes(
            args.origin, args.destination, modes=args.modes, departure_time=args.depart,
            arrival_time=args.arrive, output=args.output, transit_modes=args.transit_modes,
            transit_routing=args.transit_pref, routing_preference=args.routing_pref,
            traffic_model=args.traffic_model, avoid=args.avoid, units=args.units,
            language=args.lang, region=args.region, extra_computations=args.extra_compute,
            extra_request=_json_arg(args.extra_json))
    if args.cmd == "matrix":
        return C.compute_route_matrix(
            args.origins, args.destinations, travel_mode=args.mode, departure_time=args.depart,
            arrival_time=args.arrive, transit_modes=args.transit_modes,
            transit_routing=args.transit_pref, routing_preference=args.routing_pref,
            traffic_model=args.traffic_model, avoid=args.avoid, units=args.units,
            language=args.lang, region=args.region, extra_computations=args.extra_compute,
            extra_request=_json_arg(args.extra_json), fields=args.fields, output=args.output,
            dry_run=args.dry_run)
    if args.cmd == "geocode":
        return C.geocode(args.query, latlng=args.latlng, place_id=args.place_id, language=args.lang,
                         region=args.region, components=args.components, dry_run=args.dry_run)
    if args.cmd == "places":
        return C.search_places(args.query, near=args.near, radius_m=args.radius, max_results=args.max,
                               included_type=args.type, open_now=args.open_now,
                               min_rating=args.min_rating, language=args.lang, region=args.region,
                               detailed=args.detailed, fields=args.fields, dry_run=args.dry_run)
    if args.cmd == "decode":
        return decode_polyline(args.polyline)
    raise SystemExit(2)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except (C.GoogleMapsError, ValueError) as e:
        print(json.dumps({"error": str(e), "payload": getattr(e, "payload", None)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
