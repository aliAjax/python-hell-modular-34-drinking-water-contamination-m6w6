import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service


class ZoneSealTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _create(self, source_id, zones):
        return self.service.create_item({
            "source_id": source_id,
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": zones,
            "population": 1000,
        }, "analyst-1", "analyst")

    def _drive_to_sampled(self, item):
        zone = item["payload"]["zone_ids"][0]
        tag = str(item["id"])
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "advise", {"notice_id": "N-" + tag, "kind": "boil", "message": "煮沸"}, "d", "dispatcher", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": zone}, "f", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": zone, "completed": True}, "f", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {"sample_id": "S-" + tag, "zone_id": zone, "concentration": 2}, "l", "lab", item["version"])
        return item

    def _zone(self, zone_id):
        for zone in self.service.state()["zones"]:
            if zone["zone_id"] == zone_id:
                return zone
        return None

    def test_restore_waits_for_all_related_events(self):
        first = self._create("SRC-Z1", ["Z-1"])
        second = self._create("SRC-Z2", ["Z-1"])
        zone = self._zone("Z-1")
        self.assertTrue(zone["sealed"])
        self.assertEqual(zone["open_count"], 2)

        first = self._drive_to_sampled(first)
        with self.assertRaises(ConflictError) as context:
            self.service.act(first["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", first["version"])
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(context.exception.code, "zone_events_open")
        open_ids = [event["id"] for event in context.exception.extra["open_events"]]
        self.assertEqual(open_ids, [second["id"]])

        # 第一条事件已结案，但区域因第二条未结束而保持封控
        self.assertEqual(self.service.get_item(first["id"])["status"], "restored")
        zone = self._zone("Z-1")
        self.assertTrue(zone["sealed"])
        self.assertEqual(zone["open_count"], 1)

        second = self._drive_to_sampled(second)
        result = self.service.act(second["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", second["version"])
        self.assertEqual(result["status"], "restored")
        zone = self._zone("Z-1")
        self.assertFalse(zone["sealed"])
        self.assertEqual(zone["open_count"], 0)

    def test_cancel_waits_for_all_related_events(self):
        first = self._create("SRC-Z3", ["Z-2"])
        second = self._create("SRC-Z4", ["Z-2"])
        with self.assertRaises(ConflictError) as context:
            self.service.act(first["id"], "cancel", {"reason": "误报"}, "c", "coordinator", first["version"])
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(self.service.get_item(first["id"])["status"], "cancelled")
        self.assertTrue(self._zone("Z-2")["sealed"])
        result = self.service.act(second["id"], "cancel", {"reason": "误报"}, "c", "coordinator", second["version"])
        self.assertEqual(result["status"], "cancelled")
        zone = self._zone("Z-2")
        self.assertFalse(zone["sealed"])
        self.assertEqual(zone["open_count"], 0)

    def test_unrelated_zones_do_not_block(self):
        first = self._create("SRC-Z5", ["Z-3"])
        self._create("SRC-Z6", ["Z-4"])
        first = self._drive_to_sampled(first)
        result = self.service.act(first["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", first["version"])
        self.assertEqual(result["status"], "restored")
        self.assertFalse(self._zone("Z-3")["sealed"])
        self.assertTrue(self._zone("Z-4")["sealed"])

    def test_single_event_release_backward_compatible(self):
        item = self._create("SRC-Z7", ["Z-5"])
        self.assertTrue(self._zone("Z-5")["sealed"])
        item = self._drive_to_sampled(item)
        result = self.service.act(item["id"], "restore", {"all_zones_cleared": True}, "c", "coordinator", item["version"])
        self.assertEqual(result["status"], "restored")
        zone = self._zone("Z-5")
        self.assertFalse(zone["sealed"])
        self.assertEqual(zone["open_count"], 0)


if __name__ == "__main__":
    unittest.main()
