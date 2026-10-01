"""系统恢复（快照/续跑/告警）与分摊端到端溯源。"""

import json
import tempfile
import unittest
from pathlib import Path

from src.hub import (
    Allocation,
    Batch,
    ExceptionType,
    HubService,
    MeterGap,
    MeterKind,
    Repository,
    ReviewStatus,
    Role,
    Stage,
)


class RecoveryTraceTest(unittest.TestCase):
    def build_state(self) -> tuple[HubService, dict[str, str]]:
        svc = HubService()
        svc.register_company("C1", "华南冷链租户", Role.TENANT)
        svc.register_company("C2", "东盟货主企业", Role.TENANT)
        op = svc.register_user("u-op", "运营甲", "C1", Role.OPERATOR)
        reader = svc.register_user("u-meter", "抄表员丙", "C1", Role.METER_TEAM)
        reviewer = svc.register_user("u-rev", "复核丁", "C1", Role.REVIEWER)
        c1 = svc.register_user("u-c1", "C1 商务", "C1", Role.TENANT)
        auditor = svc.register_user("u-audit", "运营审计", "C1", Role.AUDITOR)

        svc.register_meter("M-PUB", MeterKind.PUBLIC, "冷库公共总表",
                           serves_temp_zones=["frozen"])
        svc.add_carbon_factor("F-1", "华南电网2026排放因子", 0.45,
                              "2026-01-01T00:00", 1)
        svc.register_order("O-1", "C1", "杜伊斯堡")
        svc.register_order("O-2", "C2", "胡志明")
        svc.register_location("L-F1", "frozen", "frozen")
        svc.register_location("L-F2", "frozen", "frozen")
        svc.register_rail_window("W-1", "X8421", "杜伊斯堡",
                                 "2026-09-05T18:00", "2026-09-06T08:00")

        b1 = svc.receive_batch(op, "O-1", "SKU-F", 120, "frozen",
                               "2026-09-01T08:00")
        for stage, at in ((Stage.UNLOAD, "2026-09-01T09:00"),
                          (Stage.QC, "2026-09-01T10:00")):
            svc.confirm_stage(op, b1.id, stage, at)
        svc.assign_location(op, b1.id, "L-F1")
        svc.confirm_stage(op, b1.id, Stage.PUTAWAY, "2026-09-01T12:00")
        svc.assign_window(op, b1.id, "W-1")

        b2 = svc.receive_batch(op, "O-2", "SKU-V", 180, "frozen",
                               "2026-09-01T08:30")
        for stage, at in ((Stage.UNLOAD, "2026-09-01T09:30"),
                          (Stage.QC, "2026-09-01T10:30")):
            svc.confirm_stage(op, b2.id, stage, at)
        svc.assign_location(op, b2.id, "L-F2")
        svc.confirm_stage(op, b2.id, Stage.PUTAWAY, "2026-09-01T12:30")
        svc.assign_window(op, b2.id, "W-1")

        # 一笔已分摊待复核的费用
        svc.submit_reading(reader, "M-PUB", "2026-09-01T00:00", 1000.0, "P-1")
        svc.submit_reading(reader, "M-PUB", "2026-09-02T00:00", 1600.0, "P-2")
        allocation = svc.allocate_usage(op, "M-PUB", "2026-09-01T00:00",
                                        "2026-09-02T00:00", unit_price=1.0)
        # 9-02 后停机，缺口尚未补
        svc.report_meter_outage(reader, "M-PUB", "2026-09-03T00:00",
                                "2026-09-03T06:00", "通信中断")
        return svc, {"op": op.id, "reader": reader.id, "reviewer": reviewer.id,
                     "c1": c1.id, "auditor": auditor.id,
                     "b1": b1.id, "b2": b2.id, "allocation": allocation.id}

    def test_snapshot_restore_and_recover_resumes_work(self) -> None:
        svc, ids = self.build_state()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            svc.repo.save_snapshot(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("stores", raw)

            # 系统重启：空仓储载入快照
            restarted = HubService(Repository())
            restarted.repo.load_snapshot(path)
            self.assertEqual(len(restarted.repo.list(Batch)), 2)
            self.assertEqual(restarted.repo.get(Allocation, ids["allocation"]).usage,
                             600.0)

            # 恢复时刻距截关不足 24 小时：三类告警全部回来
            alerts = restarted.recover("2026-09-04T20:00")
            kinds = {a.kind for a in alerts}
            self.assertEqual(kinds, {"meter_gap", "cutoff", "review"})
            cutoff_alert = next(a for a in alerts if a.kind == "cutoff")
            self.assertIn("2 批未交", cutoff_alert.message)

            # recover 幂等：不产生重复告警
            again = restarted.recover("2026-09-04T20:05")
            self.assertEqual(len(again), len(alerts))

            # 续跑：补抄解除缺口告警
            restarted.submit_reading(ids["reader"], "M-PUB", "2026-09-03T03:00",
                                     1750.0, "P-3")
            gap = restarted.repo.list(MeterGap)[0]
            self.assertIsNotNone(gap.resolved_reading_id)
            # 复核解除结算告警
            restarted.confirm_review(ids["reviewer"], ids["allocation"])
            # 截关后关闭窗口解除截关告警
            restarted.close_window(ids["op"], "W-1", "2026-09-05T18:00")
            remaining = restarted.recover("2026-09-05T18:01")
            self.assertEqual(remaining, [])
            self.assertEqual(restarted.pending_alerts(), [])

            # 编号生成器也从快照续号，不与既有编号冲突
            new_gap = restarted.schedule_meter_reading(
                ids["reader"], "M-PUB", "2026-09-04T00:00", "2026-09-04T06:00")
            self.assertTrue(new_gap.id.startswith("GAP-"))

    def test_recover_flags_overdue_and_recomputes_pending_count(self) -> None:
        svc, ids = self.build_state()
        alerts = svc.recover("2026-09-05T20:00")  # 已过截关
        cutoff = next(a for a in alerts if a.kind == "cutoff")
        self.assertIn("已超过截关时间", cutoff.message)

        # 一批交铁路后，告警未交数量随之更新
        svc.confirm_stage(ids["op"], ids["b1"], Stage.RAIL_HANDOVER,
                          "2026-09-05T16:00")
        alerts2 = svc.recover("2026-09-05T17:00")
        cutoff2 = next(a for a in alerts2 if a.kind == "cutoff")
        self.assertIn("1 批未交", cutoff2.message)

    def test_trace_allocation_reconstructs_full_picture(self) -> None:
        svc, ids = self.build_state()
        # 在 b1 上做改配与破损，验证溯源能呈现谱系与异常
        svc.report_exception(ids["op"], ids["b1"], ExceptionType.DAMAGE,
                             Stage.RAIL_HANDOVER, 10, "2026-09-05T10:00",
                             note="叉车剐蹭")
        svc.confirm_review(ids["reviewer"], ids["allocation"])
        dispute = svc.raise_dispute(ids["c1"], ids["allocation"], "占用量异议", 20.0)
        svc.decide_dispute(ids["reviewer"], dispute.id, True, granted_relief=20.0,
                           note="调减 20kWh")

        trace = svc.trace_allocation(ids["auditor"], ids["allocation"])

        # 仪表版本：相邻两版读数及回执
        versions = {(r["read_at"], r["version"]) for r in trace["reading_versions"]}
        self.assertEqual(versions, {("2026-09-01T00:00", 1),
                                    ("2026-09-02T00:00", 2)})
        # 碳排换算依据
        self.assertEqual(trace["carbon"]["source"], "华南电网2026排放因子")
        self.assertEqual(trace["carbon"]["factor_version"], 1)
        self.assertEqual(trace["carbon"]["total_kg"], 270.0)
        # 审批依据
        self.assertEqual(trace["review"]["status"], ReviewStatus.CONFIRMED.value)
        self.assertEqual(trace["review"]["confirmed_by"], ids["reviewer"])
        self.assertEqual(trace["dispute"]["granted_relief"], 20.0)
        self.assertEqual(trace["dispute"]["decided_by"], ids["reviewer"])
        # 货物去向：两家企业的在库批次、节点、谱系、异常
        cargo_companies = {c["company_id"] for c in trace["cargo"]}
        self.assertEqual(cargo_companies, {"C1", "C2"})
        c1_entry = next(c for c in trace["cargo"] if c["company_id"] == "C1")
        stages = {x["stage"] for x in c1_entry["confirmations"]}
        self.assertIn(Stage.PUTAWAY, stages)
        self.assertNotIn(Stage.RAIL_HANDOVER, stages)
        # C1 分摊行对应原批 b1，破损子批次作为后代可追到去向
        self.assertEqual(c1_entry["lineage"]["ancestors"], [])
        self.assertEqual(len(c1_entry["lineage"]["descendants"]), 1)
        self.assertEqual(c1_entry["exceptions"][0]["batch_id"],
                         c1_entry["lineage"]["descendants"][0])
        self.assertEqual(c1_entry["exceptions"][0]["type"], "damage")
        self.assertEqual(c1_entry["exceptions"][0]["quantity"], 10)

        # 企业视角：只看到本方货物，看不到 C2 的分摊行
        own = svc.trace_allocation(ids["c1"], ids["allocation"])
        self.assertEqual({c["company_id"] for c in own["cargo"]}, {"C1"})

        # 与本方无关的企业不能溯源该笔分摊
        svc.register_company("CX", "无关企业", Role.TENANT)
        outsider = svc.register_user("u-x", "外人", "CX", Role.TENANT)
        with self.assertRaises(Exception):
            svc.trace_allocation(outsider, ids["allocation"])


if __name__ == "__main__":
    unittest.main()
