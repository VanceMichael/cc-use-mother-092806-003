"""租户隔离、职责分离、结算复核与争议减免。"""

import unittest

from src.hub import (
    AllocationStatus,
    AuthError,
    DisputeStatus,
    DomainError,
    HubService,
    MeterKind,
    Role,
    Stage,
)


class AccessSettlementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = HubService()
        self.svc.register_company("C1", "华南冷链租户", Role.TENANT)
        self.svc.register_company("C2", "东盟货主企业", Role.TENANT)
        self.op = self.svc.register_user("u-op", "运营甲", "C1", Role.OPERATOR)
        self.reader = self.svc.register_user("u-meter", "抄表员丙", "C1",
                                             Role.METER_TEAM)
        self.reviewer = self.svc.register_user("u-rev", "复核丁", "C1",
                                               Role.REVIEWER)
        self.c1_user = self.svc.register_user("u-c1", "C1 商务", "C1", Role.TENANT)
        self.c2_user = self.svc.register_user("u-c2", "C2 商务", "C2", Role.TENANT)

        self.svc.register_meter("M-PUB", MeterKind.PUBLIC, "冷库公共总表",
                                serves_temp_zones=["frozen"])
        self.svc.add_carbon_factor("F-1", "华南电网2026排放因子", 0.45,
                                   "2026-01-01T00:00", 1)
        self.svc.register_order("O-1", "C1", "杜伊斯堡")
        self.svc.register_order("O-2", "C2", "胡志明")
        self.svc.register_location("L-F1", "frozen", "frozen")
        self.svc.register_location("L-F2", "frozen", "frozen")
        self._putaway("O-1", "L-F1", 120)
        self._putaway("O-2", "L-F2", 180)
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-01T00:00", 1000.0,
                                "P-1")
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-02T00:00", 1600.0,
                                "P-2")
        self.allocation = self.svc.allocate_usage(
            self.op, "M-PUB", "2026-09-01T00:00", "2026-09-02T00:00",
            unit_price=1.0)

    def _putaway(self, order_id: str, location: str, qty: int) -> None:
        batch = self.svc.receive_batch(self.op, order_id, "SKU-F", qty, "frozen",
                                       "2026-09-01T08:00")
        self.svc.confirm_stage(self.op, batch.id, Stage.UNLOAD, "2026-09-01T09:00")
        self.svc.confirm_stage(self.op, batch.id, Stage.QC, "2026-09-01T10:00")
        self.svc.assign_location(self.op, batch.id, location)
        self.svc.confirm_stage(self.op, batch.id, Stage.PUTAWAY, "2026-09-01T12:00")

    # ------------------------------------------------------------------
    # 租户隔离
    # ------------------------------------------------------------------

    def test_tenant_sees_only_own_batches(self) -> None:
        c1_batches = {b.order_id for b in self.svc.list_batches(self.c1_user)}
        c2_batches = {b.order_id for b in self.svc.list_batches(self.c2_user)}
        self.assertEqual(c1_batches, {"O-1"})
        self.assertEqual(c2_batches, {"O-2"})

    def test_tenant_allocation_view_masks_other_lines(self) -> None:
        visible = self.svc.list_allocations(self.c1_user)
        self.assertEqual(len(visible), 1)
        self.assertEqual({line.company_id for line in visible[0].lines}, {"C1"})
        # C2 看不到 C1 的费用，反之亦然
        self.assertFalse(any(
            line.company_id == "C1"
            for a in self.svc.list_allocations(self.c2_user)
            for line in a.lines))

    def test_meter_team_cannot_operate_batches(self) -> None:
        from src.hub import Batch
        batch = self.svc.repo.list(Batch)[0]
        with self.assertRaises(AuthError):
            self.svc.split_batch(self.reader, batch.id, [1], "2026-09-02T10:00")

    def test_tenant_cannot_confirm_other_order_stage(self) -> None:
        from src.hub import Batch
        c2_batch = next(b for b in self.svc.repo.list(Batch) if b.order_id == "O-2")
        with self.assertRaises(AuthError):
            self.svc.confirm_stage(self.c1_user, c2_batch.id, Stage.QC,
                                   "2026-09-02T10:00")

    # ------------------------------------------------------------------
    # 结算复核与争议
    # ------------------------------------------------------------------

    def test_dispute_requires_confirmed_review(self) -> None:
        # 待复核状态不能直接提争议
        with self.assertRaises(DomainError):
            self.svc.raise_dispute(self.c1_user, self.allocation.id, "费用异常", 10.0)

    def test_separation_of_duties(self) -> None:
        # 只有复核角色能确认复核
        with self.assertRaises(AuthError):
            self.svc.confirm_review(self.op, self.allocation.id)
        self.svc.confirm_review(self.reviewer, self.allocation.id)
        self.assertEqual(self.allocation.status, AllocationStatus.CONFIRMED)

        # 企业提出争议
        dispute = self.svc.raise_dispute(self.c1_user, self.allocation.id,
                                         "温区占用箱量统计偏高", 60.0)
        self.assertEqual(self.allocation.status, AllocationStatus.DISPUTED)

        # 抄表录入人不能批准争议减免
        with self.assertRaises(AuthError):
            self.svc.decide_dispute(self.reader, dispute.id, True, 60.0)
        # 独立复核人驳回 C1 的争议
        self.svc.register_company("C3", "第三方审核机构", Role.TENANT)
        independent = self.svc.register_user("u-ind", "独立复核", "C3",
                                             Role.REVIEWER)
        self.svc.decide_dispute(independent, dispute.id, False)
        self.assertEqual(dispute.status, DisputeStatus.REJECTED)

        # C2 再提争议，由独立复核人批准减免
        c2_dispute = self.svc.raise_dispute(self.c2_user, self.allocation.id,
                                            "停机时段电费不应计入", 30.0)
        decided = self.svc.decide_dispute(independent, c2_dispute.id, True,
                                          granted_relief=30.0, note="核查属实")
        self.assertEqual(decided.status, DisputeStatus.APPROVED)
        self.assertEqual(self.allocation.status, AllocationStatus.ADJUSTED)
        self.assertEqual(self.allocation.adjustment, 30.0)
        # 减免后总用量 570，按 40/60 重新落到各企业
        self.assertAlmostEqual(self.allocation.payable_usage(), 570.0)
        self.assertAlmostEqual(self.allocation.amount_for("C2"), 342.0, places=2)

    def test_disputant_cannot_approve_own_dispute(self) -> None:
        self.svc.confirm_review(self.reviewer, self.allocation.id)
        # C1 的复核角色用户提争议，本人不得审批
        c1_reviewer = self.svc.register_user("u-c1-rev", "C1复核", "C1",
                                             Role.REVIEWER)
        dispute = self.svc.raise_dispute(c1_reviewer, self.allocation.id,
                                         "本方费用异议", 20.0)
        with self.assertRaises(AuthError):
            self.svc.decide_dispute(c1_reviewer, dispute.id, True, 20.0)


if __name__ == "__main__":
    unittest.main()
