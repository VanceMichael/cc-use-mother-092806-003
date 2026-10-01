"""内存仓储与快照恢复。

服务只通过仓储访问实体，仓储整体可序列化为 JSON 快照；
系统中断后用快照恢复，未完成的抄表缺口、截关提醒、结算复核
由 ``HubService.recover`` 依据持久化状态重新生成。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from . import model
from .codec import snapshot_from_json, snapshot_to_json

T = TypeVar("T")

_STORE_TYPES: tuple[type, ...] = (
    model.Company,
    model.User,
    model.Order,
    model.Batch,
    model.BatchException,
    model.Location,
    model.RailWindow,
    model.Meter,
    model.Reading,
    model.MeterGap,
    model.CarbonFactor,
    model.Allocation,
    model.Dispute,
    model.Alert,
)


class Repository:
    def __init__(self) -> None:
        self._stores: dict[str, dict[str, Any]] = {
            cls.__name__: {} for cls in _STORE_TYPES
        }
        self._sequences: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 基本存取
    # ------------------------------------------------------------------

    def add(self, entity: Any) -> Any:
        store = self._stores[type(entity).__name__]
        if entity.id in store:
            raise ValueError(f"标识重复: {type(entity).__name__}:{entity.id}")
        store[entity.id] = entity
        return entity

    def get(self, cls: type[T], entity_id: str) -> T:
        store = self._stores[cls.__name__]
        if entity_id not in store:
            raise KeyError(f"{cls.__name__}不存在: {entity_id}")
        return store[entity_id]

    def find(self, cls: type[T], entity_id: str) -> T | None:
        return self._stores[cls.__name__].get(entity_id)

    def list(self, cls: type[T]) -> list[T]:
        return list(self._stores[cls.__name__].values())

    def delete(self, cls: type, entity_id: str) -> None:
        self._stores[cls.__name__].pop(entity_id, None)

    def next_id(self, prefix: str) -> str:
        """生成仓储内唯一、可预测的业务编号。"""
        count = self._sequences.get(prefix, 0) + 1
        self._sequences[prefix] = count
        return f"{prefix}-{count:04d}"

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        collections = {
            name: list(store.values()) for name, store in self._stores.items()
        }
        return {
            "domain": model.DOMAIN if hasattr(model, "DOMAIN") else "hub",
            "sequences": dict(self._sequences),
            "stores": snapshot_to_json(collections),
        }

    def save_snapshot(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def restore(self, data: dict[str, Any]) -> None:
        restored = snapshot_from_json(data["stores"])
        for name, store in self._stores.items():
            store.clear()
            for entity in restored.get(name, []):
                store[entity.id] = entity
        self._sequences = dict(data.get("sequences", {}))

    def load_snapshot(self, path: str | Path) -> None:
        self.restore(json.loads(Path(path).read_text(encoding="utf-8")))
