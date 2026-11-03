# gmaps-routes

Full-featured wrapper for Google Maps routing: Routes API (`computeRoutes`, `computeRouteMatrix`),
Geocoding, Places (New) text search. CLI + MCP server (stdio). Only dependency: `requests` (+ `mcp` for the server).

**Status**: offline tests pass (`pytest tests/`; the fixture is hand-written, not a captured response).
Verified live (Bonn, 2026-11): `computeRoutes` for TRANSIT / BICYCLE / WALK / DRIVE, `compare`,
`computeRouteMatrix` (BICYCLE), Geocoding. **Not yet live-tested**: Places search, `--arrive`,
waypoints / `--optimize`, `--avoid`.

Known quirks:
- Google localizes its own strings (e.g. "3 Minuten") by default; pass `--lang en` / `--lang zh-CN` (MCP: `language`) to unify.
- Transit `duration` excludes waiting for the vehicle; use `door_to_door_s` (departure request -> final arrival).
- Transit fare can be empty: Google returns no fare for many agencies.
- Walk steps inside transit routes may have no instruction text.

## Setup
1. Google Cloud project -> enable *Routes API* (+ *Geocoding API*, *Places API (New)* if used). Restrict the key to those APIs, set a daily quota cap.
2. Provide the key as env var `GOOGLE_MAPS_API_KEY` (never paste it into chat / commit it).
3. Optional: `GMAPS_TZ` (default `Europe/Berlin`) for naive times like `2026-11-04T08:30`.

## CLI
```bash
python3 -m gmaps_routes route "Bonn Hbf" "Uni Bonn" --mode TRANSIT --arrive 2026-11-04T08:30 --transit-pref FEWER_TRANSFERS
python3 -m gmaps_routes route A B --mode BICYCLE --depart +20m --output summary
python3 -m gmaps_routes compare A B --depart +0m            # transit vs bike vs walk vs drive
python3 -m gmaps_routes matrix --origins A B --destinations C D --mode BICYCLE
python3 -m gmaps_routes geocode "Bonn Hbf" ; python3 -m gmaps_routes places "Bäckerei" --near 50.73,7.10
python3 -m gmaps_routes route A B --dry-run                  # print request, no network
```

## MCP (Claude Code `.mcp.json`)
```json
{"mcpServers": {"gmaps": {"command": "python3", "args": ["-m", "gmaps_routes.mcp_server"],
  "env": {"GOOGLE_MAPS_API_KEY": "${GOOGLE_MAPS_API_KEY}"}}}}
```
Or register at user level (stdio, nothing listens on a port; the key stays in your shell env):
```bash
claude mcp add --scope user gmaps -e GOOGLE_MAPS_API_KEY="$GOOGLE_MAPS_API_KEY" -- python3 -m gmaps_routes.mcp_server
```
Run it from an install (`pip install -e .`) or set `PYTHONPATH` to the repo, so `gmaps_routes` is importable.

Tools: `compute_route`, `compare_modes`, `compute_route_matrix`, `geocode`, `search_places`, `decode_polyline`.

## Output
`output=both` (default) returns a readable `routes[]` summary (headline, times, transfers, walking, fare,
per-step instructions and transit line/stop/time details) **and** the full raw API JSON under `raw`.
Field mask is the full set (no `*`); override with `fields`. Use `extra_request` to merge any API option not exposed.

## Cost notes
Traffic-aware driving, waypoint optimisation and some fields bill at higher SKUs; free monthly caps are per SKU.
Check the official pricing page. Defaults: DRIVE/TWO_WHEELER use TRAFFIC_AWARE; transit/bicycle/walk have no routing preference.
