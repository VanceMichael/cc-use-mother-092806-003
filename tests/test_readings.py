"""计量点、读数版本、幂等、隔离与双重计量拦截。"""

import unittest

from src.hub.errors import (
    DoubleMeasurement,
    IdConflict,
    IncidentScopeError,
    PermissionDenied,
    QuarantineConflict,
    UnknownReference,
)
from tests.world import build_world


class ReadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = build_world()

    def test_identical_receipt_is_idempotent(self) -> None:
        self.s.submit_reading("R-0001", "M-COLD", "2026-09", 1200.0, "rec1")
        before = len(self.s.events)
        event = self.s.submit_reading("R-0001", "M-COLD", "2026-09", 1200.0, "rec1")
        self.assertEqual(len(self.s.events), before)
        self.assertEqual(event["type"], "reading_received")

    def test_same_receipt_different_value_is_quarantined(self) -> None:
        self.s.submit_reading("R-0001", "M-COLD", "2026-09", 1200.0, "rec1")
        with self.assertRaisesRegex(QuarantineConflict, "编号相同但数值不同"):
            self.s.submit_reading("R-0001", "M-COLD", "2026-09", 1800.0, "rec2")
        # 冲突读数被隔离，不计入周期；原读数仍是当前有效读数
        self.assertIn("R-0001", self.s.quarantined)
        slot = self.s.period_index["M-COLD"]["2026-09"]
        self.assertEqual(slot["current"], "R-0001")
        self.assertEqual(self.s.readings["R-0001"]["value"], 1200.0)
        # 隔离回执再次提交继续被拒，不会悄悄入账
        with self.assertRaises(QuarantineConflict):
            self.s.submit_reading("R-0001", "M-COLD", "2026-09", 1800.0, "rec2")

    def test_new_receipt_for_same_period_is_double_measurement(self) -> None:
        """同一批货被两次计量：不同回执、同周期同仪表，直接拦截。"""
        self.s.submit_reading("R-0001", "M-COLD", "2026-09", 1200.0, "rec1")
        with self.assertRaisesRegex(DoubleMeasurement, "两次计量"):
            self.s.submit_reading("R-0002", "M-COLD", "2026-09", 1200.0, "rec1")

    def test_only_recorder_can_submit(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.s.submit_reading("R-9", "M-COLD", "2026-09", 1.0, "op")
        with self.assertRaises(PermissionDenied):
            self.s.submit_reading("R-9", "M-COLD", "2026-09", 1.0, "co_a")

    def test_correction_creates_new_version_and_flags_settlements(self) -> None:
        self.s.submit_reading("R-0001", "M-PUB", "2026-09", 5000.0, "rec1")
        self.s.submit_driver("M-PUB", "2026-09", "甲企业", "B-A1", 3.0)
        self.s.submit_driver("M-PUB", "2026-09", "乙企业", "B-B1", 2.0)
        self.s.create_allocation("AL-09", "M-PUB", "2026-09", 1000.0)

        self.s.correct_reading("R-0001B", "M-PUB", "2026-09", 4000.0, "rec2")

        self.assertTrue(self.s.readings["R-0001"]["superseded"])
        self.assertEqual(self.s.readings["R-0001B"]["value"], 4000.0)
        for settlement_id in ("AL-09:甲企业", "AL-09:乙企业"):
            self.assertIsNotNone(self.s.settlements[settlement_id]["flagged"])
        review_keys = {t["key"] for t in self.s.open_tasks()}
        self.assertIn("review:AL-09:甲企业", review_keys)
        self.assertIn("review:AL-09:乙企业", review_keys)

    def test_correction_requires_existing_reading(self) -> None:
        with self.assertRaises(UnknownReference):
            self.s.correct_reading("R-X", "M-PUB", "2026-08", 1.0, "rec1")
        self.s.submit_reading("R-1", "M-PUB", "2026-09", 1.0, "rec1")
        with self.assertRaises(IdConflict):
            self.s.correct_reading("R-1", "M-PUB", "2026-09", 2.0, "rec1")

    def test_stopped_meter_rejects_new_readings_but_not_closed_period(self) -> None:
        self.s.submit_reading("R-0001", "M-PUB", "2026-09", 5000.0, "rec1")
        self.s.submit_driver("M-PUB", "2026-09", "甲企业", "B-A1", 3.0)
        self.s.create_allocation("AL-09", "M-PUB", "2026-09", 1000.0)
        # 该周期已完成分摊结算：停机不能追溯改动它
        with self.assertRaisesRegex(IncidentScopeError, "已完成分摊结算"):
            self.s.report_incident(
                "meter_stop", "op", meter_id="M-PUB", period="2026-09"
            )
        # 未结算周期可以停机，停机后不能再录入
        self.s.report_incident(
            "meter_stop", "op", meter_id="M-PUB", period="2026-10"
        )
        with self.assertRaisesRegex(IncidentScopeError, "已停机"):
            self.s.submit_reading("R-10", "M-PUB", "2026-10", 1.0, "rec1")


if __name__ == "__main__":
    unittest.main()
