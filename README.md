# gmaps-routes

Google Maps routing for an LLM (public transit and bicycle first): Routes API
(`computeRoutes`, `computeRouteMatrix`), Places API (New) text search, and Geocoding.
It ships as a CLI and a stdio MCP server. The only dependency is `requests` (plus `mcp` for the server).

Tools return **compact text** by default, sized for a model's context. `format=json` gives normalised
JSON and `format=raw` gives the untouched API response.

## Setup
1. In a Google Cloud project, enable *Routes API*, plus *Places API (New)* and *Geocoding API* if you use them.
   Restrict the key to those APIs and set a daily quota cap.
2. `pip install -e '.[mcp]'`
3. Set the env var `GOOGLE_MAPS_API_KEY`. Never put the key in the repo or in command-line arguments.
4. Optional:
   - `GMAPS_TZ` (default `Europe/Berlin`) sets how naive times are read and how times are shown.
   - `GMAPS_LANG` (default `en`) sets the language of Google's own strings.

## MCP (stdio; the client spawns it, nothing listens on a port)
```bash
claude mcp add --scope user gmaps -e GOOGLE_MAPS_API_KEY="$GOOGLE_MAPS_API_KEY" -- gmaps-routes-mcp
```
Or in `.mcp.json`:
```json
{"mcpServers": {"gmaps": {"command": "gmaps-routes-mcp",
  "env": {"GOOGLE_MAPS_API_KEY": "${GOOGLE_MAPS_API_KEY}"}}}}
```
Tools (all read-only):
- `compute_route`
- `compare_modes`
- `compute_route_matrix`
- `search_places`
- `geocode`

## CLI
```bash
gmaps-routes route "Bonn, Kaiserplatz" "Düren Bahnhof"                 # transit, now
gmaps-routes route A B --arrive 2026-11-04T09:00 --transit-pref FEWER_TRANSFERS
gmaps-routes route A B --mode BICYCLE --steps                           # turn-by-turn
gmaps-routes compare A B --arrive "tomorrow 08:45"                      # latest departure per mode
gmaps-routes compare A B --modes transit bike car                       # default: transit bike walk
gmaps-routes matrix --origins A B --destinations C D --mode BICYCLE
gmaps-routes places "Bäckerei" --near 50.7323,7.0969 --detailed --open-now
gmaps-routes route A B --dry-run                                        # request only, no network
```
Each command also accepts `--json` or `--raw`. Times can be:
- ISO (naive means `GMAPS_TZ`)
- `HH:MM` (the next occurrence), `today HH:MM`, `tomorrow HH:MM`
- `+45m`
- omitted, which means now

Modes accept aliases such as `bike`, `car` and `walking`.

Example output (live, 2026-11-03):
```
TRANSIT Bonn, Kaiserplatz → Düren Bahnhof · depart now (10:50)
1. 11:01 → 12:23 (1 h 21 min) · leave in 11 min · 1 transfer · walk 331 m
   walk 2 min → RE5 Bonn Hauptbahnhof 11:04 → Köln Messe/Deutz 11:33 (→Dinslaken Bahnhof, 4 stops) → walk 2 min → wait 17 min → RE9 Köln Messe/Deutz 11:52 → Düren 12:22 (→Aachen Hbf, 4 stops) → walk 1 min
...
Bonn Hbf → Köln Hbf · depart now (10:50) · ranked by earliest arrival
TRANSIT  10:53 → 11:22 (28 min) · leave in 3 min · 0 transfers · walk 7 m · RB48
DRIVE    10:50 → 11:26 (36 min) · 29.0 km
BICYCLE  10:50 → 12:26 (1 h 36 min) · 30.7 km
WALK     10:50 → 17:16 (6 h 26 min) · 28.4 km
```

## Semantics
- **Transit times**:
  - `leave` and `arrive` are door to door: the first vehicle's departure minus the walk to it, and the last
    arrival plus the walk from it.
  - `duration` is `arrive - leave`.
  - `leave in N min` is the wait before you have to set off.
  - `wait N min` is idle time at a transfer.
  - The API's own `duration` excludes the initial wait, which is why it isn't used here.
- **Sorting**: transit options are sorted by earliest arrival, or with `arrive` by latest departure. The API's
  own order is not like that.
- **Arrive-by**: the API supports it only for TRANSIT. For bike, walk and drive, `compare` computes
  `leave = arrive − duration`. For DRIVE this is approximate, because it uses the current traffic estimate.
- **TRANSIT with intermediate stops**: the API does not support it, so it is rejected locally. Route each
  leg separately.
- **Transit matrix**: durations exclude the initial wait.
- **Partially matched places**: the Routes API's own geocoder resolves some common inputs to a whole
  city, for example `Köln Hbf` and `Flughafen Köln/Bonn`. It then routes to the city centre without
  any error, which gives a plausible but wrong trip (Bonn → "Köln Hbf" came out as 22 min to Köln Süd).
  - `compute_route` and `compare_modes` detect `partialMatch` in `geocodingResults`, resolve the text
    with Places (cached per process), retry with the `place_id`, and say so in a note.
  - The matrix endpoint never reports partial matches, so `compute_route_matrix` resolves every
    address with Places up front.
  - This costs one extra Places Text Search (Pro SKU) per distinct text.
- **Unresolvable addresses**: these come back as a note with a hint, instead of a silent "no route".
  Transit queries with no result also get a hint: a time too far ahead (timetables aren't published
  yet) or no service at that time.
- **`transit_modes`**: Google treats this as a preference and still returns other vehicles.
  Options that use other vehicles are dropped, and a note says how many.
- **Walk-only results**: when walking is faster than any connection, Google returns a walk-only
  "transit" route. It is shown as `walk only`.
- **`compare` modes**: `compare` defaults to TRANSIT, BICYCLE and WALK. DRIVE is opt-in because
  traffic-aware driving bills as Pro.

## Performance (measured from a cloud container, Bonn queries, 2026-11-03)
| | old version | now |
|---|---|---|
| transit route, repeat call | ~450–600 ms | ~130–180 ms (keep-alive session) |
| compare 4 modes | ~2.0 s (sequential) | ~0.1–0.2 s warm, ~0.5 s cold (parallel) |
| transit route output | 72 kB JSON (summary + raw) | ~1 kB text for 6 options |
| bike route output | 118 kB | 0.3 kB (1.5 kB with `--steps`) |

Almost all of the remaining latency is Google's. The field masks request only what the output uses: no
polylines unless asked, and no step details for bike, walk or drive unless `steps`.

## Billing (Google's SKU rules, checked 2026-11)
Routes SKUs are set by **features, not by field masks**:
- **Essentials** covers TRANSIT, BICYCLE and WALK with the defaults here.
- **Pro** is triggered by `TRAFFIC_AWARE` or `TRAFFIC_AWARE_OPTIMAL` (the DRIVE default here),
  `optimizeWaypointOrder`, 11–25 intermediates, or a heading, side-of-road or vehicle-stopover modifier.
- **Enterprise** is triggered by TWO_WHEELER, toll calculation (`--extra-compute TOLLS`) or traffic on
  polylines.
- **Places Text Search**: the basic fields are the Pro SKU. `detailed`, `open_now` and `min_rating` add
  Enterprise fields (rating, hours, phone, website).
- **Free monthly caps** are per SKU. Check Google's pricing page for current numbers.

## Known limits
- Google often returns no transit fare, for example for VRS/VRR in NRW.
- Platforms and real-time delays are not in the Routes API.
- Bicycle routes have no elevation data.
- Tests: `pytest tests/` runs offline against real API responses captured in `tests/fixtures/`.
