"""批次与计量服务核心模型。

所有状态变化都以事件形式记录：命令只做校验，状态推进统一走 ``_record`` /
``_apply``。进程恢复时按顺序重放事件即可重建全部状态，包括未完成的抄表缺口、
截关提醒和结算复核任务。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from src.hub.errors import (
    AllocationError,
    CutoffViolation,
    DoubleMeasurement,
    IdConflict,
    IncidentScopeError,
    PermissionDenied,
    QuarantineConflict,
    SequenceViolation,
    UnknownReference,
)

# 车辆到场、卸货、质检、上架、交铁路，逐段确认
STAGES = ("arrival", "unload", "qc", "putaway", "rail_handover")
STAGE_LABELS = {
    "arrival": "车辆到场",
    "unload": "卸货",
    "qc": "质检",
    "putaway": "上架",
    "rail_handover": "交铁路",
}

REASSIGN = "reassign"
DAMAGE = "damage"
METER_STOP = "meter_stop"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


class HubService:
    """批次谱系、五段链路、计量版本、分摊结算与恢复任务的内存模型。"""

    def __init__(self) -> None:
        self._seq = 0
        self.events: list[dict[str, Any]] = []
        self._store: Any | None = None
        self._reset()

    def _reset(self) -> None:
        self.actors: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.lineage: list[dict[str, Any]] = []
        self.stages: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
        self.incidents: list[dict[str, Any]] = []
        self.windows: dict[str, dict[str, Any]] = {}
        self.meters: dict[str, dict[str, Any]] = {}
        self.readings: dict[str, dict[str, Any]] = {}
        self.period_index: dict[str, dict[str, dict[str, Any]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        self.quarantined: dict[str, dict[str, Any]] = {}
        self.drivers: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self.allocations: dict[str, dict[str, Any]] = {}
        self.settlements: dict[str, dict[str, Any]] = {}
        self.disputes: dict[str, dict[str, Any]] = {}
        self.factors: dict[str, dict[str, Any]] = {}
        self.active_factor: str | None = None
        self.outbox: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 事件

    def bind_store(self, store: Any) -> None:
        """绑定持久化事件存储，之后每个事件同步追加写入。"""
        self._store = store

    def _record(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        envelope = {"seq": self._seq, "type": event_type, "data": data}
        self._apply(envelope)
        self.events.append(envelope)
        if self._store is not None:
            self._store.append(envelope)
        return envelope

    def restore(self, envelopes: list[dict[str, Any]]) -> None:
        """按事件流重建状态，不重复写入存储。"""
        self._reset()
        for envelope in sorted(envelopes, key=lambda e: e["seq"]):
            self._seq = envelope["seq"]
            self._apply(envelope)
            self.events.append(envelope)

    def _apply(self, event: dict[str, Any]) -> None:
        data = event["data"]
        kind = event["type"]
        if kind == "party_registered":
            self.actors[data["actor_id"]] = data
        elif kind == "order_registered":
            self.orders[data["order_id"]] = data
        elif kind == "batch_registered":
            self.batches[data["batch_id"]] = dict(data, active=True, damaged=False)
        elif kind == "batch_split":
            parent = self.batches[data["parent"]]
            parent["active"] = False
            for child in data["children"]:
                self.batches[child["id"]] = {
                    "batch_id": child["id"],
                    "order_id": parent["order_id"],
                    "company": parent["company"],
                    "qty": child["qty"],
                    "location": parent.get("location"),
                    "zone": parent.get("zone"),
                    "window": parent.get("window"),
                    "active": True,
                    "damaged": False,
                }
                self.stages[child["id"]] = {
                    s: dict(rec) for s, rec in self.stages[data["parent"]].items()
                }
            self.lineage.append(
                {"kind": "split", "parents": [data["parent"]], "children": data["children"]}
            )
        elif kind == "batch_merge":
            parents = [self.batches[p] for p in data["parents"]]
            self.batches[data["child"]] = {
                "batch_id": data["child"],
                "order_id": parents[0]["order_id"],
                "company": parents[0]["company"],
                "qty": data["qty"],
                "location": parents[0].get("location"),
                "zone": parents[0].get("zone"),
                "window": parents[0].get("window"),
                "active": True,
                "damaged": False,
            }
            for pid in data["parents"]:
                self.batches[pid]["active"] = False
            common: dict[str, dict[str, str]] = {}
            for stage in STAGES:
                records = [self.stages[p].get(stage) for p in data["parents"]]
                if all(records) and len({r["at"] for r in records}) == 1:
                    common[stage] = dict(records[0])  # type: ignore[index]
                else:
                    break
            self.stages[data["child"]] = common
            self.lineage.append(
                {"kind": "merge", "parents": data["parents"], "children": [{"id": data["child"], "qty": data["qty"]}]}
            )
        elif kind == "stage_confirmed":
            self.stages[data["batch_id"]][data["stage"]] = {"by": data["by"], "at": data["at"]}
        elif kind == "located":
            batch = self.batches[data["batch_id"]]
            batch["location"] = data["location"]
            batch["zone"] = data["zone"]
        elif kind == "window_registered":
            self.windows[data["window_id"]] = data
        elif kind == "window_assigned":
            self.batches[data["batch_id"]]["window"] = data["window_id"]
        elif kind == "incident_reported":
            self.incidents.append(data)
            if data["kind"] == DAMAGE:
                self.batches[data["batch_id"]]["damaged"] = True
            elif data["kind"] == REASSIGN and data.get("new_window"):
                self.batches[data["batch_id"]]["window"] = data["new_window"]
            elif data["kind"] == METER_STOP:
                self.meters[data["meter_id"]]["stopped"] = True
        elif kind == "meter_registered":
            self.meters[data["meter_id"]] = dict(data, stopped=False)
        elif kind == "reading_received":
            self.readings[data["receipt"]] = dict(data, superseded=False, quarantined=False)
            slot = self.period_index[data["meter_id"]][data["period"]]
            slot["current"] = data["receipt"]
            slot.setdefault("history", []).append(data["receipt"])
        elif kind == "reading_superseded":
            self.readings[data["old_receipt"]]["superseded"] = True
            self.readings[data["new_receipt"]] = dict(
                self.readings[data["old_receipt"]],
                receipt=data["new_receipt"],
                value=data["value"],
                reporter=data["by"],
                at=data["at"],
                superseded=False,
            )
            slot = self.period_index[data["meter_id"]][data["period"]]
            slot["current"] = data["new_receipt"]
            slot.setdefault("history", []).append(data["new_receipt"])
        elif kind == "reading_quarantined":
            self.quarantined[data["receipt"]] = data
        elif kind == "driver_recorded":
            self.drivers[(data["meter_id"], data["period"])].append(
                {"company": data["company"], "batch_id": data["batch_id"], "driver": data["driver"]}
            )
        elif kind == "carbon_factor_published":
            if data.get("supersedes"):
                self.factors[data["supersedes"]]["active"] = False
            self.factors[data["factor_id"]] = {
                "factor_id": data["factor_id"],
                "kg_per_kwh": data["kg_per_kwh"],
                "basis": data["basis"],
                "active": True,
            }
            self.active_factor = data["factor_id"]
        elif kind == "allocation_created":
            self.allocations[data["allocation_id"]] = data
            for share in data["shares"]:
                settlement = {
                    "settlement_id": share["settlement_id"],
                    "company": share["company"],
                    "allocation_id": data["allocation_id"],
                    "meter_id": data["meter_id"],
                    "period": data["period"],
                    "receipt": data["receipt"],
                    "reading_value": data["reading_value"],
                    "factor_id": data["factor_id"],
                    "factor_basis": self.factors[data["factor_id"]]["basis"],
                    "driver": share["driver"],
                    "driver_total": data["driver_total"],
                    "energy_kwh": share["energy_kwh"],
                    "fee": share["fee"],
                    "carbon_kg": share["carbon_kg"],
                    "batches": share["batches"],
                    "status": "open",
                    "relief": 0.0,
                    "reading_reporter": data["reading_reporter"],
                    "flagged": None,
                    "rechecked": False,
                }
                self.settlements[share["settlement_id"]] = settlement
        elif kind == "settlement_flagged":
            self.settlements[data["settlement_id"]]["flagged"] = data["reason"]
        elif kind == "dispute_raised":
            self.disputes[data["dispute_id"]] = data
            self.settlements[data["settlement_id"]]["status"] = "disputed"
        elif kind == "relief_granted":
            dispute = self.disputes[data["dispute_id"]]
            dispute["status"] = "granted"
            settlement = self.settlements[dispute["settlement_id"]]
            settlement["status"] = "relieved"
            settlement["relief"] = data["amount"]
        elif kind == "settlement_rechecked":
            self.settlements[data["settlement_id"]]["rechecked"] = True
        elif kind == "task_scheduled":
            self.outbox[data["key"]] = {
                "key": data["key"],
                "type": data["type"],
                "payload": data["payload"],
                "run_at": data["run_at"],
                "status": "open",
            }
        elif kind == "task_completed":
            if data["key"] in self.outbox:
                self.outbox[data["key"]]["status"] = "done"
                self.outbox[data["key"]]["result"] = data.get("result")
        else:  # pragma: no cover - 防御未知事件
            raise ValueError(f"未知事件类型: {kind}")

    # ------------------------------------------------------------- 参与方/订单

    def register_party(
        self, actor_id: str, role: str, company: str | None = None
    ) -> dict[str, Any]:
        if actor_id in self.actors:
            raise IdConflict("参与方编号已存在")
        if role not in {"operator", "tenant", "meter_recorder", "approver"}:
            raise ValueError("角色不被识别")
        return self._record(
            "party_registered",
            {"actor_id": actor_id, "role": role, "company": company},
        )

    def register_order(self, order_id: str, company: str, by: str) -> dict[str, Any]:
        self._actor(by)
        if order_id in self.orders:
            raise IdConflict("订单编号已存在")
        return self._record(
            "order_registered", {"order_id": order_id, "company": company, "by": by}
        )

    def register_batch(
        self,
        batch_id: str,
        order_id: str,
        qty: float,
        zone: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        if batch_id in self.batches:
            raise IdConflict("批次编号已存在")
        order = self._order(order_id)
        if qty <= 0:
            raise ValueError("批次数量必须为正")
        return self._record(
            "batch_registered",
            {
                "batch_id": batch_id,
                "order_id": order_id,
                "company": order["company"],
                "qty": qty,
                "zone": zone,
                "location": location,
            },
        )

    # ------------------------------------------------------------- 批次谱系

    def split_batch(self, parent_id: str, children: list[tuple[str, float]]) -> dict[str, Any]:
        parent = self._batch(parent_id)
        if not parent["active"]:
            raise IncidentScopeError("只能拆分活动批次")
        total = sum(qty for _, qty in children)
        if abs(total - parent["qty"]) > 1e-9:
            raise ValueError("拆分子批次数量之和必须等于原批次")
        for child_id, qty in children:
            if child_id in self.batches:
                raise IdConflict(f"子批次编号已存在: {child_id}")
            if qty <= 0:
                raise ValueError("子批次数量必须为正")
        return self._record(
            "batch_split",
            {"parent": parent_id, "children": [{"id": c, "qty": q} for c, q in children]},
        )

    def merge_batches(self, parent_ids: list[str], child_id: str) -> dict[str, Any]:
        if child_id in self.batches:
            raise IdConflict("合并后批次编号已存在")
        parents = [self._batch(p) for p in parent_ids]
        if any(not p["active"] for p in parents):
            raise IncidentScopeError("只能合并活动批次")
        companies = {p["company"] for p in parents}
        orders = {p["order_id"] for p in parents}
        if len(companies) != 1 or len(orders) != 1:
            raise ValueError("只能合并同一企业同一订单下的批次")
        return self._record(
            "batch_merge",
            {
                "parents": parent_ids,
                "child": child_id,
                "qty": sum(p["qty"] for p in parents),
            },
        )

    def place_batch(self, batch_id: str, location: str, zone: str | None = None) -> dict[str, Any]:
        self._batch(batch_id)
        return self._record(
            "located", {"batch_id": batch_id, "location": location, "zone": zone}
        )

    # ------------------------------------------------------------- 五段链路

    def confirm_stage(
        self, batch_id: str, stage: str, by: str, at: str | None = None
    ) -> dict[str, Any]:
        actor = self._actor(by)
        batch = self._batch(batch_id)
        if actor["role"] == "tenant" and actor.get("company") != batch["company"]:
            raise PermissionDenied("只能确认本方货物的链路")
        if stage not in STAGES:
            raise SequenceViolation("未知链路节点")
        already = self.stages[batch_id]
        if stage in already:
            # 相同确认幂等返回，不产生重复事件
            return next(e for e in reversed(self.events)
                        if e["type"] == "stage_confirmed"
                        and e["data"]["batch_id"] == batch_id
                        and e["data"]["stage"] == stage)
        index = STAGES.index(stage)
        if index > 0 and STAGES[index - 1] not in already:
            raise SequenceViolation(
                f"必须先完成{STAGE_LABELS[STAGES[index - 1]]}才能确认{STAGE_LABELS[stage]}"
            )
        if batch.get("damaged"):
            raise IncidentScopeError("破损批次的未完成链路已中止，不能继续确认")
        timestamp = at or _now()
        if stage == "rail_handover":
            window_id = batch.get("window")
            if not window_id:
                raise CutoffViolation("交铁路前必须绑定班列窗口")
            cutoff = self.windows[window_id]["cutoff"]
            if timestamp > cutoff:
                raise CutoffViolation(
                    f"交铁路时间 {timestamp} 晚于班列截关 {cutoff}"
                )
        return self._record(
            "stage_confirmed",
            {"batch_id": batch_id, "stage": stage, "by": by, "at": timestamp},
        )

    # ------------------------------------------------------------- 班列窗口

    def register_window(self, window_id: str, train: str, cutoff: str) -> dict[str, Any]:
        if window_id in self.windows:
            raise IdConflict("班列窗口编号已存在")
        return self._record(
            "window_registered",
            {"window_id": window_id, "train": train, "cutoff": cutoff},
        )

    def assign_window(self, batch_id: str, window_id: str) -> dict[str, Any]:
        batch = self._batch(batch_id)
        window = self._window(window_id)
        if "rail_handover" in self.stages[batch_id]:
            raise IncidentScopeError("已交铁路的批次不能改配班列窗口")
        event = self._record("window_assigned", {"batch_id": batch_id, "window_id": window_id})
        self._schedule(
            f"cutoff:{batch_id}:{window_id}",
            "cutoff_reminder",
            {"batch_id": batch_id, "window_id": window_id, "train": window["train"]},
            window["cutoff"],
        )
        return event

    # ------------------------------------------------------------- 异常事件

    def report_incident(
        self,
        kind: str,
        by: str,
        batch_id: str | None = None,
        meter_id: str | None = None,
        period: str | None = None,
        new_window: str | None = None,
        at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """改配、破损、设备停机登记。

        只允许作用于尚未走完的链路：批次五段全部完成后不能再登记改配/破损；
        仪表所在周期已经出分摊后，停机不能追溯改动已完成的结算链路。
        """
        self._actor(by)
        if kind not in (REASSIGN, DAMAGE, METER_STOP):
            raise ValueError("异常类型不被识别")
        unfinished: list[str] = []
        if kind in (REASSIGN, DAMAGE):
            if not batch_id:
                raise ValueError("该异常必须指明批次")
            batch = self._batch(batch_id)
            unfinished = [s for s in STAGES if s not in self.stages[batch_id]]
            if not unfinished:
                raise IncidentScopeError(
                    f"{STAGE_LABELS['rail_handover']}已完成，异常不能回溯影响已完成链路"
                )
            if kind == REASSIGN and new_window is not None:
                self._window(new_window)
        else:
            if not meter_id or not period:
                raise ValueError("设备停机必须指明仪表与周期")
            self._meter(meter_id)
            for allocation in self.allocations.values():
                if allocation["meter_id"] == meter_id and allocation["period"] == period:
                    raise IncidentScopeError("该周期已完成分摊结算，停机不能追溯改动")

        old_window: str | None = None
        if kind == REASSIGN and batch_id and batch_id in self.batches:
            old_window = self.batches[batch_id].get("window")
        event = self._record(
            "incident_reported",
            {
                "kind": kind,
                "by": by,
                "batch_id": batch_id,
                "meter_id": meter_id,
                "period": period,
                "new_window": new_window,
                "at": at or _now(),
                "note": note,
                "affected_unfinished": unfinished,
            },
        )
        if kind == REASSIGN and new_window and batch_id:
            if old_window:
                # 旧窗口的截关提醒随改配作废，只提醒新窗口
                self._complete_task(
                    f"cutoff:{batch_id}:{old_window}", "已改配，旧窗口提醒作废"
                )
            self._schedule(
                f"cutoff:{batch_id}:{new_window}",
                "cutoff_reminder",
                {"batch_id": batch_id, "window_id": new_window,
                 "train": self.windows[new_window]["train"]},
                self.windows[new_window]["cutoff"],
            )
        return event

    # ------------------------------------------------------------- 仪表与读数

    def register_meter(
        self, meter_id: str, kind: str, zone: str | None = None, shared: bool = True
    ) -> dict[str, Any]:
        if meter_id in self.meters:
            raise IdConflict("计量点编号已存在")
        if kind not in ("cold", "common"):
            raise ValueError("仪表类型必须是 cold 或 common")
        return self._record(
            "meter_registered",
            {"meter_id": meter_id, "kind": kind, "zone": zone, "shared": shared},
        )

    def submit_reading(
        self,
        receipt: str,
        meter_id: str,
        period: str,
        value: float,
        by: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """登记一版读数。

        - 回执编号、仪表、周期、数值完全相同：幂等，不产生新事件；
        - 回执编号相同但数值（或周期/仪表）不同：读数隔离，不进入计量；
        - 同仪表同周期已有有效读数而换了新回执：判定重复计量，拒绝。
        """
        actor = self._actor(by)
        if actor["role"] != "meter_recorder":
            raise PermissionDenied("只有计量班组可以录入读数")
        self._meter(meter_id)
        if self.meters[meter_id].get("stopped"):
            raise IncidentScopeError("仪表已停机，不能继续录入读数")
        if value < 0:
            raise ValueError("读数值不能为负")

        existing = self.readings.get(receipt)
        blocked = self.quarantined.get(receipt)
        if blocked is not None:
            if (blocked["meter_id"] == meter_id and blocked["period"] == period
                    and blocked["value"] == value):
                raise QuarantineConflict("相同隔离回执重复提交，读数保持隔离")
            raise QuarantineConflict("隔离回执编号再次出现数值冲突，继续隔离")
        if existing is not None:
            if (existing["meter_id"] == meter_id and existing["period"] == period
                    and existing["value"] == value):
                return next(e for e in reversed(self.events)
                            if e["type"] == "reading_received"
                            and e["data"]["receipt"] == receipt)
            self._record(
                "reading_quarantined",
                {
                    "receipt": receipt,
                    "meter_id": meter_id,
                    "period": period,
                    "value": value,
                    "existing_value": existing["value"],
                    "at": at or _now(),
                },
            )
            raise QuarantineConflict(
                f"回执 {receipt} 编号相同但数值不同，新读数已隔离待人工裁决"
            )

        slot = self.period_index[meter_id][period]
        current = slot.get("current")
        if current is not None:
            raise DoubleMeasurement(
                f"{meter_id} 在 {period} 已有有效读数 {current}，疑似同一批货被两次计量"
            )
        event = self._record(
            "reading_received",
            {
                "receipt": receipt,
                "meter_id": meter_id,
                "period": period,
                "value": value,
                "reporter": by,
                "at": at or _now(),
            },
        )
        self._complete_task(f"meter_gap:{meter_id}:{period}", "读数已补录")
        return event

    def correct_reading(
        self,
        new_receipt: str,
        meter_id: str,
        period: str,
        value: float,
        by: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """登记读数新版本，旧版本保留并标记，已生成的结算挂入复核。"""
        actor = self._actor(by)
        if actor["role"] != "meter_recorder":
            raise PermissionDenied("只有计量班组可以更正读数")
        self._meter(meter_id)
        if new_receipt in self.readings or new_receipt in self.quarantined:
            raise IdConflict("新回执编号已被使用")
        slot = self.period_index[meter_id][period]
        old_receipt = slot.get("current")
        if not old_receipt:
            raise UnknownReference("该周期尚无读数，应直接录入而非更正")
        if value < 0:
            raise ValueError("读数值不能为负")
        event = self._record(
            "reading_superseded",
            {
                "meter_id": meter_id,
                "period": period,
                "old_receipt": old_receipt,
                "new_receipt": new_receipt,
                "value": value,
                "by": by,
                "at": at or _now(),
            },
        )
        for settlement in self.settlements.values():
            if (settlement["meter_id"] == meter_id and settlement["period"] == period
                    and settlement["status"] != "relieved"):
                self._record(
                    "settlement_flagged",
                    {"settlement_id": settlement["settlement_id"],
                     "reason": f"读数 {old_receipt} 已被 {new_receipt} 更正"},
                )
                self._schedule(
                    f"review:{settlement['settlement_id']}",
                    "settlement_review",
                    {"settlement_id": settlement["settlement_id"],
                     "reason": f"读数 {old_receipt} 已被 {new_receipt} 更正"},
                    at or _now(),
                )
        return event

    # ------------------------------------------------------------- 碳排与分摊

    def publish_carbon_factor(
        self, factor_id: str, kg_per_kwh: float, basis: str
    ) -> dict[str, Any]:
        if factor_id in self.factors:
            raise IdConflict("碳排因子版本编号已存在")
        if kg_per_kwh <= 0 or not basis.strip():
            raise ValueError("碳排因子与换算依据不能为空")
        return self._record(
            "carbon_factor_published",
            {
                "factor_id": factor_id,
                "kg_per_kwh": kg_per_kwh,
                "basis": basis,
                "supersedes": self.active_factor,
            },
        )

    def submit_driver(
        self,
        meter_id: str,
        period: str,
        company: str,
        batch_id: str,
        driver: float,
        due: str | None = None,
    ) -> dict[str, Any]:
        """登记某企业某批次在该计量点该周期的分摊驱动量。"""
        self._meter(meter_id)
        batch = self._batch(batch_id)
        if batch["company"] != company:
            raise PermissionDenied("驱动量只能登记给批次所属企业")
        if driver < 0:
            raise ValueError("驱动量不能为负")
        event = self._record(
            "driver_recorded",
            {
                "meter_id": meter_id,
                "period": period,
                "company": company,
                "batch_id": batch_id,
                "driver": driver,
            },
        )
        gap_key = f"meter_gap:{meter_id}:{period}"
        if self.period_index[meter_id][period].get("current"):
            # 读数已在，缺口任务若有则直接关闭
            self._complete_task(gap_key, "读数已补录")
        elif not any(
            t["type"] == "meter_gap" and t["status"] == "open"
            and t["payload"].get("meter_id") == meter_id
            and t["payload"].get("period") == period
            for t in self.outbox.values()
        ):
            self._schedule(
                gap_key,
                "meter_gap",
                {"meter_id": meter_id, "period": period},
                due or f"{period}-28T23:00",
            )
        return event

    def create_allocation(
        self,
        allocation_id: str,
        meter_id: str,
        period: str,
        fee_total: float,
    ) -> dict[str, Any]:
        """按驱动量把周期读数与总费用分摊到各企业，并固化计量与碳排依据。"""
        if allocation_id in self.allocations:
            raise IdConflict("分摊单编号已存在")
        self._meter(meter_id)
        slot = self.period_index[meter_id][period]
        receipt = slot.get("current")
        if not receipt:
            raise AllocationError("该计量点周期尚无有效读数，不能分摊")
        rows = [r for r in self.drivers[(meter_id, period)] if r["driver"] > 0]
        if not rows:
            raise AllocationError("缺少分摊驱动量，跨租户电费无法解释")
        if not self.active_factor:
            raise AllocationError("尚未发布碳排换算因子")
        if fee_total < 0:
            raise ValueError("费用总额不能为负")

        reading = self.readings[receipt]
        factor = self.factors[self.active_factor]
        driver_total = sum(r["driver"] for r in rows)
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = grouped.setdefault(
                row["company"], {"driver": 0.0, "batches": []}
            )
            bucket["driver"] += row["driver"]
            bucket["batches"].append(row["batch_id"])

        shares = []
        for company, bucket in sorted(grouped.items()):
            ratio = bucket["driver"] / driver_total
            shares.append(
                {
                    "settlement_id": f"{allocation_id}:{company}",
                    "company": company,
                    "driver": bucket["driver"],
                    "batches": sorted(bucket["batches"]),
                    "energy_kwh": round(reading["value"] * ratio, 6),
                    "fee": round(fee_total * ratio, 2),
                    "carbon_kg": round(
                        reading["value"] * ratio * factor["kg_per_kwh"], 6
                    ),
                }
            )
        return self._record(
            "allocation_created",
            {
                "allocation_id": allocation_id,
                "meter_id": meter_id,
                "period": period,
                "receipt": receipt,
                "reading_value": reading["value"],
                "reading_reporter": reading["reporter"],
                "factor_id": factor["factor_id"],
                "driver_total": driver_total,
                "fee_total": fee_total,
                "shares": shares,
            },
        )

    # ------------------------------------------------------------- 争议与减免

    def raise_dispute(
        self, settlement_id: str, dispute_id: str, by: str, reason: str
    ) -> dict[str, Any]:
        actor = self._actor(by)
        settlement = self._settlement(settlement_id)
        if actor["role"] != "operator" and actor.get("company") != settlement["company"]:
            raise PermissionDenied("只能对本方费用提出争议")
        if dispute_id in self.disputes:
            raise IdConflict("争议单编号已存在")
        return self._record(
            "dispute_raised",
            {
                "dispute_id": dispute_id,
                "settlement_id": settlement_id,
                "by": by,
                "reason": reason,
                "status": "open",
            },
        )

    def grant_relief(
        self, dispute_id: str, approver: str, amount: float
    ) -> dict[str, Any]:
        """批准争议减免。审批人必须是审核角色，且不能是该笔读数的录入人。"""
        actor = self._actor(approver)
        if actor["role"] != "approver":
            raise PermissionDenied("只有费用与碳核算审核人员可以批准减免")
        dispute = self.disputes.get(dispute_id)
        if not dispute:
            raise UnknownReference("争议单不存在")
        if dispute["status"] != "open":
            raise IncidentScopeError("争议已处理，不能重复批准")
        settlement = self._settlement(dispute["settlement_id"])
        if approver == settlement["reading_reporter"]:
            raise PermissionDenied("录入计量的人不能批准同一笔费用的争议减免")
        if amount <= 0 or amount > settlement["fee"]:
            raise ValueError("减免额必须为正且不超过费用金额")
        return self._record(
            "relief_granted",
            {
                "dispute_id": dispute_id,
                "settlement_id": settlement["settlement_id"],
                "approver": approver,
                "amount": amount,
            },
        )

    def recheck_settlement(self, settlement_id: str, note: str = "复核完成") -> dict[str, Any]:
        self._settlement(settlement_id)
        event = self._record(
            "settlement_rechecked", {"settlement_id": settlement_id, "note": note}
        )
        self._complete_task(f"review:{settlement_id}", note)
        return event

    # ------------------------------------------------------------- 可见性与追溯

    def visible_batches(self, actor_id: str) -> list[dict[str, Any]]:
        actor = self._actor(actor_id)
        if actor["role"] == "operator":
            return list(self.batches.values())
        company = actor.get("company")
        return [b for b in self.batches.values() if b["company"] == company]

    def visible_settlements(self, actor_id: str) -> list[dict[str, Any]]:
        actor = self._actor(actor_id)
        if actor["role"] == "operator":
            return list(self.settlements.values())
        company = actor.get("company")
        return [s for s in self.settlements.values() if s["company"] == company]

    def trace_settlement(self, settlement_id: str, viewer: str) -> dict[str, Any]:
        """从一笔能耗分摊还原：货物去向、仪表读数版本、碳排依据与审批依据。"""
        actor = self._actor(viewer)
        settlement = self._settlement(settlement_id)
        if actor["role"] != "operator" and actor.get("company") != settlement["company"]:
            raise PermissionDenied("企业只能查看本方费用分摊")

        meter_id = settlement["meter_id"]
        period = settlement["period"]
        slot = self.period_index[meter_id][period]
        versions = [
            {
                "receipt": rid,
                "value": self.readings[rid]["value"],
                "reporter": self.readings[rid]["reporter"],
                "at": self.readings[rid]["at"],
                "current": rid == slot.get("current"),
                "superseded": self.readings[rid]["superseded"],
            }
            for rid in slot.get("history", [])
        ]
        factor = self.factors[settlement["factor_id"]]
        goods = [self._trace_batch(bid) for bid in settlement["batches"]]
        dispute = next(
            (d for d in self.disputes.values() if d["settlement_id"] == settlement_id),
            None,
        )
        approval = None
        if dispute and dispute["status"] == "granted":
            relief = next(
                e["data"] for e in self.events
                if e["type"] == "relief_granted" and e["data"]["dispute_id"] == dispute["dispute_id"]
            )
            approval = {
                "dispute_id": dispute["dispute_id"],
                "reason": dispute["reason"],
                "approver": relief["approver"],
                "amount": relief["amount"],
                "recorder_segregated": relief["approver"] != settlement["reading_reporter"],
            }
        return {
            "settlement_id": settlement_id,
            "company": settlement["company"],
            "viewer_company": actor.get("company"),
            "period": period,
            "energy_kwh": settlement["energy_kwh"],
            "fee": settlement["fee"],
            "relief": settlement["relief"],
            "carbon_kg": settlement["carbon_kg"],
            "status": settlement["status"],
            "flagged": settlement["flagged"],
            "meter": {
                "meter_id": meter_id,
                "kind": self.meters[meter_id]["kind"],
                "zone": self.meters[meter_id]["zone"],
                "pinned_receipt": settlement["receipt"],
                "versions": versions,
            },
            "carbon_basis": {
                "factor_id": factor["factor_id"],
                "kg_per_kwh": factor["kg_per_kwh"],
                "basis": factor["basis"],
                "factor_still_active": factor["active"],
            },
            "allocation_basis": {
                "allocation_id": settlement["allocation_id"],
                "driver_share": round(settlement["driver"] / settlement["driver_total"], 6),
            },
            "goods": goods,
            "approval": approval,
        }

    def _trace_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self._batch(batch_id)
        window_id = batch.get("window")
        confirmed = {s: rec for s, rec in self.stages[batch_id].items()}
        parents, children = [], []
        for link in self.lineage:
            if link["kind"] == "split" and link["parents"] == [batch_id]:
                children = [c["id"] for c in link["children"]]
            if batch_id in [c["id"] for c in link["children"]]:
                parents = list(link["parents"])

        location = batch.get("location")
        zone = batch.get("zone")
        # 已拆分的父批次：从仍在活动的叶子子孙汇聚实际去向
        leaf_ids = self._leaf_descendants(batch_id)
        if leaf_ids != [batch_id]:
            stages_union: dict[str, dict[str, str]] = {}
            for leaf in leaf_ids:
                for stage, rec in self.stages[leaf].items():
                    stages_union.setdefault(stage, rec)
            confirmed = stages_union
            locations = {self.batches[leaf].get("location") for leaf in leaf_ids}
            zones = {self.batches[leaf].get("zone") for leaf in leaf_ids}
            windows = {self.batches[leaf].get("window") for leaf in leaf_ids}
            if len(locations) == 1:
                location = next(iter(locations))
            if len(zones) == 1:
                zone = next(iter(zones))
            if len(windows) == 1:
                window_id = next(iter(windows))

        return {
            "batch_id": batch_id,
            "order_id": batch["order_id"],
            "qty": batch["qty"],
            "location": location,
            "zone": zone,
            "active": batch["active"],
            "leaf_batches": leaf_ids,
            "stages": {STAGE_LABELS[s]: confirmed[s] for s in STAGES if s in confirmed},
            "next_unfinished": [
                STAGE_LABELS[s] for s in STAGES if s not in confirmed
            ],
            "window": (
                {"window_id": window_id, **{k: self.windows[window_id][k] for k in ("train", "cutoff")}}
                if window_id else None
            ),
            "lineage": {"parents": parents, "children": children},
        }

    def _leaf_descendants(self, batch_id: str) -> list[str]:
        """返回批次当前仍活动的叶子后代；自身仍活动时返回自身。"""
        for link in self.lineage:
            if batch_id in link["parents"]:
                result: list[str] = []
                for child in [c["id"] for c in link["children"]]:
                    result.extend(self._leaf_descendants(child))
                return result
        return [batch_id]

    # ------------------------------------------------------------- 恢复任务

    def _schedule(self, key: str, task_type: str, payload: dict[str, Any], run_at: str) -> None:
        existing = self.outbox.get(key)
        if existing and existing["status"] == "done":
            return
        self._record(
            "task_scheduled",
            {"key": key, "type": task_type, "payload": payload, "run_at": run_at},
        )

    def _complete_task(self, key: str, result: str) -> None:
        task = self.outbox.get(key)
        if task and task["status"] == "open":
            self._record("task_completed", {"key": key, "result": result})

    def open_tasks(self) -> list[dict[str, Any]]:
        return [t for t in self.outbox.values() if t["status"] == "open"]

    # ------------------------------------------------------------- 查找辅助

    def _actor(self, actor_id: str) -> dict[str, Any]:
        actor = self.actors.get(actor_id)
        if not actor:
            raise UnknownReference(f"参与方不存在: {actor_id}")
        return actor

    def _order(self, order_id: str) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if not order:
            raise UnknownReference(f"订单不存在: {order_id}")
        return order

    def _batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.batches.get(batch_id)
        if not batch:
            raise UnknownReference(f"批次不存在: {batch_id}")
        return batch

    def _window(self, window_id: str) -> dict[str, Any]:
        window = self.windows.get(window_id)
        if not window:
            raise UnknownReference(f"班列窗口不存在: {window_id}")
        return window

    def _meter(self, meter_id: str) -> dict[str, Any]:
        meter = self.meters.get(meter_id)
        if not meter:
            raise UnknownReference(f"计量点不存在: {meter_id}")
        return meter

    def _settlement(self, settlement_id: str) -> dict[str, Any]:
        settlement = self.settlements.get(settlement_id)
        if not settlement:
            raise UnknownReference(f"结算单不存在: {settlement_id}")
        return settlement
