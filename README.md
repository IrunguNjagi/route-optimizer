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

Open <http://localhost:5001>. Click once to place the depot, click to add up to 24 stops, then optimize. Dragging a pin or changing round-trip mode automatically recalculates the route. The browser fetches map tiles from the configured tile provider; the default is OpenStreetMap's public tile service and is not intended for heavy or commercial use.

On Windows PowerShell, activate the virtual environment with `.venv\Scripts\Activate.ps1` and set the development URL with `$env:OSRM_BASE_URL = 'https://router.project-osrm.org'` before running `python app.py`.

## Single-host Linux deployment

The production Compose stack runs the Flask app, self-hosted OSRM and Caddy on one Linux host. Caddy obtains and renews TLS certificates automatically. Before starting the services:

1. Install Docker Engine and the Docker Compose plugin. Point the application's DNS name at the host and allow inbound TCP ports 80 and 443 (UDP 443 enables HTTP/3).
2. Download a regional `.osm.pbf` extract from a source such as [Geofabrik](https://download.geofabrik.de/) and place it at `osrm/data/region.osm.pbf`. Pick an extract that covers all expected depots and stops; allow for RAM and disk requirements during preprocessing.
3. From the repository root, preprocess the extract using the same OSRM image as the server:

```sh
docker run --rm -t -v "$PWD/osrm/data:/data" ghcr.io/project-osrm/osrm-backend:26.7.3 \
  osrm-extract -p /opt/car.lua /data/region.osm.pbf
docker run --rm -t -v "$PWD/osrm/data:/data" ghcr.io/project-osrm/osrm-backend:26.7.3 \
  osrm-partition /data/region.osrm
docker run --rm -t -v "$PWD/osrm/data:/data" ghcr.io/project-osrm/osrm-backend:26.7.3 \
  osrm-customize /data/region.osrm
```

4. Copy `.env.example` to `.env`, set `CADDY_DOMAIN` to the DNS name, and configure `MAP_TILE_URL` and `MAP_ATTRIBUTION` for the intended usage. The OpenStreetMap public tile server default is suitable for development only; production deployments must select a tile provider whose terms and capacity fit their use.
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

Coordinates use decimal latitude and longitude. `round_trip` defaults to no implicit value: callers must send a boolean. The response contains the optimized stop list, a GeoJSON route feature, total road distance in meters, and estimated drive time in seconds. Errors return a JSON `error` message with an appropriate HTTP status. `GET /healthz` is a lightweight application health check; `GET /config` returns the configured basemap URL and attribution.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OSRM_BASE_URL` | `http://localhost:5000` | OSRM service root; set to the public demo only in local development |
| `OSRM_TIMEOUT_SECONDS` | `10` | Timeout for each OSRM request |
| `SOLVER_TIMEOUT_SECONDS` | `5` | OR-Tools search time budget |
| `MAX_STOPS` | `24` | Maximum stops accepted by the API |
| `MAP_TILE_URL` | OpenStreetMap public tile URL | Leaflet tile template; configure a production provider |
| `MAP_ATTRIBUTION` | OpenStreetMap contributors | Attribution HTML displayed by Leaflet |
| `PORT` | `5001` | Flask development server port |

## Current boundaries

The app keeps route data in browser memory and does not persist customer information. It does not geocode addresses, assign time windows or service times, or optimize multiple drivers. OSRM travel-time estimates do not account for live traffic unless the self-hosted routing data is updated with an appropriate traffic workflow.
