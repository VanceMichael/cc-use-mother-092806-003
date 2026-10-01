"""企业数据可见性隔离。"""

import unittest

from tests.world import build_world


class VisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = build_world()
        self.s.submit_reading("R-1", "M-PUB", "2026-09", 5000.0, "rec1")
        self.s.submit_driver("M-PUB", "2026-09", "甲企业", "B-A1", 3.0)
        self.s.submit_driver("M-PUB", "2026-09", "乙企业", "B-B1", 7.0)
        self.s.create_allocation("AL-09", "M-PUB", "2026-09", 2000.0)

    def test_tenant_sees_only_own_batches(self) -> None:
        a_batches = {b["batch_id"] for b in self.s.visible_batches("co_a")}
        self.assertEqual(a_batches, {"B-A1", "B-A2"})
        b_batches = {b["batch_id"] for b in self.s.visible_batches("co_b")}
        self.assertEqual(b_batches, {"B-B1"})

    def test_tenant_sees_only_own_settlements(self) -> None:
        a_fees = {s["settlement_id"] for s in self.s.visible_settlements("co_a")}
        self.assertEqual(a_fees, {"AL-09:甲企业"})
        b_fees = {s["settlement_id"] for s in self.s.visible_settlements("co_b")}
        self.assertEqual(b_fees, {"AL-09:乙企业"})

    def test_operator_sees_everything(self) -> None:
        self.assertEqual(
            {b["batch_id"] for b in self.s.visible_batches("op")},
            {"B-A1", "B-A2", "B-B1"},
        )
        self.assertEqual(
            len(self.s.visible_settlements("op")), 2
        )


if __name__ == "__main__":
    unittest.main()
