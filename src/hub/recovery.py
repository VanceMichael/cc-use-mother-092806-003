"""事件持久化与系统恢复。

事件以 JSONL 追加写入，恢复时重放重建内存状态。恢复后，恢复运行器继续处理
未完成的抄表缺口、截关提醒和结算复核：每个待办任务只推进一次，处理结果再落成
``task_completed`` 事件，因此进程反复重启也不会重复提醒、重复复核。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.hub.service import HubService


class EventStore:
    """只追加的 JSONL 事件存储。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, envelope: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            handle.flush()

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        envelopes = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                envelopes.append(json.loads(line))
        return envelopes


# 任务类型 -> 接收 (service, task, now) 的处理函数，返回提示文本；抛异常表示本次无法完成
TaskHandler = Callable[[HubService, dict[str, Any], str], str]


class RecoveryRunner:
    """绑定服务与存储，重放事件并继续未完成任务。"""

    def __init__(self, service: HubService, store: EventStore) -> None:
        self.service = service
        self.store = store
        self.service.bind_store(store)

    @classmethod
    def boot(cls, path: str | Path) -> "RecoveryRunner":
        """新建服务，重放磁盘上的全部事件（模拟进程重启后的恢复）。"""
        service = HubService()
        runner = cls(service, EventStore(path))
        service.restore(runner.store.load())
        return runner

    def run_due(self, now: str, handlers: dict[str, TaskHandler]) -> list[dict[str, Any]]:
        """推进所有到期（run_at <= now）且仍打开的任务，返回处理结果列表。"""
        results: list[dict[str, Any]] = []
        for task in list(self.service.open_tasks()):
            if task["run_at"] > now:
                continue
            handler = handlers.get(task["type"])
            if handler is None:
                continue
            result = handler(self.service, task, now)
            self.service._complete_task(task["key"], result)
            results.append({"key": task["key"], "type": task["type"], "result": result})
        return results

    def pending_summary(self) -> dict[str, list[str]]:
        """按类型汇总仍未完成的恢复任务键。"""
        summary: dict[str, list[str]] = {}
        for task in self.service.open_tasks():
            summary.setdefault(task["type"], []).append(task["key"])
        return summary
