"""批次谱系、逐段确认、异常只影响未完成链路、截关规则。"""

import unittest

from src.hub import (
    Batch,
    BatchStatus,
    CutoffError,
    DomainError,
    ExceptionType,
    HubService,
    Role,
    Stage,
)


class LineageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = HubService()
        self.svc.register_company("C1", "华南冷链租户", Role.TENANT)
        self.svc.register_company("C3", "改配接收企业", Role.TENANT)
        self.op = self.svc.register_user("u-op", "运营甲", "C1", Role.OPERATOR)
        self.svc.register_order("O-1", "C1", "杜伊斯堡")
        self.svc.register_order("O-2", "C3", "阿拉木图")
        for loc_id in ("L-F1", "L-F2"):
            self.svc.register_location(loc_id, "frozen", "frozen")
        self.win = self.svc.register_rail_window(
            "W-1", "X8421", "杜伊斯堡", "2026-09-05T18:00", "2026-09-06T08:00")
        self.win2 = self.svc.register_rail_window(
            "W-2", "X7602", "阿拉木图", "2026-09-07T18:00", "2026-09-08T08:00")

    def test_split_merge_and_stage_order(self) -> None:
        b1 = self.svc.receive_batch(self.op, "O-1", "SKU-F", 100, "frozen",
                                    "2026-09-01T08:00")
        # 未卸货不能质检/上架
        with self.assertRaises(DomainError):
            self.svc.confirm_stage(self.op, b1.id, Stage.QC, "2026-09-01T10:00")
        self.svc.confirm_stage(self.op, b1.id, Stage.UNLOAD, "2026-09-01T09:00")
        self.svc.confirm_stage(self.op, b1.id, Stage.QC, "2026-09-01T10:00")

        # 改配 20 箱到 O-2：子批次只继承改配节点之前的确认
        event = self.svc.report_exception(
            self.op, b1.id, ExceptionType.REASSIGN,
            Stage.PUTAWAY, 20, "2026-09-01T11:00",
            target_order_id="O-2", target_window_id="W-2")
        b_reassign = self.svc.repo.get(Batch, event.batch_id)
        self.assertEqual(b_reassign.order_id, "O-2")
        self.assertEqual(b_reassign.parent_ids, [b1.id])
        self.assertEqual(set(b_reassign.confirmations),
                         {Stage.ARRIVAL.value, Stage.UNLOAD.value, Stage.QC.value})
        self.assertNotIn(Stage.PUTAWAY.value, b_reassign.confirmations)
        self.assertEqual(b1.quantity, 80)

        # 普通拆分 30 箱，子批次继承全部已完成节点
        b_split = self.svc.split_batch(self.op, b1.id, [30], "2026-09-01T12:00")[0]
        self.assertEqual(set(b_split.confirmations), set(b1.confirmations))
        self.assertEqual(b1.quantity, 50)

        # 同订单同温区再收一批并合并，只继承共同节点
        b2 = self.svc.receive_batch(self.op, "O-1", "SKU-F", 5, "frozen",
                                    "2026-09-02T08:00")
        self.svc.confirm_stage(self.op, b2.id, Stage.UNLOAD, "2026-09-02T09:00")
        self.svc.confirm_stage(self.op, b2.id, Stage.QC, "2026-09-02T10:00")
        merged = self.svc.merge_batches(self.op, [b1.id, b2.id], "2026-09-02T11:00")
        self.assertEqual(merged.quantity, 55)
        self.assertEqual(set(merged.parent_ids), {b1.id, b2.id})
        self.assertEqual(b1.status, BatchStatus.CLOSED)  # 并尽即终止

        # 上架必须先有库位
        with self.assertRaises(DomainError):
            self.svc.confirm_stage(self.op, merged.id, Stage.PUTAWAY,
                                   "2026-09-02T14:00")
        self.svc.assign_location(self.op, merged.id, "L-F1")
        self.svc.assign_location(self.op, b_split.id, "L-F2")
        self.svc.assign_location(self.op, b_reassign.id, "L-F2")
        self.svc.confirm_stage(self.op, merged.id, Stage.PUTAWAY, "2026-09-02T14:00")
        self.svc.confirm_stage(self.op, b_split.id, Stage.PUTAWAY, "2026-09-02T14:30")
        self.svc.confirm_stage(self.op, b_reassign.id, Stage.PUTAWAY, "2026-09-02T15:00")

        # 破损 30 箱发生在交铁路节点：之前节点全部保留，破损子批次终止、不进铁路
        damage = self.svc.report_exception(
            self.op, b_split.id, ExceptionType.DAMAGE,
            Stage.RAIL_HANDOVER, 30, "2026-09-05T09:00", note="箱体挤压")
        b_damage = self.svc.repo.get(Batch, damage.batch_id)
        self.assertEqual(b_damage.status, BatchStatus.CLOSED)
        self.assertIn(Stage.PUTAWAY.value, b_damage.confirmations)
        self.assertNotIn(Stage.RAIL_HANDOVER.value, b_damage.confirmations)

        # 交铁路：超过截关被拒，状态不变
        self.svc.assign_window(self.op, merged.id, "W-1")
        with self.assertRaises(CutoffError):
            self.svc.confirm_stage(self.op, merged.id, Stage.RAIL_HANDOVER,
                                   "2026-09-05T20:00")
        self.assertNotIn(Stage.RAIL_HANDOVER.value, merged.confirmations)
        self.svc.confirm_stage(self.op, merged.id, Stage.RAIL_HANDOVER,
                               "2026-09-05T16:00")
        self.svc.assign_window(self.op, b_reassign.id, "W-2")
        self.svc.confirm_stage(self.op, b_reassign.id, Stage.RAIL_HANDOVER,
                               "2026-09-07T16:00")

        self.assertEqual(set(self.win.handed_batch_ids), {merged.id})
        self.assertNotIn(b_damage.id, self.win.handed_batch_ids)
        self.assertEqual(merged.status, BatchStatus.HANDED_OVER)

        # 节点确认幂等
        again = self.svc.confirm_stage(self.op, merged.id, Stage.UNLOAD,
                                       "2026-09-01T09:00")
        self.assertIs(again, merged)
        self.assertEqual(len(merged.confirmations), 5)

    def test_temp_zone_mismatch_rejected(self) -> None:
        self.svc.register_location("L-A1", "ambient", "ambient")
        with self.assertRaises(DomainError):
            self.svc.receive_batch(self.op, "O-1", "SKU-F", 10, "frozen",
                                   "2026-09-03T08:00", location_id="L-A1")


if __name__ == "__main__":
    unittest.main()
