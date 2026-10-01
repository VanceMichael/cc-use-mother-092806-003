"""系统恢复后继续抄表缺口、截关提醒与结算复核。"""

import tempfile
import unittest
from pathlib import Path

from src.hub.recovery import EventStore, RecoveryRunner
from tests.world import build_world


def make_handlers(log: list[str]) -> dict:
    def meter_gap(service, task, now):
        log.append(f"抄表缺口提醒:{task['payload']['meter_id']}@{task['payload']['period']}")
        return "缺口提醒已发送"

    def cutoff_reminder(service, task, now):
        log.append(f"截关提醒:{task['payload']['batch_id']}->{task['payload']['train']}")
        return "截关提醒已发送"

    def settlement_review(service, task, now):
        service.recheck_settlement(task["payload"]["settlement_id"], "恢复后复核完成")
        log.append(f"结算复核:{task['payload']['settlement_id']}")
        return "结算复核完成"

    return {
        "meter_gap": meter_gap,
        "cutoff_reminder": cutoff_reminder,
        "settlement_review": settlement_review,
    }


class RecoveryTest(unittest.TestCase):
    def test_state_rebuilds_and_due_tasks_continue_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"

            # —— 第一个进程：登记场景、读数更正产生复核任务、绑定窗口产生截关任务 ——
            runner = RecoveryRunner.boot(path)
            s = runner.service
            build_world(s)
            s.submit_reading("R-1", "M-PUB", "2026-09", 5000.0, "rec1")
            s.submit_driver("M-PUB", "2026-09", "甲企业", "B-A1", 3.0)
            s.submit_driver("M-PUB", "2026-09", "乙企业", "B-B1", 7.0)
            s.create_allocation("AL-09", "M-PUB", "2026-09", 2000.0)
            s.correct_reading("R-1B", "M-PUB", "2026-09", 4800.0, "rec2",
                              at="2026-09-30T10:00")
            s.assign_window("B-A1", "W501")

            pending_before = runner.pending_summary()
            self.assertIn("review:AL-09:甲企业", pending_before["settlement_review"])
            self.assertIn("cutoff:B-A1:W501", pending_before["cutoff_reminder"])
            # 读数先于驱动量到达，不产生抄表缺口任务
            self.assertNotIn("meter_gap", pending_before)

            log: list[str] = []
            # 截关在 10-15，此时不应触发；读数更正复核应触发
            first = runner.run_due("2026-10-01T00:00", make_handlers(log))
            types = {r["type"] for r in first}
            self.assertIn("settlement_review", types)
            self.assertNotIn("cutoff_reminder", types)
            self.assertTrue(s.settlements["AL-09:甲企业"]["rechecked"])
            # 已复核的任务不再是打开状态
            self.assertNotIn(
                "review:AL-09:甲企业",
                runner.pending_summary().get("settlement_review", []),
            )

            # —— 模拟系统崩溃后用新进程从事件流恢复 ——
            runner2 = RecoveryRunner.boot(path)
            s2 = runner2.service
            # 状态完整重建：批次、读数版本、分摊、隔离关系都在
            self.assertEqual(s2.readings["R-1B"]["value"], 4800.0)
            self.assertTrue(s2.readings["R-1"]["superseded"])
            self.assertEqual(s2.settlements["AL-09:乙企业"]["fee"], 1400.0)
            self.assertEqual(s2.batches["B-A1"]["window"], "W501")
            # 已处理过的复核不会重复执行
            log2: list[str] = []
            due = runner2.run_due("2026-10-16T00:00", make_handlers(log2))
            due_types = [r["type"] for r in due]
            self.assertEqual(due_types.count("cutoff_reminder"), 1)
            self.assertNotIn("settlement_review", due_types)
            # 截关提醒只发一次：再次恢复运行不重复
            runner3 = RecoveryRunner.boot(path)
            third = runner3.run_due("2026-10-17T00:00", make_handlers([]))
            self.assertEqual(third, [])

    def test_meter_gap_task_closes_when_reading_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            runner = RecoveryRunner.boot(path)
            s = runner.service
            build_world(s)
            s.submit_driver("M-COLD", "2026-09", "甲企业", "B-A1", 4.0)
            self.assertIn("meter_gap:M-COLD:2026-09", runner.pending_summary()["meter_gap"])
            # 缺口期读数补录，任务由读数事件直接关闭
            s.submit_reading("R-COLD-1", "M-COLD", "2026-09", 800.0, "rec1")
            self.assertNotIn("meter_gap", runner.pending_summary())


if __name__ == "__main__":
    unittest.main()
