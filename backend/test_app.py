import unittest
from unittest.mock import patch

from app import ApiError, app


class RouteOptimizerApiTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.payload = {
            "depot": {"lat": 52.517, "lng": 13.389},
            "stops": [
                {"id": "order-1", "lat": 52.496, "lng": 13.386},
                {"id": "order-2", "lat": 52.510, "lng": 13.420},
            ],
            "round_trip": True,
        }

    def test_rejects_missing_depot_and_empty_stops(self):
        response = self.client.post("/optimize", json={"stops": self.payload["stops"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("depot", response.json["error"].lower())

        response = self.client.post(
            "/optimize", json={"depot": self.payload["depot"], "stops": []}
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_out_of_range_coordinates_and_too_many_stops(self):
        payload = {**self.payload, "depot": {"lat": 91, "lng": 0}}
        response = self.client.post("/optimize", json=payload)
        self.assertEqual(response.status_code, 400)

        payload = {**self.payload, "stops": [{"lat": 52, "lng": 13}] * 25}
        response = self.client.post("/optimize", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("maximum", response.json["error"])

    def test_rejects_duplicate_stop_ids(self):
        payload = {**self.payload, "stops": [
            {"id": "duplicate", "lat": 52, "lng": 13},
            {"id": "duplicate", "lat": 53, "lng": 14},
        ]}
        response = self.client.post("/optimize", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("unique", response.json["error"])

    def test_round_trip_starts_and_ends_at_depot(self):
        durations = [[0, 1, 10], [10, 0, 1], [1, 10, 0]]
        geometry = {"type": "LineString", "coordinates": [[13.389, 52.517], [13.386, 52.496]]}
        with patch("app._osrm_get", side_effect=[
            {"durations": durations},
            {"routes": [{"geometry": geometry, "distance": 1200, "duration": 240}]},
        ]) as osrm:
            response = self.client.post("/optimize", json=self.payload)

        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual([stop["id"] for stop in response.json["stops"]], ["order-1", "order-2"])
        self.assertTrue(response.json["round_trip"])
        route_points = osrm.call_args_list[1].args[1]
        self.assertEqual(len(route_points), 4)
        self.assertEqual(route_points[0], route_points[-1])
        self.assertEqual(response.json["distance_meters"], 1200)
        self.assertEqual(response.json["duration_seconds"], 240)

    def test_open_route_ends_at_last_stop_without_returning_to_depot(self):
        payload = {**self.payload, "round_trip": False}
        durations = [[0, 10, 1], [1, 0, 1], [100, 1, 0]]
        geometry = {"type": "LineString", "coordinates": [[13.389, 52.517], [13.420, 52.510]]}
        with patch("app._osrm_get", side_effect=[
            {"durations": durations},
            {"routes": [{"geometry": geometry, "distance": 800, "duration": 180}]},
        ]) as osrm:
            response = self.client.post("/optimize", json=payload)

        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual([stop["id"] for stop in response.json["stops"]], ["order-2", "order-1"])
        route_points = osrm.call_args_list[1].args[1]
        self.assertEqual(len(route_points), 3)
        self.assertNotEqual(route_points[0], route_points[-1])

    def test_unreachable_matrix_returns_clear_error(self):
        durations = [[0, None, None], [None, 0, None], [None, None, 0]]
        with patch("app._osrm_get", return_value={"durations": durations}):
            response = self.client.post("/optimize", json=self.payload)
        self.assertEqual(response.status_code, 422)
        self.assertRegex(response.json["error"].lower(), "route|path|reach")

    def test_osrm_timeout_is_reported_as_gateway_timeout(self):
        with patch("app._osrm_get", side_effect=ApiError("The routing service timed out.", 504)):
            response = self.client.post("/optimize", json=self.payload)
        self.assertEqual(response.status_code, 504)
        self.assertIn("timed out", response.json["error"].lower())


if __name__ == "__main__":
    unittest.main()
