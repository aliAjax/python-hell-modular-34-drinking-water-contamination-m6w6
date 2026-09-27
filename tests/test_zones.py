import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.domain import ConflictError
from src.http_api import build_handler
from src.repository import Repository
from src.service import Service


def make_payload(source_id, zones):
    return {
        "source_id": source_id,
        "contaminant": "nitrate",
        "detected_at": "2026-09-27T06:00:00+00:00",
        "concentration": 20,
        "limit": 10,
        "zone_ids": zones,
        "population": 1000,
        "complaints": 1,
    }


class ZoneLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, source_id, zones):
        return self.service.create_item(make_payload(source_id, zones), "a", "analyst")

    def _drive_to_sampled(self, item):
        zone_id = item["payload"]["zone_ids"][0]
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-%s" % item["id"], "kind": "boil", "message": "煮沸"}, "d", "dispatcher", item["version"])
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "ALT-1"}, "c", "coordinator", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": zone_id}, "f", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": zone_id, "completed": True}, "f", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-%s" % item["id"], "zone_id": zone_id, "concentration": 2}, "l", "lab", item["version"])
        return item

    def _zones(self):
        return {zone["zone_id"]: zone for zone in self.service.state()["zones"]}

    def test_zone_locked_on_create_and_released_after_restore(self):
        item = self._create("SRC-Z1", ["Z-9"])
        zones = self._zones()
        self.assertTrue(zones["Z-9"]["locked"])
        self.assertEqual(zones["Z-9"]["open_events"], 1)
        item = self._drive_to_sampled(item)
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        zones = self._zones()
        self.assertFalse(zones["Z-9"]["locked"])
        self.assertEqual(zones["Z-9"]["open_events"], 0)

    def test_restore_reports_open_events_until_all_end(self):
        first = self._drive_to_sampled(self._create("SRC-Z2", ["Z-10"]))
        second = self._drive_to_sampled(self._create("SRC-Z3", ["Z-10"]))
        zones = self._zones()
        self.assertEqual(zones["Z-10"]["open_events"], 2)
        with self.assertRaises(ConflictError) as context:
            self.service.act(first["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", first["version"])
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(context.exception.code, "zone_events_open")
        self.assertEqual([e["id"] for e in context.exception.extra["open_events"]], [second["id"]])
        zones = self._zones()
        self.assertTrue(zones["Z-10"]["locked"])
        self.assertEqual(zones["Z-10"]["open_events"], 1)
        self.service.act(second["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", second["version"])
        zones = self._zones()
        self.assertFalse(zones["Z-10"]["locked"])
        self.assertEqual(zones["Z-10"]["open_events"], 0)

    def test_cancel_reports_open_events_until_all_end(self):
        first = self._create("SRC-Z4", ["Z-11"])
        second = self._create("SRC-Z5", ["Z-11"])
        with self.assertRaises(ConflictError) as context:
            self.service.act(first["id"], "cancel", {"reason": "误报"}, "c", "coordinator", first["version"])
        self.assertEqual(context.exception.code, "zone_events_open")
        self.assertEqual([e["id"] for e in context.exception.extra["open_events"]], [second["id"]])
        self.assertTrue(self._zones()["Z-11"]["locked"])
        self.service.act(second["id"], "cancel", {"reason": "误报"}, "c", "coordinator", second["version"])
        self.assertFalse(self._zones()["Z-11"]["locked"])

    def test_ended_events_do_not_block_restore(self):
        first = self._create("SRC-Z6", ["Z-12"])
        second = self._create("SRC-Z7", ["Z-12"])
        with self.assertRaises(ConflictError):
            self.service.act(second["id"], "cancel", {"reason": "误报"}, "c", "coordinator", second["version"])
        first = self._drive_to_sampled(first)
        item = self.service.act(first["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", first["version"])
        self.assertEqual(item["status"], "restored")
        self.assertFalse(self._zones()["Z-12"]["locked"])

    def test_events_on_other_zones_do_not_block(self):
        first = self._drive_to_sampled(self._create("SRC-Z8", ["Z-13"]))
        self._create("SRC-Z9", ["Z-14"])
        item = self.service.act(first["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", first["version"])
        self.assertEqual(item["status"], "restored")
        zones = self._zones()
        self.assertFalse(zones["Z-13"]["locked"])
        self.assertTrue(zones["Z-14"]["locked"])


class HttpZoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        repo = Repository(self.tmp.name)
        repo.initialize()
        service = Service(repo)
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(service, static_dir))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.unlink(self.tmp.name)

    def _request(self, method, path, body=None, role=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("X-User-Id", "tester")
        if role:
            request.add_header("X-Role", role)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_zone_lock_api_contract(self):
        status, first = self._request("POST", "/api/items", make_payload("SRC-H1", ["Z-20"]), "analyst")
        self.assertEqual(status, 201)
        status, second = self._request("POST", "/api/items", make_payload("SRC-H2", ["Z-20"]), "analyst")
        self.assertEqual(status, 201)
        status, state = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        zones = {zone["zone_id"]: zone for zone in state["zones"]}
        self.assertTrue(zones["Z-20"]["locked"])
        self.assertEqual(zones["Z-20"]["open_events"], 2)
        status, body = self._request(
            "POST",
            "/api/items/%d/actions" % first["id"],
            {"action": "cancel", "reason": "误报", "expected_version": first["version"]},
            "coordinator",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "zone_events_open")
        self.assertEqual([event["id"] for event in body["open_events"]], [second["id"]])
        status, state = self._request("GET", "/api/state")
        zones = {zone["zone_id"]: zone for zone in state["zones"]}
        self.assertTrue(zones["Z-20"]["locked"])
        self.assertEqual(zones["Z-20"]["open_events"], 1)
        status, body = self._request(
            "POST",
            "/api/items/%d/actions" % second["id"],
            {"action": "cancel", "reason": "误报", "expected_version": second["version"]},
            "coordinator",
        )
        self.assertEqual(status, 200)
        status, state = self._request("GET", "/api/state")
        zones = {zone["zone_id"]: zone for zone in state["zones"]}
        self.assertFalse(zones["Z-20"]["locked"])
        self.assertEqual(zones["Z-20"]["open_events"], 0)


if __name__ == "__main__":
    unittest.main()
