import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
MAX_STOPS = int(os.getenv("MAX_STOPS", "24"))
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "http://localhost:5000").rstrip("/")
OSRM_TIMEOUT_SECONDS = float(os.getenv("OSRM_TIMEOUT_SECONDS", "10"))
SOLVER_TIMEOUT_SECONDS = int(os.getenv("SOLVER_TIMEOUT_SECONDS", "5"))
MAP_TILE_URL = os.getenv("MAP_TILE_URL", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png")
MAP_ATTRIBUTION = os.getenv("MAP_ATTRIBUTION", '<a href="https://www.openstreetmap.org/copyright">&copy; OpenStreetMap contributors</a>')
GEOCODER_PROVIDER = os.getenv("GEOCODER_PROVIDER", "photon").strip().lower()
if GEOCODER_PROVIDER not in ("photon", "nominatim"):
    raise RuntimeError("GEOCODER_PROVIDER must be 'photon' or 'nominatim'.")
GEOCODER_BASE_URL = os.getenv("GEOCODER_BASE_URL", "https://photon.komoot.io").rstrip("/")
GEOCODER_TIMEOUT_SECONDS = float(os.getenv("GEOCODER_TIMEOUT_SECONDS", "8"))
GEOCODER_ATTRIBUTION = os.getenv("GEOCODER_ATTRIBUTION", "© OpenStreetMap contributors")
GEOCODER_USER_AGENT = os.getenv(
    "GEOCODER_USER_AGENT", "RouteOptimizer/1.0 (location search; configure contact details)"
)
GEOCODER_CACHE_PATH = Path(os.getenv(
    "GEOCODER_CACHE_PATH", str(ROOT / "backend" / "geocoder-cache.sqlite3")
))
GEOCODER_CACHE_TTL_SECONDS = 24 * 60 * 60
GEOCODER_MIN_INTERVAL_SECONDS = 1.1
UNREACHABLE_COST = 10**9

app = Flask(__name__)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)


