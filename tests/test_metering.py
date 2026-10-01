"""设备计量：回执幂等、同编号冲突隔离、读数版本、停机缺口、跨租户分摊与碳排。"""

import unittest

from src.hub import (
    Allocation,
    AllocationStatus,
    Batch,
    ConflictError,
    DomainError,
    HubService,
    MeterKind,
    Reading,
    ReadingStatus,
    Role,
    Stage,
)


class MeteringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = HubService()
        self.svc.register_company("C1", "华南冷链租户", Role.TENANT)
        self.svc.register_company("C2", "东盟货主企业", Role.TENANT)
        self.op = self.svc.register_user("u-op", "运营甲", "C1", Role.OPERATOR)
        self.reader = self.svc.register_user("u-meter", "抄表员丙", "C1",
                                             Role.METER_TEAM)
        # 公共冷库总表服务 frozen 温区
        self.svc.register_meter("M-PUB", MeterKind.PUBLIC, "冷库公共总表",
                                serves_temp_zones=["frozen"])
        # 冷链私表归属 C1
        self.svc.register_meter("M-C1", MeterKind.COLD_ZONE, "C1 冷链分表",
                                owner_company_id="C1")
        self.svc.add_carbon_factor("F-1", "华南电网2026排放因子", 0.45,
                                   "2026-01-01T00:00", 1)

        # 两家企业各一批 frozen 货物在库：C1 120 箱、C2 180 箱
        self.svc.register_order("O-1", "C1", "杜伊斯堡")
        self.svc.register_order("O-2", "C2", "胡志明")
        self.svc.register_location("L-F1", "frozen", "frozen")
        self.svc.register_location("L-F2", "frozen", "frozen")
        self.b1 = self._putaway("O-1", 120, "L-F1")
        self.b2 = self._putaway("O-2", 180, "L-F2")

    def _putaway(self, order_id: str, qty: int, location: str) -> Batch:
        batch = self.svc.receive_batch(self.op, order_id, "SKU-F", qty, "frozen",
                                       "2026-09-01T08:00")
        self.svc.confirm_stage(self.op, batch.id, Stage.UNLOAD, "2026-09-01T09:00")
        self.svc.confirm_stage(self.op, batch.id, Stage.QC, "2026-09-01T10:00")
        self.svc.assign_location(self.op, batch.id, location)
        self.svc.confirm_stage(self.op, batch.id, Stage.PUTAWAY, "2026-09-01T12:00")
        return batch

    # ------------------------------------------------------------------
    # 回执：幂等与隔离
    # ------------------------------------------------------------------

    def test_duplicate_receipt_is_idempotent(self) -> None:
        r1 = self.svc.submit_reading(self.reader, "M-C1", "2026-09-01T00:00", 100.0,
                                     "RC-1")
        r2 = self.svc.submit_reading(self.reader, "M-C1", "2026-09-01T00:00", 100.0,
                                     "RC-1")
        self.assertIs(r1, r2)
        self.assertEqual(len([r for r in self.svc.repo.list(Reading)
                              if r.receipt_id == "RC-1"]), 1)

    def test_same_receipt_different_value_is_quarantined(self) -> None:
        self.svc.submit_reading(self.reader, "M-C1", "2026-09-01T00:00", 100.0, "RC-2")
        with self.assertRaises(ConflictError):
            self.svc.submit_reading(self.reader, "M-C1", "2026-09-01T00:05", 105.0,
                                    "RC-2")
        quarantined = self.svc.list_quarantined()
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].status, ReadingStatus.QUARANTINED)
        # 隔离读数不产生新版本：已采纳版本仍只有 1 版
        accepted = [r for r in self.svc.repo.list(Reading)
                    if r.meter_id == "M-C1" and r.status is ReadingStatus.ACCEPTED]
        self.assertEqual([r.version for r in accepted], [1])

        # 复核人员采纳隔离读数后成为第 2 版
        reviewer = self.svc.register_user("u-rev", "复核丁", "C1", Role.REVIEWER)
        fixed = self.svc.resolve_quarantine(reviewer, quarantined[0].id, accept=True)
        self.assertEqual(fixed.status, ReadingStatus.ACCEPTED)
        self.assertEqual(fixed.version, 2)

    def test_reading_cannot_go_backwards(self) -> None:
        self.svc.submit_reading(self.reader, "M-C1", "2026-09-01T00:00", 100.0, "RC-3")
        with self.assertRaises(DomainError):
            self.svc.submit_reading(self.reader, "M-C1", "2026-09-02T00:00", 90.0,
                                    "RC-4")

    # ------------------------------------------------------------------
    # 停机缺口
    # ------------------------------------------------------------------

    def test_outage_gap_blocks_allocation_until_resumed(self) -> None:
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-01T00:00", 0.0, "R-1")
        # 9-02 仪表停机
        self.svc.report_meter_outage(self.reader, "M-PUB", "2026-09-02T00:00",
                                     "2026-09-02T12:00", "通讯模块故障")
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-03T00:00", 600.0, "R-2")

        # 停机缺口未补齐：不能分摊
        with self.assertRaisesRegex(DomainError, "抄表缺口"):
            self.svc.allocate_usage(self.op, "M-PUB", "2026-09-01T00:00",
                                    "2026-09-03T00:00")

        # 恢复后补抄 9-02 时点，缺口解除
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-02T12:00", 300.0, "R-3")
        from src.hub import MeterGap
        gaps = self.svc.repo.list(MeterGap)
        self.assertEqual(len(gaps), 1)
        self.assertIsNotNone(gaps[0].resolved_reading_id)

        # 补抄插入历史位置，版本按抄表时点重排：9-01=v1、9-02=v2、9-03=v3
        versions = {r.read_at: r.version
                    for r in self.svc.repo.list(Reading)
                    if r.meter_id == "M-PUB" and r.status is ReadingStatus.ACCEPTED}
        self.assertEqual(versions["2026-09-01T00:00"], 1)
        self.assertEqual(versions["2026-09-02T12:00"], 2)
        self.assertEqual(versions["2026-09-03T00:00"], 3)

        # 相邻版本分摊：9-01 -> 9-02
        alloc1 = self.svc.allocate_usage(self.op, "M-PUB", "2026-09-01T00:00",
                                         "2026-09-02T12:00", unit_price=1.0)
        self.assertEqual(alloc1.usage, 300.0)
        with self.assertRaisesRegex(DomainError, "已分摊"):
            self.svc.allocate_usage(self.op, "M-PUB", "2026-09-01T00:00",
                                    "2026-09-02T12:00", unit_price=1.0)

    # ------------------------------------------------------------------
    # 跨租户分摊与碳排
    # ------------------------------------------------------------------

    def test_public_meter_split_by_occupancy(self) -> None:
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-01T00:00", 1000.0,
                                "P-1")
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-02T00:00", 1600.0,
                                "P-2")
        allocation = self.svc.allocate_usage(self.op, "M-PUB", "2026-09-01T00:00",
                                             "2026-09-02T00:00", unit_price=0.8)
        self.assertEqual(allocation.usage, 600.0)
        # 120 : 180 = 40% : 60%
        self.assertAlmostEqual(allocation.usage_for("C1"), 240.0)
        self.assertAlmostEqual(allocation.usage_for("C2"), 360.0)
        self.assertAlmostEqual(allocation.amount_for("C1"), 192.0, places=2)
        # 碳排：600 kWh * 0.45 = 270 kg；C1 占 40%
        self.assertEqual(allocation.carbon_kg, 270.0)
        self.assertAlmostEqual(allocation.carbon_for("C1"), 108.0, places=2)
        self.assertEqual(allocation.factor_version, 1)

    def test_private_meter_billed_to_owner(self) -> None:
        self.svc.submit_reading(self.reader, "M-C1", "2026-09-01T00:00", 50.0, "Q-1")
        self.svc.submit_reading(self.reader, "M-C1", "2026-09-02T00:00", 80.0, "Q-2")
        allocation = self.svc.allocate_usage(self.op, "M-C1", "2026-09-01T00:00",
                                             "2026-09-02T00:00")
        self.assertEqual(len(allocation.lines), 1)
        self.assertEqual(allocation.lines[0].company_id, "C1")
        self.assertEqual(allocation.lines[0].share, 30.0)

    def test_non_adjacent_versions_rejected(self) -> None:
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-01T00:00", 0.0, "N-1")
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-02T00:00", 100.0, "N-2")
        self.svc.submit_reading(self.reader, "M-PUB", "2026-09-03T00:00", 200.0, "N-3")
        with self.assertRaisesRegex(DomainError, "相邻版本"):
            self.svc.allocate_usage(self.op, "M-PUB", "2026-09-01T00:00",
                                    "2026-09-03T00:00")


if __name__ == "__main__":
    unittest.main()
