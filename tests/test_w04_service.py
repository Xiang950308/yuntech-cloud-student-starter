"""Offline W4 public-contract checks."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class W04ServiceContract(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        version = Path(self.tempdir.name) / "version"
        version.write_text("a" * 40, encoding="utf-8")
        auth = Path(self.tempdir.name) / "app.env"
        auth.write_text("REPORTER_TOKEN=reporter-secret\nOPERATOR_TOKEN=operator-secret\n", encoding="utf-8")
        self.server = service.make_server(version, port=0, auth_file=auth)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.tempdir.cleanup()

    def request(self, method, path, body=None, token=None, content_type="application/json"):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {}
        if data is not None:
            headers["Content-Type"] = content_type
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_fixtures_cover_accept_and_reject_paths(self):
        success = json.loads((ROOT / "tests/fixtures/success.json").read_text(encoding="utf-8"))
        no_timezone = json.loads((ROOT / "tests/fixtures/rejected-no-timezone.json").read_text(encoding="utf-8"))
        extra_field = json.loads((ROOT / "tests/fixtures/rejected-extra-field.json").read_text(encoding="utf-8"))
        self.assertEqual(self.request("POST", "/events", success, "reporter-secret")[0], 201)
        self.assertEqual(self.request("POST", "/events", success, "reporter-secret")[0], 200)
        self.assertEqual(self.request("POST", "/events", no_timezone, "reporter-secret"),
                         (400, {"error": "invalid_value", "field": "observed_at"}))
        self.assertEqual(self.request("POST", "/events", extra_field, "reporter-secret"),
                         (400, {"error": "unknown_field", "field": "secret"}))

    def test_authentication_authorization_and_query(self):
        event = json.loads((ROOT / "tests/fixtures/success.json").read_text(encoding="utf-8"))
        self.assertEqual(self.request("POST", "/events", event)[0], 401)
        self.assertEqual(self.request("POST", "/events", event, "operator-secret")[0], 403)
        self.assertEqual(self.request("POST", "/events", event, "reporter-secret")[0], 201)
        self.assertEqual(self.request("GET", "/events", token="reporter-secret")[0], 403)
        self.assertEqual(self.request("GET", "/events", token="operator-secret")[0], 200)
        status, found = self.request("GET", f"/events/{event['event_id']}", token="operator-secret")
        self.assertEqual(status, 200)
        self.assertEqual(found["event_id"], event["event_id"])
        self.assertIn("received_at", found)

    def test_health_and_display_page(self):
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["auth_configured"])
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request) as response:
            page = response.read().decode("utf-8")
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("?token=", page)


if __name__ == "__main__":
    unittest.main()
