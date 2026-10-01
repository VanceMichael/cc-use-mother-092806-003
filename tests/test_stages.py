"""五段链路、班列截关与异常影响面规则。"""

import unittest

from src.hub.errors import (
    CutoffViolation,
    IncidentScopeError,
    PermissionDenied,
    SequenceViolation,
)
from tests.world import build_world, walk_to_rail


class StageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = build_world()

    def test_stages_must_be_confirmed_in_order(self) -> None:
        self.s.assign_window("B-A1", "W501")
        with self.assertRaisesRegex(SequenceViolation, "先完成车辆到场"):
            self.s.confirm_stage("B-A1", "unload", "op")
        self.s.confirm_stage("B-A1", "arrival", "op")
        with self.assertRaisesRegex(SequenceViolation, "先完成卸货"):
            self.s.confirm_stage("B-A1", "qc", "op")

    def test_rail_handover_checks_cutoff(self) -> None:
        self.s.assign_window("B-A1", "W501")
        for stage in ("arrival", "unload", "qc", "putaway"):
            self.s.confirm_stage("B-A1", stage, "op")
        with self.assertRaisesRegex(CutoffViolation, "晚于班列截关"):
            self.s.confirm_stage(
                "B-A1", "rail_handover", "op", at="2026-10-15T19:30"
            )
        # 截关前可以完成
        event = self.s.confirm_stage(
            "B-A1", "rail_handover", "op", at="2026-10-15T16:00"
        )
        self.assertEqual(event["data"]["stage"], "rail_handover")

    def test_rail_handover_requires_window(self) -> None:
        self.s.register_batch("B-A3", "ORD-1", 10)
        for stage in ("arrival", "unload", "qc", "putaway"):
            self.s.confirm_stage("B-A3", stage, "op")
        with self.assertRaisesRegex(CutoffViolation, "绑定班列窗口"):
            self.s.confirm_stage("B-A3", "rail_handover", "op")

    def test_same_stage_confirmation_is_idempotent(self) -> None:
        self.s.confirm_stage("B-A1", "arrival", "op", at="2026-10-01T08:00")
        before = len(self.s.events)
        self.s.confirm_stage("B-A1", "arrival", "op", at="2026-10-01T09:00")
        self.assertEqual(len(self.s.events), before)

    def test_damage_only_blocks_unfinished_chain(self) -> None:
        self.s.confirm_stage("B-A1", "arrival", "op")
        self.s.confirm_stage("B-A1", "unload", "op")
        event = self.s.report_incident("damage", "op", batch_id="B-A1")
        # 已完成的两段保留；受影响的只有未完成三段
        self.assertEqual(
            event["data"]["affected_unfinished"], ["qc", "putaway", "rail_handover"]
        )
        with self.assertRaisesRegex(IncidentScopeError, "破损"):
            self.s.confirm_stage("B-A1", "qc", "op")
        self.assertIn("unload", self.s.stages["B-A1"])

    def test_reassign_after_rail_is_rejected(self) -> None:
        walk_to_rail(self.s, "B-A1", "W501")
        with self.assertRaisesRegex(IncidentScopeError, "不能回溯"):
            self.s.report_incident(
                "reassign", "op", batch_id="B-A1", new_window="W502"
            )

    def test_reassign_mid_chain_switches_window(self) -> None:
        self.s.assign_window("B-A1", "W501")
        self.s.confirm_stage("B-A1", "arrival", "op")
        self.s.report_incident(
            "reassign", "op", batch_id="B-A1", new_window="W502"
        )
        self.assertEqual(self.s.batches["B-A1"]["window"], "W502")
        # 新窗口的截关提醒已排上，旧窗口提醒仍在但可由运营去重处理
        keys = {t["key"] for t in self.s.open_tasks()}
        self.assertIn("cutoff:B-A1:W502", keys)

    def test_tenant_cannot_confirm_other_company_goods(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.s.confirm_stage("B-B1", "arrival", "co_a")
        # 本方货物可以确认
        self.s.confirm_stage("B-A1", "arrival", "co_a")


if __name__ == "__main__":
    unittest.main()