def _coordinate(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(f"{label} must be an object with numeric lat and lng fields.")
    lat, lng = value.get("lat"), value.get("lng")
    if isinstance(lat, bool) or isinstance(lng, bool):
        raise ApiError(f"{label} latitude and longitude must be numbers.")
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        raise ApiError(f"{label} latitude and longitude must be numbers.")
    if not math.isfinite(lat) or not math.isfinite(lng):
        raise ApiError(f"{label} latitude and longitude must be finite numbers.")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise ApiError(f"{label} coordinates are outside the valid latitude/longitude range.")
    point = {"lat": lat, "lng": lng}
    name = value.get("label")
    if name is not None:
        if not isinstance(name, str) or len(name) > 200:
            raise ApiError(f"{label} label must be a string of at most 200 characters.")
        point["label"] = name
    return point


def _parse_request(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    if not isinstance(payload, dict):
        raise ApiError("Request body must be a JSON object.")
    if "depot" not in payload:
        raise ApiError("A depot is required.")
    depot = _coordinate(payload["depot"], "Depot")
    raw_stops = payload.get("stops")
    if not isinstance(raw_stops, list):
        raise ApiError("Stops must be provided as a list.")
    if not raw_stops:
        raise ApiError("Add at least one stop before optimizing.")
    if len(raw_stops) > MAX_STOPS:
        raise ApiError(f"A maximum of {MAX_STOPS} stops is supported.")
    stops = []
    stop_ids = set()
    for index, raw_stop in enumerate(raw_stops, start=1):
        stop = _coordinate(raw_stop, f"Stop {index}")
        stop_id = raw_stop.get("id", f"stop-{index}")
        if isinstance(stop_id, bool) or not isinstance(stop_id, (str, int)):
            raise ApiError(f"Stop {index} id must be a string or integer.")
        stop_id = str(stop_id)
        if not stop_id or len(stop_id) > 100:
            raise ApiError(f"Stop {index} id must contain between 1 and 100 characters.")
        if stop_id in stop_ids:
            raise ApiError("Stop ids must be unique.")
        stop_ids.add(stop_id)
        stop["id"] = stop_id
        stops.append(stop)
    round_trip = payload.get("round_trip", True)
    if not isinstance(round_trip, bool):
        raise ApiError("round_trip must be true or false.")
    return depot, stops, round_trip


def _osrm_get(service: str, coordinates: list[dict[str, Any]], **params: Any) -> dict[str, Any]:
    coords = ";".join(f"{point['lng']:.7f},{point['lat']:.7f}" for point in coordinates)
    url = f"{OSRM_BASE_URL}/{service}/v1/driving/{coords}"
    try:
        response = requests.get(url, params=params, timeout=OSRM_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        raise ApiError("The routing service timed out. Please try again.", 504)
    except requests.RequestException:
        app.logger.exception("OSRM request failed")
        raise ApiError("The routing service is unavailable. Please try again shortly.", 502)
    except ValueError:
        raise ApiError("The routing service returned an invalid response.", 502)
    if not isinstance(data, dict) or data.get("code") not in ("Ok",):
        code = data.get("code", "UnknownError") if isinstance(data, dict) else "UnknownError"
        if code in ("NoRoute", "NoSegment", "InvalidUrl", "InvalidQuery"):
            raise ApiError("One or more locations could not be matched to a drivable road.", 422)
        raise ApiError("The routing service could not calculate this route.", 502)
    return data


def _geocoder_connection() -> sqlite3.Connection:
    GEOCODER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(GEOCODER_CACHE_PATH, timeout=15)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS geocoder_cache "
        "(query_key TEXT PRIMARY KEY, response_json TEXT NOT NULL, expires_at REAL NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS geocoder_schedule "
        "(service TEXT PRIMARY KEY, next_request_at REAL NOT NULL)"
    )
    return connection


def _cached_geocode(query_key: str) -> list[dict[str, Any]] | None:
    connection = _geocoder_connection()
    try:
        row = connection.execute(
            "SELECT response_json, expires_at FROM geocoder_cache WHERE query_key = ?",
            (query_key,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or row[1] <= time.time():
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _reserve_geocoder_slot() -> None:
    connection = _geocoder_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT next_request_at FROM geocoder_schedule WHERE service = ?",
            (GEOCODER_BASE_URL,),
        ).fetchone()
        request_at = max(time.time(), row[0] if row else 0)
        connection.execute(
            "INSERT INTO geocoder_schedule(service, next_request_at) VALUES(?, ?) "
            "ON CONFLICT(service) DO UPDATE SET next_request_at = excluded.next_request_at",
            (GEOCODER_BASE_URL, request_at + GEOCODER_MIN_INTERVAL_SECONDS),
        )
        connection.commit()
    finally:
        connection.close()
    delay = request_at - time.time()
    if delay > 0:
        time.sleep(delay)


def _search_places(query: str, bias: dict[str, float] | None = None) -> list[dict[str, Any]]:
    # Location-biased lookups bypass the persistent cache so user coordinates
    # are neither stored in cache keys nor mixed with general search results.
    query_key = f"{GEOCODER_BASE_URL}\n{' '.join(query.casefold().split())}"
    if bias is None:
        cached = _cached_geocode(query_key)
        if cached is not None:
            return cached

    if GEOCODER_PROVIDER == "nominatim":
        _reserve_geocoder_slot()
    # Another worker may have populated this query while this request waited.
    if bias is None:
        cached = _cached_geocode(query_key)
        if cached is not None:
            return cached

    search_params = {"q": query, "limit": 5}
    if GEOCODER_PROVIDER == "photon":
        search_params.update({"lat": bias["lat"], "lon": bias["lng"], "zoom": 12} if bias else {})
    else:
        search_params.update({"format": "jsonv2", "addressdetails": 0})

    try:
        response = requests.get(
            f"{GEOCODER_BASE_URL}/{'api' if GEOCODER_PROVIDER == 'photon' else 'search'}",
            params=search_params,
            headers={"User-Agent": GEOCODER_USER_AGENT},
            timeout=GEOCODER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout:
        raise ApiError("Location search timed out. Please try again.", 504)
    except requests.RequestException:
        app.logger.exception("Geocoding service request failed")
        raise ApiError("Location search is unavailable right now. Please try again shortly.", 502)
    except ValueError:
        raise ApiError("The geocoding service returned an invalid response.", 502)

    if GEOCODER_PROVIDER == "photon":
        if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
            raise ApiError("The geocoding service returned an invalid result list.", 502)
        results = payload["features"]
    elif isinstance(payload, list):
        results = payload
    else:
        raise ApiError("The geocoding service returned an invalid result list.", 502)
    places = []
    for result in results:
        if not isinstance(result, dict):
            continue
        if GEOCODER_PROVIDER == "photon":
            geometry = result.get("geometry") or {}
            coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
            props = result.get("properties") or {}
            try:
                lng, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError, IndexError):
                continue
            label = props.get("name") or props.get("street") or props.get("city")
            parts = [props.get(key) for key in ("housenumber", "street", "postcode", "city", "state", "country")]
            detail = ", ".join(str(part) for part in parts if part)
            if label and detail and detail.casefold() not in str(label).casefold():
                label = f"{label}, {detail}"
        else:
            try:
                lat, lng = float(result["lat"]), float(result["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            label = result.get("display_name")
        if not math.isfinite(lat) or not math.isfinite(lng) or not isinstance(label, str):
            continue
        if not -90 <= lat <= 90 or not -180 <= lng <= 180:
            continue
        places.append({
            "id": str((result.get("properties") or {}).get("osm_id", result.get("place_id", len(places)))),
            "label": label[:200],
            "lat": lat,
            "lng": lng,
        })

    if bias is None:
        connection = _geocoder_connection()
        try:
            now = time.time()
            connection.execute("DELETE FROM geocoder_cache WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO geocoder_cache(query_key, response_json, expires_at) VALUES(?, ?, ?) "
                "ON CONFLICT(query_key) DO UPDATE SET response_json = excluded.response_json, "
                "expires_at = excluded.expires_at",
                (query_key, json.dumps(places), now + GEOCODER_CACHE_TTL_SECONDS),
            )
            connection.commit()
        finally:
            connection.close()
    return places


def _solve_order(durations: list[list[float | None]], round_trip: bool) -> list[int]:
    node_count = len(durations)
    if node_count < 2:
        raise ApiError("Add at least one stop before optimizing.")
    sink = node_count if not round_trip else 0
    matrix_size = node_count + (0 if round_trip else 1)
    manager = pywrapcp.RoutingIndexManager(matrix_size, 1, [0], [sink])
    routing = pywrapcp.RoutingModel(manager)

    def transit(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        if not round_trip and to_node == sink:
            return 0
        cost = durations[from_node][to_node]
        return UNREACHABLE_COST if cost is None else int(cost)

    callback_index = routing.RegisterTransitCallback(transit)
    routing.SetArcCostEvaluatorOfAllVehicles(callback_index)
    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    parameters.time_limit.FromSeconds(max(1, SOLVER_TIMEOUT_SECONDS))
    solution = routing.SolveWithParameters(parameters)
    if solution is None:
        raise ApiError("No route could be found for all stops. Check that every location is reachable.", 422)

    ordered_nodes = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != 0:
            ordered_nodes.append(node)
        next_index = solution.Value(routing.NextVar(index))
        next_node = manager.IndexToNode(next_index)
        if not round_trip and not routing.IsEnd(next_index) and durations[node][next_node] is None:
            raise ApiError("No drivable path connects all locations in this configuration.", 422)
        if round_trip and routing.IsEnd(next_index) and durations[node][0] is None:
            raise ApiError("No drivable path connects the final stop back to the depot.", 422)
        if not routing.IsEnd(next_index) and durations[node][next_node] is None:
            raise ApiError("No drivable path connects all locations in this configuration.", 422)
        index = next_index
    if len(ordered_nodes) != node_count - 1:
        raise ApiError("The solver did not produce a complete route.", 422)
    return ordered_nodes


@app.errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return jsonify({"error": error.message}), error.status


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/config")
def config():
    return jsonify({
        "map_tile_url": MAP_TILE_URL,
        "map_attribution": MAP_ATTRIBUTION,
        "max_stops": MAX_STOPS,
        "geocoder_attribution": GEOCODER_ATTRIBUTION,
        "geocoder_provider": GEOCODER_PROVIDER,
    })


@app.route("/search", methods=["GET", "POST"])
def search_locations():
    payload = request.get_json(silent=True) if request.method == "POST" else None
    if payload is not None and not isinstance(payload, dict):
        raise ApiError("Search request must be a JSON object.")
    raw_query = payload.get("q", "") if payload is not None else request.args.get("q", "")
    if not isinstance(raw_query, str):
        raise ApiError("Search text must be a string.")
    query = raw_query.strip()
    if len(query) < 3:
        raise ApiError("Enter at least 3 characters to search.")
    if len(query) > 200:
        raise ApiError("Search text must be 200 characters or fewer.")
    raw_lat = payload.get("lat") if payload is not None else request.args.get("lat")
    raw_lng = payload.get("lon") if payload is not None else request.args.get("lon")
    bias = None
    if raw_lat is not None or raw_lng is not None:
        if raw_lat is None or raw_lng is None:
            raise ApiError("Both latitude and longitude are required for nearby search.")
        bias = _coordinate({"lat": raw_lat, "lng": raw_lng}, "Search location")
    return jsonify({"results": _search_places(query, bias)})


@app.post("/optimize")
def optimize():
    depot, stops, round_trip = _parse_request(request.get_json(silent=True))
    points = [depot, *stops]
    table = _osrm_get("table", points, annotations="duration")
    durations = table.get("durations")
    if not isinstance(durations, list) or len(durations) != len(points):
        raise ApiError("The routing service returned an incomplete travel-time matrix.", 502)
    if any(not isinstance(row, list) or len(row) != len(points) for row in durations):
        raise ApiError("The routing service returned an invalid travel-time matrix.", 502)
    order = _solve_order(durations, round_trip)
    ordered_stops = [stops[node - 1] for node in order]
    route_points = [depot, *ordered_stops]
    if round_trip:
        route_points.append(depot)
    route_data = _osrm_get(
        "route", route_points, overview="full", geometries="geojson", steps="false"
   )
    routes = route_data.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ApiError("The routing service did not return route geometry.", 502)
    route = routes[0]
    geometry = route.get("geometry")
    if not isinstance(geometry, dict) or not isinstance(geometry.get("coordinates"), list):
        raise ApiError("The routing service returned invalid route geometry.", 502)
    return jsonify({
        "depot": depot,
        "stops": ordered_stops,
        "round_trip": round_trip,
        "route": {"type": "Feature", "geometry": geometry},
        "distance_meters": route.get("distance"),
        "duration_seconds": route.get("duration"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")), debug=False)
