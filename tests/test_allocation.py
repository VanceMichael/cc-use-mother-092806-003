"""跨租户能耗分摊、碳排换算、争议减免与职责分离。"""

import unittest

from src.hub.errors import (
    AllocationError,
    PermissionDenied,
)
from tests.world import build_world


class AllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = build_world()

    def _settle(self) -> None:
        self.s.submit_reading("R-0901", "M-PUB", "2026-09", 5000.0, "rec1")
        self.s.submit_driver("M-PUB", "2026-09", "甲企业", "B-A1", 3.0)
        self.s.submit_driver("M-PUB", "2026-09", "甲企业", "B-A2", 2.0)
        self.s.submit_driver("M-PUB", "2026-09", "乙企业", "B-B1", 5.0)
        self.s.create_allocation("AL-09", "M-PUB", "2026-09", 2000.0)

    def test_energy_fee_and_carbon_split_by_driver(self) -> None:
        self._settle()
        a = self.s.settlements["AL-09:甲企业"]
        b = self.s.settlements["AL-09:乙企业"]
        self.assertEqual(a["energy_kwh"], 2500.0)
        self.assertEqual(b["energy_kwh"], 2500.0)
        self.assertEqual(a["fee"], 1000.0)
        self.assertEqual(b["fee"], 1000.0)
        # 碳排 = 电量 × 因子 0.581
        self.assertEqual(a["carbon_kg"], round(2500.0 * 0.581, 6))
        self.assertEqual(a["factor_id"], "CF-2025")
        # 甲企业的分摊覆盖其两个批次
        self.assertEqual(a["batches"], ["B-A1", "B-A2"])

    def test_allocation_requires_reading_drivers_and_factor(self) -> None:
        with self.assertRaisesRegex(AllocationError, "尚无有效读数"):
            self.s.create_allocation("AL-x", "M-PUB", "2026-08", 100.0)
        self.s.submit_reading("R-1", "M-PUB", "2026-08", 100.0, "rec1")
        with self.assertRaisesRegex(AllocationError, "缺少分摊驱动量"):
            self.s.create_allocation("AL-x", "M-PUB", "2026-08", 100.0)

    def test_driver_must_belong_to_batch_company(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.s.submit_driver("M-PUB", "2026-09", "甲企业", "B-B1", 1.0)

    def test_recorder_cannot_approve_own_reading_relief(self) -> None:
        self._settle()
        # 乙企业对本方费用提争议
        self.s.raise_dispute("AL-09:乙企业", "D-1", "co_b", "冷藏区用电计量存疑")
        # 录入人不能审批（即便误配了审核角色，同一人也不行）
        with self.assertRaises(PermissionDenied):
            self.s.grant_relief("D-1", "rec1", 100.0)
        # 独立审核人可以批准
        self.s.grant_relief("D-1", "aud", 100.0)
        settlement = self.s.settlements["AL-09:乙企业"]
        self.assertEqual(settlement["status"], "relieved")
        self.assertEqual(settlement["relief"], 100.0)
        # 已处理的争议不能重复批准
        from src.hub.errors import IncidentScopeError

        with self.assertRaises(IncidentScopeError):
            self.s.grant_relief("D-1", "aud", 10.0)

    def test_company_cannot_dispute_other_company_fee(self) -> None:
        self._settle()
        with self.assertRaises(PermissionDenied):
            self.s.raise_dispute("AL-09:乙企业", "D-2", "co_a", "不是我的费用")

    def test_non_approver_role_cannot_grant_relief(self) -> None:
        self._settle()
        self.s.raise_dispute("AL-09:甲企业", "D-3", "co_a", "费用异常")
        with self.assertRaises(PermissionDenied):
            self.s.grant_relief("D-3", "op", 50.0)

    def test_trace_settlement_reconstructs_goods_meter_and_approval(self) -> None:
        self.s.submit_reading("R-0901", "M-PUB", "2026-09", 5000.0, "rec1")
        self.s.submit_driver("M-PUB", "2026-09", "甲企业", "B-A1", 3.0)
        self.s.create_allocation("AL-09", "M-PUB", "2026-09", 2000.0)
        # 批次随后拆分并走完链路
        self.s.split_batch("B-A1", [("B-A1-X", 100)])
        self.s.assign_window("B-A1-X", "W501")
        for stage in ("arrival", "unload", "qc", "putaway", "rail_handover"):
            self.s.confirm_stage("B-A1-X", stage, "op", at="2026-10-14T09:00")
        # 读数更正出新版
        self.s.correct_reading("R-0901B", "M-PUB", "2026-09", 4800.0, "rec2")
        # 争议减免由独立审核人批准
        self.s.raise_dispute("AL-09:甲企业", "D-9", "co_a", "更正后申请差额减免")
        self.s.grant_relief("D-9", "aud", 20.0)

        trace = self.s.trace_settlement("AL-09:甲企业", "co_a")

        # 货物去向：结算批次 B-A1 已拆出 B-A1-X 并交铁路
        goods = trace["goods"][0]
        self.assertEqual(goods["batch_id"], "B-A1")
        self.assertEqual(goods["lineage"]["children"], ["B-A1-X"])
        self.assertEqual(goods["window"]["window_id"], "W501")
        self.assertEqual(goods["next_unfinished"], [])
        self.assertIn("交铁路", goods["stages"])
        # 仪表版本：两版读数，旧版被取代、新版当前
        versions = trace["meter"]["versions"]
        self.assertEqual([v["receipt"] for v in versions], ["R-0901", "R-0901B"])
        self.assertTrue(versions[0]["superseded"])
        self.assertTrue(versions[1]["current"])
        # 结算固化的仍是生成时的回执（可解释差异来源）
        self.assertEqual(trace["meter"]["pinned_receipt"], "R-0901")
        # 碳排依据
        self.assertEqual(trace["carbon_basis"]["factor_id"], "CF-2025")
        self.assertIn("排放因子", trace["carbon_basis"]["basis"])
        # 审批依据：审批人与读数录入人不同
        self.assertEqual(trace["approval"]["approver"], "aud")
        self.assertTrue(trace["approval"]["recorder_segregated"])

    def test_tenant_cannot_trace_other_company_settlement(self) -> None:
        self._settle()
        with self.assertRaises(PermissionDenied):
            self.s.trace_settlement("AL-09:乙企业", "co_a")


if __name__ == "__main__":
    unittest.main()
