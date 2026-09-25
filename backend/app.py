import math
import os
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
SOLVER_TIMEOUT_SECONDS = int(os.getenv("SOLVER_TIMEOUT_SECONDS", "5"))`r`nMAP_TILE_URL = os.getenv("MAP_TILE_URL", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png")`r`nMAP_ATTRIBUTION = os.getenv("MAP_ATTRIBUTION", "&copy; OpenStreetMap contributors")
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
    return {"lat": lat, "lng": lng}


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
    for index, raw_stop in enumerate(raw_stops, start=1):
        stop = _coordinate(raw_stop, f"Stop {index}")
        stop["id"] = raw_stop.get("id", f"stop-{index}")
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

