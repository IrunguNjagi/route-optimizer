# Route Optimizer

A single-driver, multi-stop route optimizer. It uses real road travel times to choose an efficient visit order, then draws the driving route on a Leaflet map.

## Stack

- Flask API and Google OR-Tools routing solver
- OSRM Table API for drive-time matrices and Route API for road geometry
- Single-file Leaflet frontend
- Docker Compose for a one-host deployment, with Caddy providing HTTPS

## Local development

Requires Python 3.12 or newer. The default local mode uses the public OSRM demo server. It is rate-limited and is only suitable for development and low-volume testing.

```sh
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
OSRM_BASE_URL=https://router.project-osrm.org python app.py
```

Open <http://localhost:5001>. On first visit, a short introduction explains the route planner. Search an address or place and choose a suggestion to set the depot or add a stop. Suggestions appear as you type; use the arrow keys and Enter to choose one. Select **Use my location** to grant browser location access and bias Photon suggestions toward your current area. Coordinates stay in browser memory and are sent to the configured geocoder only with location-biased searches; they are not written to the geocoder cache. You can also click the map to add locations. Add up to 24 stops, then optimize. Dragging a pin or changing round-trip mode automatically recalculates the route. The browser fetches map tiles from the configured tile provider; the default is OpenStreetMap's public tile service and is not intended for heavy or commercial use.

Location search uses Photon by default (`GEOCODER_PROVIDER=photon`), through the Flask backend. Photon supports search-as-you-type and location bias; the browser waits briefly after typing, and the backend caches non-biased results for 24 hours. When the user chooses **Use my location**, the browser's geolocation API asks permission, then search coordinates are posted to the backend and forwarded to Photon as a location bias. The browser does not expose the user's IP-derived location directly; if geolocation is denied, search still works without nearby bias. The public Photon demo permits reasonable project use but does not guarantee uptime and may throttle extensive traffic, so use a private instance or a provider whose terms and capacity fit your production use. Alternatively, set `GEOCODER_PROVIDER=nominatim` and `GEOCODER_BASE_URL=https://nominatim.openstreetmap.org` for explicit submit-to-search behavior; the public Nominatim service prohibits autocomplete, so that mode only searches when the user submits and spaces uncached requests by at least 1.1 seconds. Search text is sent to the configured service; avoid confidential or sensitive information.

On Windows PowerShell, activate the virtual environment with `.venv\Scripts\Activate.ps1` and set the development URL with `$env:OSRM_BASE_URL = 'https://router.project-osrm.org'` before running `python app.py`.

## Single-host Linux deployment

The production Compose stack runs the Flask app, self-hosted OSRM and Caddy on one Linux host. Caddy obtains and renews TLS certificates automatically. Before starting the services:

1. Install Docker Engine and the Docker Compose plugin. Point the application's DNS name at the host and allow inbound TCP ports 80 and 443 (UDP 443 enables HTTP/3).
2. Download a regional `.osm.pbf` extract from a source such as [Geofabrik](https://download.geofabrik.de/) and place it at `osrm/data/region.osm.pbf`. Pick an extract that covers all expected depots and stops; allow for RAM and disk requirements during preprocessing.
3. From the repository root, preprocess the extract using the same OSRM image as the server, following the [official OSRM Docker instructions](https://github.com/Project-OSRM/osrm-backend#using-docker):

```sh
docker run --rm -t -v "$PWD/osrm/data:/data" ghcr.io/project-osrm/osrm-backend:26.7.3 \
  osrm-extract -p /opt/car.lua /data/region.osm.pbf
docker run --rm -t -v "$PWD/osrm/data:/data" ghcr.io/project-osrm/osrm-backend:26.7.3 \
  osrm-partition /data/region.osrm
docker run --rm -t -v "$PWD/osrm/data:/data" ghcr.io/project-osrm/osrm-backend:26.7.3 \
  osrm-customize /data/region.osrm
```

4. Copy `.env.example` to `.env` and set `CADDY_DOMAIN` to the DNS name. Configure `MAP_TILE_URL` and `MAP_ATTRIBUTION` for the intended usage. The OpenStreetMap public tile server default is suitable for development only; production deployments must select a tile provider whose terms and capacity fit their use. For production location suggestions, configure `GEOCODER_PROVIDER=photon` and point `GEOCODER_BASE_URL` to your private Photon instance (base URL only, for example `http://photon:2322`). Nominatim-compatible providers can also be used with `GEOCODER_PROVIDER=nominatim`; that mode has submit-only search.
5. Start the stack:

```sh
docker compose up -d --build
docker compose logs -f api osrm caddy
```

The site is available at `https://<CADDY_DOMAIN>`. The app and API share one origin; OSRM is available only to the internal Compose network. If OSRM needs to be rebuilt for a map update, preprocess the new extract and restart the `osrm` service.

## API

`POST /optimize` accepts JSON:

```json
{
  "depot": {"lat": 52.517, "lng": 13.389},
  "stops": [
    {"id": "order-1", "lat": 52.496, "lng": 13.386},
    {"id": "order-2", "lat": 52.51, "lng": 13.42}
  ],
  "round_trip": false
}
```

Coordinates use decimal latitude and longitude. `round_trip` is a boolean and defaults to `true`. The response contains the optimized stop list, a GeoJSON route feature, total road distance in meters, and estimated drive time in seconds. Errors return a JSON `error` message with an appropriate HTTP status. `GET /search?q=<text>` returns up to five place results; queries must contain 3–200 characters. `POST /search` accepts the same `q` with optional `lat` and `lon` fields for location-biased Photon results. `GET /healthz` is a lightweight application health check; `GET /config` returns the configured basemap URL, attribution, stop limit, and geocoder attribution.

## Checks

Run the API and route-mode checks from the repository root with the application dependencies installed:

```sh
python -m unittest discover -s backend -v
```

The browser behavior and Compose deployment also need an interactive map check and a prepared OSRM extract on a Linux Docker host.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OSRM_BASE_URL` | `http://localhost:5000` | OSRM service root; set to the public demo only in local development |
| `OSRM_TIMEOUT_SECONDS` | `10` | Timeout for each OSRM request |
| `SOLVER_TIMEOUT_SECONDS` | `5` | OR-Tools search time budget |
| `MAX_STOPS` | `24` | Maximum stops accepted by the API |
| `MAP_TILE_URL` | OpenStreetMap public tile URL | Leaflet tile template; configure a production provider |
| `MAP_ATTRIBUTION` | OpenStreetMap contributors | Attribution HTML displayed by Leaflet |
| `GEOCODER_PROVIDER` | `photon` | `photon` enables autocomplete; `nominatim` enables submit-only search |
| `GEOCODER_BASE_URL` | `https://photon.komoot.io` | Geocoder base URL; use a private instance or authorized provider for production |
| `GEOCODER_TIMEOUT_SECONDS` | `8` | Timeout for each geocoder request |
| `GEOCODER_ATTRIBUTION` | OpenStreetMap contributors | Place-data attribution displayed in the search panel |
| `GEOCODER_USER_AGENT` | Route Optimizer identifier | Identifies the application to the geocoder; configure contact details for deployment |
| `PORT` | `5001` | Flask development server port |

## Current boundaries

The app keeps route plans in browser memory. The backend caches search queries and their geocoder results locally for 24 hours to reduce repeat requests. It does not assign time windows or service times, or optimize multiple drivers. OSRM travel-time estimates do not account for live traffic unless the self-hosted routing data is updated with an appropriate traffic workflow.
