"""MCP server (stdio).  Run:  python -m gmaps_routes.mcp_server

Needs GOOGLE_MAPS_API_KEY in the environment.  Works with mcp 2.x (MCPServer) and 1.x (FastMCP).
"""
from __future__ import annotations

from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import client as C
from .summary import decode_polyline as _decode

mcp = _Server(
    "gmaps-routes",
    instructions=(
        "Google Maps routing. Prefer compute_route for A->B (transit, bicycle, walk, drive, "
        "two-wheeler; alternatives, waypoints, departure/arrival time), compare_modes to pick a "
        "mode, compute_route_matrix for many-to-many travel times. Times: ISO-8601 (naive = "
        "GMAPS_TZ, default Europe/Berlin) or '+30m'. Waypoints: address, 'lat,lng', "
        "'place_id:...', or prefix 'via:'."))


def _wrap(fn, *a, **k) -> dict:
    try:
        return fn(*a, **k)
    except (C.GoogleMapsError, ValueError) as e:
        # surfaced as a tool error with Google's own message (key is scrubbed)
        raise RuntimeError(str(e)) from None


@mcp.tool()
def compute_route(
    origin: str, destination: str, travel_mode: str = "TRANSIT",
    intermediates: list[str] | None = None,
    departure_time: str | None = None, arrival_time: str | None = None,
    alternatives: bool = True,
    transit_modes: list[str] | None = None, transit_routing: str | None = None,
    routing_preference: str | None = None, traffic_model: str | None = None,
    avoid: list[str] | None = None, units: str = "METRIC",
    language: str | None = None, region: str | None = None,
    optimize_waypoint_order: bool = False, polyline_quality: str = "HIGH_QUALITY",
    geojson: bool = False, extra_computations: list[str] | None = None,
    reference_routes: list[str] | None = None, extra_request: dict[str, Any] | None = None,
    fields: str | None = None, output: str = "both", dry_run: bool = False,
) -> dict:
    """Routes API computeRoutes. travel_mode: TRANSIT|BICYCLE|WALK|DRIVE|TWO_WHEELER.
    transit_modes: BUS SUBWAY TRAIN LIGHT_RAIL RAIL; transit_routing: LESS_WALKING|FEWER_TRANSFERS.
    avoid: tolls highways ferries indoor tunnels. arrival_time only for TRANSIT.
    output: both (readable summary + full raw API JSON) | summary | raw.
    extra_request is deep-merged into the request body for any option not exposed here."""
    return _wrap(C.compute_routes, origin, destination, intermediates=intermediates,
                 travel_mode=travel_mode, departure_time=departure_time, arrival_time=arrival_time,
                 alternatives=alternatives, transit_modes=transit_modes,
                 transit_routing=transit_routing, routing_preference=routing_preference,
                 traffic_model=traffic_model, avoid=avoid, units=units, language=language,
                 region=region, optimize_waypoint_order=optimize_waypoint_order,
                 polyline_quality=polyline_quality, geojson=geojson,
                 extra_computations=extra_computations, reference_routes=reference_routes,
                 extra_request=extra_request, fields=fields, output=output, dry_run=dry_run)


@mcp.tool()
def compare_modes(
    origin: str, destination: str, modes: list[str] | None = None,
    departure_time: str | None = None, arrival_time: str | None = None,
    language: str | None = None, region: str | None = None, units: str = "METRIC",
    output: str = "summary",
) -> dict:
    """Same trip across several modes (default TRANSIT, BICYCLE, WALK, DRIVE); returns the best
    route per mode and a fastest-first ranking."""
    return _wrap(C.compare_modes, origin, destination, modes=tuple(modes or C.TRAVEL_MODES[:4]),
                 departure_time=departure_time, arrival_time=arrival_time, language=language,
                 region=region, units=units, output=output)


@mcp.tool()
def compute_route_matrix(
    origins: list[str], destinations: list[str], travel_mode: str = "TRANSIT",
    departure_time: str | None = None, arrival_time: str | None = None,
    transit_modes: list[str] | None = None, transit_routing: str | None = None,
    routing_preference: str | None = None, traffic_model: str | None = None,
    avoid: list[str] | None = None, units: str = "METRIC", language: str | None = None,
    region: str | None = None, extra_computations: list[str] | None = None,
    extra_request: dict[str, Any] | None = None, fields: str | None = None,
    output: str = "both", dry_run: bool = False,
) -> dict:
    """Routes API computeRouteMatrix (origins x destinations). Limits: 625 elements, 100 for
    TRANSIT or TRAFFIC_AWARE_OPTIMAL; 50 combined origins+destinations when given as addresses."""
    return _wrap(C.compute_route_matrix, origins, destinations, travel_mode=travel_mode,
                 departure_time=departure_time, arrival_time=arrival_time,
                 transit_modes=transit_modes, transit_routing=transit_routing,
                 routing_preference=routing_preference, traffic_model=traffic_model, avoid=avoid,
                 units=units, language=language, region=region,
                 extra_computations=extra_computations, extra_request=extra_request,
                 fields=fields, output=output, dry_run=dry_run)


@mcp.tool()
def geocode(query: str | None = None, latlng: str | None = None, place_id: str | None = None,
            language: str | None = None, region: str | None = None,
            components: str | None = None) -> dict:
    """Geocoding API: give exactly one of query (address), latlng ('lat,lng' reverse), place_id."""
    return _wrap(C.geocode, query, latlng=latlng, place_id=place_id, language=language,
                 region=region, components=components)


@mcp.tool()
def search_places(text: str, near: str | None = None, radius_m: float = 5000,
                  max_results: int = 10, included_type: str | None = None,
                  open_now: bool | None = None, min_rating: float | None = None,
                  language: str | None = None, region: str | None = None,
                  detailed: bool = False) -> dict:
    """Places API (New) Text Search. near='lat,lng' biases results. detailed adds rating, opening
    hours, phone, website (higher-priced SKU)."""
    return _wrap(C.search_places, text, near=near, radius_m=radius_m, max_results=max_results,
                 included_type=included_type, open_now=open_now, min_rating=min_rating,
                 language=language, region=region, detailed=detailed)


@mcp.tool()
def decode_polyline(polyline: str) -> list[list[float]]:
    """Decode an encoded polyline (from a route's polyline.encodedPolyline) to [[lat, lng], ...]."""
    return _decode(polyline)


if __name__ == "__main__":
    mcp.run()
