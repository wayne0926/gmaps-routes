"""MCP server (stdio).  Run:  python -m gmaps_routes.mcp_server

Needs GOOGLE_MAPS_API_KEY.  Optional: GMAPS_TZ (default Europe/Berlin), GMAPS_LANG (default en).
Tools return compact text by default (cheap on context); format="json" gives normalised JSON.
Works with mcp 2.x (MCPServer) and 1.x (FastMCP).
"""
from __future__ import annotations

import json
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import client as C
from . import live as L

mcp = _Server(
    "gmaps-routes",
    instructions=(
        "Google Maps routing for planning trips (public transit and bicycle first). "
        "compute_route: A->B with alternatives; transit results give leave/arrive times, lines, "
        "stops, transfers, transfer waits and walking. compare_modes: one call for transit vs "
        "bike vs walk vs drive, ranked by arrival (or latest departure for arrive-by). "
        "compute_route_matrix: many origins x destinations. search_places: find a place near "
        "lat,lng; pass its 'place_id:...' as a waypoint. Times: ISO-8601 (naive = local time "
        f"{C.default_tz()}), 'HH:MM', 'tomorrow HH:MM', '+30m', or omit for now. arrival_time ('arrive by') "
        "works for TRANSIT; compare_modes back-computes it for the other modes. Times in the "
        "output are local. TRANSIT cannot take intermediate stops: route each leg. "
        "LIVE data (delays, platforms, cancellations): station_board for a station's "
        "departures/arrivals (DB OpenBoards when DB keys are set, else Transitous), "
        "live_connections for journeys with real-time delays (Transitous). Google's transit "
        "routes are timetable-only; check live_connections or station_board before telling "
        "someone a train is on time."))


# Plain text only: structured output would send every result twice. All tools only read.
_TOOL = dict(structured_output=False,
             annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))


def _out(fn, *a, **k) -> str:
    try:
        r = fn(*a, **k)
    except (C.GoogleMapsError, ValueError) as e:
        # ToolError keeps the message visible to the model (other exceptions are masked)
        raise ToolError(str(e)) from None
    return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, separators=(",", ":"))


@mcp.tool(**_TOOL)
def compute_route(
    origin: str, destination: str, travel_mode: str = "TRANSIT",
    departure_time: str | None = None, arrival_time: str | None = None,
    intermediates: list[str] | None = None, optimize_waypoint_order: bool = False,
    alternatives: bool = True, max_routes: int | None = None, steps: bool = False,
    transit_modes: list[str] | None = None, transit_routing: str | None = None,
    avoid: list[str] | None = None, routing_preference: str | None = None,
    language: str | None = None, region: str | None = None,
    extra_request: dict[str, Any] | None = None, format: str = "text",
) -> str:
    """Route between two places. travel_mode: TRANSIT | BICYCLE | WALK | DRIVE | TWO_WHEELER.
    Places: address/name, 'lat,lng', or 'place_id:...'; intermediates may be prefixed 'via:'
    (pass through without stopping); not for TRANSIT.
    transit_modes: BUS SUBWAY TRAIN LIGHT_RAIL RAIL; transit_routing: LESS_WALKING | FEWER_TRANSFERS.
    steps=true adds turn-by-turn instructions (bike/walk/drive). avoid: tolls highways ferries indoor.
    routing_preference (DRIVE/TWO_WHEELER): TRAFFIC_UNAWARE | TRAFFIC_AWARE (default) | TRAFFIC_AWARE_OPTIMAL.
    format: text | json | raw (full API response, large). extra_request is merged into the API body."""
    return _out(C.compute_routes, origin, destination, intermediates=intermediates,
                travel_mode=travel_mode, departure_time=departure_time,
                arrival_time=arrival_time, alternatives=alternatives, max_routes=max_routes,
                detail="steps" if steps else "brief", format=format,
                optimize_waypoint_order=optimize_waypoint_order, transit_modes=transit_modes,
                transit_routing=transit_routing, avoid=avoid,
                routing_preference=routing_preference, language=language, region=region,
                extra_request=extra_request)


