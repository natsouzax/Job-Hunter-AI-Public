import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from job_hunter.storage import Store
from job_hunter.web import create_server, dashboard_data

DEMO_PROFILE = json.loads((Path(__file__).resolve().parents[1] / "examples" / "demo_profile.json").read_text(encoding="utf-8"))


class WebInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "jobs.sqlite3")
        self.server = create_server(self.store, root=self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, response.headers, response.read()

    def post(self, path, payload, *, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, data=json.dumps(payload).encode(),
                          headers=headers, method="POST")
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_assets_and_empty_dashboard_are_served(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Job Hunter", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _, script = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/dashboard", script)
        status, _, raw = self.get("/api/dashboard")
        data = json.loads(raw)
        self.assertIsNone(data["profile"])
        self.assertEqual(data["stats"]["applications"], 0)

    def test_profile_can_be_initialized_and_updated(self):
        status, initialized = self.post("/api/profile/init", DEMO_PROFILE)
        self.assertEqual(status, 200)
        profile_id = initialized["result"]["id"]
        status, contact = self.post("/api/profile/contact",
                                    {"email": "person@example.test", "phone": "+351 000"})
        self.assertEqual(status, 200)
        self.assertEqual(contact["dashboard"]["profile"]["preferences"]["contact"]["email"],
                         "person@example.test")
        status, available = self.post("/api/profile/availability", {"confirmed": True})
        self.assertEqual(status, 200)
        self.assertEqual(available["result"]["id"], profile_id)
        self.assertEqual(dashboard_data(self.store, self.root)["profile"]["availability"]["status"],
                         "confirmed")

    def test_mutation_rejects_foreign_origin(self):
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/profile/init", {}, origin="https://attacker.example")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        self.assertEqual(self.store.list_records("CandidateProfile"), [])

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(ValueError):
            create_server(self.store, root=self.root, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
