"""箱货拆分合并谱系与库位温区规则。"""

import unittest

from src.hub.errors import IdConflict, IncidentScopeError
from tests.world import build_world


class LineageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = build_world()

    def test_split_preserves_company_zone_and_completed_stages(self) -> None:
        self.s.assign_window("B-A1", "W501")
        self.s.confirm_stage("B-A1", "arrival", "op")
        self.s.confirm_stage("B-A1", "unload", "op")
        self.s.place_batch("B-A1", "A-01-03", "冷藏2-8℃")

        self.s.split_batch("B-A1", [("B-A1-X", 60), ("B-A1-Y", 40)])

        self.assertFalse(self.s.batches["B-A1"]["active"])
        for child in ("B-A1-X", "B-A1-Y"):
            batch = self.s.batches[child]
            self.assertEqual(batch["company"], "甲企业")
            self.assertEqual(batch["zone"], "冷藏2-8℃")
            self.assertEqual(batch["location"], "A-01-03")
            self.assertEqual(batch["window"], "W501")
            # 已完成的到场、卸货随拆分继承，质检起需逐段再确认
            self.assertIn("arrival", self.s.stages[child])
            self.assertIn("unload", self.s.stages[child])
            self.assertNotIn("qc", self.s.stages[child])

    def test_split_quantities_must_conserve(self) -> None:
        with self.assertRaisesRegex(ValueError, "数量之和必须等于"):
            self.s.split_batch("B-A1", [("B-X", 90), ("B-Y", 5)])
        with self.assertRaises(IdConflict):
            self.s.split_batch("B-A2", [("B-A1", 60)])

    def test_merge_combines_same_company_order_and_inherits_common_stages(self) -> None:
        for bid in ("B-A1", "B-A2"):
            self.s.confirm_stage(bid, "arrival", "op", at="2026-10-01T08:00")
            self.s.confirm_stage(bid, "unload", "op", at="2026-10-01T09:00")
        self.s.merge_batches(["B-A1", "B-A2"], "B-A12")
        merged = self.s.batches["B-A12"]
        self.assertEqual(merged["qty"], 160)
        self.assertEqual(merged["company"], "甲企业")
        self.assertIn("unload", self.s.stages["B-A12"])

    def test_merge_across_companies_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "同一企业同一订单"):
            self.s.merge_batches(["B-A1", "B-B1"], "B-AB")

    def test_inactive_batch_cannot_split_again(self) -> None:
        self.s.split_batch("B-A1", [("B-A1-X", 100)])
        with self.assertRaises(IncidentScopeError):
            self.s.split_batch("B-A1", [("B-A1-Z", 100)])


if __name__ == "__main__":
    unittest.main()