@mcp.tool(**_TOOL)
def compare_modes(
    origin: str, destination: str, modes: list[str] | None = None,
    departure_time: str | None = None, arrival_time: str | None = None,
    transit_modes: list[str] | None = None, transit_routing: str | None = None,
    language: str | None = None, format: str = "text",
) -> str:
    """Same trip in several modes at once (default TRANSIT, BICYCLE, WALK; add DRIVE if
    relevant), best route each, ranked by earliest arrival, or by latest departure when
    arrival_time is given."""
    return _out(C.compare_modes, origin, destination,
                modes=tuple(modes or C.DEFAULT_COMPARE),
                departure_time=departure_time, arrival_time=arrival_time,
                transit_modes=transit_modes, transit_routing=transit_routing,
                language=language, format=format)


@mcp.tool(**_TOOL)
def compute_route_matrix(
    origins: list[str], destinations: list[str], travel_mode: str = "TRANSIT",
    departure_time: str | None = None, arrival_time: str | None = None,
    transit_modes: list[str] | None = None, transit_routing: str | None = None,
    avoid: list[str] | None = None, routing_preference: str | None = None,
    language: str | None = None, format: str = "text",
) -> str:
    """Travel time and distance for every origin x destination pair. Limits: 100 elements
    for TRANSIT, 625 otherwise; at most 50 origins+destinations given as address/place_id.
    TRANSIT durations exclude the initial wait. Addresses are resolved via Places first
    (one lookup per distinct address, cached)."""
    return _out(C.compute_route_matrix, origins, destinations, travel_mode=travel_mode,
                departure_time=departure_time, arrival_time=arrival_time,
                transit_modes=transit_modes, transit_routing=transit_routing, avoid=avoid,
                routing_preference=routing_preference, language=language, format=format)


@mcp.tool(**_TOOL)
def search_places(
    text: str, near: str | None = None, radius_m: float = 3000, max_results: int = 8,
    included_type: str | None = None, open_now: bool | None = None,
    min_rating: float | None = None, rank_by: str | None = None, detailed: bool = False,
    language: str | None = None, format: str = "text",
) -> str:
    """Find places by text ('Bäckerei', 'Uni Bonn Hörsaalzentrum'). near='lat,lng' biases
    to that area and adds distance; rank_by: RELEVANCE | DISTANCE. detailed/open_now/min_rating
    add rating, opening hours, phone, website (pricier Enterprise SKU). Use the returned
    place_id as 'place_id:...' in routing tools."""
    return _out(C.search_places, text, near=near, radius_m=radius_m, max_results=max_results,
                included_type=included_type, open_now=open_now, min_rating=min_rating,
                rank_by=rank_by, detailed=detailed, language=language, format=format)


@mcp.tool(**_TOOL)
def geocode(query: str | None = None, latlng: str | None = None, place_id: str | None = None,
            language: str | None = None, region: str | None = None,
            format: str = "text") -> str:
    """Address -> coordinates (query), coordinates -> address (latlng='lat,lng'), or
    place_id -> address. Routing tools accept addresses directly; use this to check one."""
    return _out(C.geocode, query, latlng=latlng, place_id=place_id, language=language,
                region=region, format=format)


@mcp.tool(**_TOOL)
def station_board(station: str, time: str | None = None, arrivals: bool = False,
                  duration_min: int = 60, max_results: int = 15, all_modes: bool = False,
                  source: str = "auto", format: str = "text") -> str:
    """LIVE departure/arrival board: scheduled time, delay (+min), platform and platform
    changes, cancellations, delay reasons, station disruptions. station: name ('Bonn Hbf'),
    DHID ('de:05314:61101') or EVA number. Rail only by default; all_modes=true adds bus,
    tram, subway (Transitous). source: auto (DB OpenBoards if DB keys are configured, else
    Transitous) | db | transitous."""
    return _out(L.station_board, station, time=time, arrivals=arrivals,
                duration_min=duration_min, max_results=max_results, all_modes=all_modes,
                source=source, format=format)


@mcp.tool(**_TOOL)
def live_connections(origin: str, destination: str, departure_time: str | None = None,
                     arrival_time: str | None = None, max_results: int = 4,
                     max_transfers: int | None = None, format: str = "text") -> str:
    """LIVE journey planning with real-time delays, cancellations and platforms (Transitous /
    MOTIS: DELFI real-time in Germany, many feeds Europe-wide). Each leg shows scheduled time
    and +delay. No fares. Places: names, 'lat,lng' or DHID."""
    return _out(L.live_connections, origin, destination, departure_time=departure_time,
                arrival_time=arrival_time, max_results=max_results,
                max_transfers=max_transfers, format=format)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
