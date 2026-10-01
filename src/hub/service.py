"""枢纽批次与计量服务。

覆盖五条业务主线：

1. 箱货批次谱系：订单收货、拆分/合并、库位温区、逐段确认；
   改配/破损/温区降级只阻断受影响数量的未完成链路。
2. 班列窗口：交铁路受截关时间约束，恢复后继续截关提醒。
3. 设备计量：读数回执幂等，同编号不同数值先隔离；读数版本化，
   缺口未补齐不得分摊，设备停机只登记缺口、不影响已确认结算。
4. 跨租户分摊与碳排：私表归表主，公共表按温区占用箱量分摊，
   碳排换算因子版本留痕；争议录入与审批职责分离。
5. 恢复与溯源：recover 重建抄表缺口、截关、结算复核告警；
   任一分摊可还原货物去向、仪表版本与审批依据。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from . import model
from .model import (
    Alert,
    Allocation,
    AllocationLine,
    AllocationStatus,
    AuthError,
    Batch,
    BatchException,
    BatchStatus,
    CarbonFactor,
    ConflictError,
    CutoffError,
    Dispute,
    DisputeStatus,
    DomainError,
    Location,
    Meter,
    MeterGap,
    MeterKind,
    Order,
    RailWindow,
    Reading,
    ReadingStatus,
    ReviewStatus,
    Role,
    Stage,
)
from .repository import Repository

_CUTOFF_LOOKAHEAD = timedelta(hours=24)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


class HubService:
    def __init__(self, repo: Repository | None = None) -> None:
        self.repo = repo or Repository()

    # ==================================================================
    # 主体与基础资料
    # ==================================================================

    def register_company(
        self, company_id: str, name: str, role: Role,
        visible_order_ids: set[str] | None = None,
    ) -> model.Company:
        company = model.Company(
            id=company_id, name=name, role=role,
            visible_order_ids=set(visible_order_ids) if visible_order_ids is not None else None,
        )
        return self.repo.add(company)

    def register_user(self, user_id: str, name: str, company_id: str, role: Role) -> model.User:
        self.repo.get(model.Company, company_id)
        return self.repo.add(model.User(id=user_id, name=name, company_id=company_id, role=role))

    def register_order(self, order_id: str, owner_company_id: str, destination: str,
                       created_at: str = "", **metadata: Any) -> Order:
        self.repo.get(model.Company, owner_company_id)
        return self.repo.add(Order(
            id=order_id, owner_company_id=owner_company_id, destination=destination,
            created_at=created_at or _now(), metadata=dict(metadata),
        ))

    def register_location(self, location_id: str, zone: str, temp_zone: str,
                          meter_id: str | None = None) -> Location:
        if meter_id is not None:
            self.repo.get(Meter, meter_id)
        return self.repo.add(Location(id=location_id, zone=zone, temp_zone=temp_zone,
                                      meter_id=meter_id))

    def register_meter(self, meter_id: str, kind: MeterKind, name: str,
                       owner_company_id: str | None = None, zone: str | None = None,
                       unit: str = "kWh", serves_temp_zones: list[str] | None = None) -> Meter:
        if owner_company_id is not None:
            self.repo.get(model.Company, owner_company_id)
        meter = Meter(id=meter_id, kind=kind, name=name, owner_company_id=owner_company_id,
                      zone=zone, unit=unit,
                      serves_temp_zones=list(serves_temp_zones or []))
        return self.repo.add(meter)

    def register_rail_window(self, window_id: str, train_code: str, destination: str,
                             cutoff_at: str, depart_at: str) -> RailWindow:
        return self.repo.add(RailWindow(id=window_id, train_code=train_code,
                                        destination=destination, cutoff_at=cutoff_at,
                                        depart_at=depart_at))

    def add_carbon_factor(self, factor_id: str, source: str, kg_per_unit: float,
                          valid_from: str, version: int) -> CarbonFactor:
        if kg_per_unit < 0:
            raise DomainError("碳排因子不能为负")
        return self.repo.add(CarbonFactor(id=factor_id, source=source,
                                          kg_per_unit=kg_per_unit, valid_from=valid_from,
                                          version=version))

    # ==================================================================
    # 权限
    # ==================================================================

    def _user(self, user_id: str) -> model.User:
        return self.repo.get(model.User, user_id)

    def _require_role(self, user: model.User, *roles: Role) -> None:
        if user.role not in roles:
            raise AuthError(f"角色 {user.role.value} 无权执行该操作")

    def _company(self, user: model.User) -> model.Company:
        return self.repo.get(model.Company, user.company_id)

    def _can_see_order(self, user: model.User, order_id: str) -> bool:
        if user.role in (Role.OPERATOR, Role.AUDITOR, Role.REVIEWER):
            return True
        order = self.repo.find(Order, order_id)
        if order is None:
            return False
        company = self._company(user)
        if order.owner_company_id == company.id:
            return True
        return company.visible_order_ids is not None and order_id in company.visible_order_ids

    def _assert_order_visible(self, user: model.User, order_id: str) -> None:
        if not self._can_see_order(user, order_id):
            raise AuthError("只能查看本方货物")

    # ==================================================================
    # 批次谱系：收货、拆分、合并、异常
    # ==================================================================

    def receive_batch(self, user: model.User | str, order_id: str, sku: str, quantity: int,
                      temp_zone: str, at: str, location_id: str | None = None,
                      batch_id: str | None = None) -> Batch:
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR)
        self._assert_order_visible(user, order_id)
        if quantity <= 0:
            raise DomainError("批次数量必须为正")
        if location_id is not None:
            location = self.repo.get(Location, location_id)
            if location.temp_zone != temp_zone:
                raise DomainError("库位温区与批次温区不一致")
        batch = Batch(
            id=batch_id or self.repo.next_id("B"),
            order_id=order_id, sku=sku, quantity=quantity, temp_zone=temp_zone,
            location_id=location_id, created_at=at,
            confirmations={Stage.ARRIVAL.value: (at, user.id)},
        )
        return self.repo.add(batch)

    def _get_active_batch(self, batch_id: str) -> Batch:
        batch = self.repo.get(Batch, batch_id)
        if batch.status == BatchStatus.CLOSED:
            raise DomainError(f"批次 {batch_id} 已终止")
        return batch

    def _spawn_child(self, parent: Batch, quantity: int, at: str,
                     order_id: str | None = None, temp_zone: str | None = None,
                     location_id: str | None = None,
                     copy_through: Stage | None = None) -> Batch:
        """从父批次切出子批次，形成谱系。

        copy_through 给出异常发生节点：子批次只继承该节点之前已完成的
        确认，异常数量不计入该节点及以后——即异常只影响未完成链路。
        """
        confirmations: dict[str, tuple[str, str]] = {}
        if copy_through is not None:
            cutoff_index = Stage.sequence().index(copy_through)
            for stage in Stage.sequence()[:cutoff_index]:
                if stage.value in parent.confirmations:
                    confirmations[stage.value] = parent.confirmations[stage.value]
        else:
            confirmations = dict(parent.confirmations)
        child = Batch(
            id=self.repo.next_id("B"),
            order_id=order_id or parent.order_id,
            sku=parent.sku,
            quantity=quantity,
            temp_zone=temp_zone or parent.temp_zone,
            location_id=location_id if location_id is not None else parent.location_id,
            parent_ids=[parent.id],
            created_at=at,
            confirmations=confirmations,
            rail_window_id=parent.rail_window_id if order_id is None else None,
        )
        return self.repo.add(child)

    def _retire_if_empty(self, batch: Batch) -> None:
        if batch.quantity == 0 and batch.status == BatchStatus.ACTIVE:
            batch.status = BatchStatus.CLOSED

    def split_batch(self, user: model.User | str, batch_id: str, quantities: list[int],
                    at: str) -> list[Batch]:
        """普通箱货拆分：子批次继承父批次全部已完成节点。"""
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR)
        parent = self._get_active_batch(batch_id)
        if not quantities or any(q <= 0 for q in quantities):
            raise DomainError("拆分数量必须为正")
        if sum(quantities) > parent.quantity:
            raise DomainError("拆分数量超过批次现存数量")
        children = [self._spawn_child(parent, q, at) for q in quantities]
        parent.quantity -= sum(quantities)
        self._retire_if_empty(parent)
        return children

    def merge_batches(self, user: model.User | str, batch_ids: list[str], at: str) -> Batch:
        """合箱：新批次承接各父批次，只继承共同完成的节点。"""
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR)
        if len(batch_ids) < 2:
            raise DomainError("合并至少需要两个批次")
        parents = [self._get_active_batch(bid) for bid in batch_ids]
        order_ids = {b.order_id for b in parents}
        if len(order_ids) != 1:
            raise DomainError("只能合并同一订单的批次")
        if len({b.sku for b in parents}) != 1 or len({b.temp_zone for b in parents}) != 1:
            raise DomainError("品类或温区不同的批次不能合并")
        common = {stage.value for stage in Stage.sequence()}
        for parent in parents:
            common &= set(parent.confirmations)
        locations = {b.location_id for b in parents}
        merged = Batch(
            id=self.repo.next_id("B"), order_id=parents[0].order_id, sku=parents[0].sku,
            quantity=sum(b.quantity for b in parents), temp_zone=parents[0].temp_zone,
            location_id=next(iter(locations)) if len(locations) == 1 else None,
            parent_ids=[b.id for b in parents], created_at=at,
            confirmations={k: v for k, v in parents[0].confirmations.items() if k in common},
        )
        merged = self.repo.add(merged)
        for parent in parents:
            parent.quantity = 0
            self._retire_if_empty(parent)
        return merged

    def report_exception(self, user: model.User | str, batch_id: str,
                         exception_type: model.ExceptionType, at_stage: Stage, quantity: int,
                         at: str, target_order_id: str | None = None,
                         target_window_id: str | None = None,
                         target_temp_zone: str | None = None, note: str = "") -> BatchException:
        """登记改配/破损/温区降级。

        受影响数量切为子批次：已完成节点保留在谱系上，未完成链路按
        异常类型终止（破损）或改道（改配、降级）。
        """
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR)
        parent = self._get_active_batch(batch_id)
        if quantity <= 0 or quantity > parent.quantity:
            raise DomainError("异常数量超出批次现存数量")
        if exception_type is model.ExceptionType.REASSIGN and not target_order_id:
            raise DomainError("改配必须指定目标订单")
        if exception_type is model.ExceptionType.DOWNGRADE and not target_temp_zone:
            raise DomainError("温区降级必须指定目标温区")

        child = self._spawn_child(
            parent, quantity, at,
            order_id=target_order_id,
            temp_zone=target_temp_zone if exception_type is model.ExceptionType.DOWNGRADE else None,
            copy_through=at_stage,
        )
        event = BatchException(
            id=self.repo.next_id("EX"), batch_id=child.id,
            exception_type=exception_type, at_stage=at_stage, quantity=quantity,
            created_at=at, target_order_id=target_order_id,
            target_window_id=target_window_id,
            target_temp_zone=target_temp_zone, note=note,
        )
        self.repo.add(event)
        child.exception_ids.append(event.id)
        if target_window_id:
            self.repo.get(RailWindow, target_window_id)
            child.rail_window_id = target_window_id

        parent.quantity -= quantity
        self._retire_if_empty(parent)
        if exception_type is model.ExceptionType.DAMAGE:
            child.status = BatchStatus.CLOSED
        return event

    def assign_location(self, user: model.User | str, batch_id: str, location_id: str) -> Batch:
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR, Role.TENANT)
        batch = self._get_active_batch(batch_id)
        self._assert_order_visible(user, batch.order_id)
        location = self.repo.get(Location, location_id)
        if location.temp_zone != batch.temp_zone:
            raise DomainError("库位温区与批次温区不一致")
        batch.location_id = location_id
        return batch

    def assign_window(self, user: model.User | str, batch_id: str, window_id: str) -> Batch:
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR, Role.RAIL_TEAM)
        batch = self._get_active_batch(batch_id)
        self.repo.get(RailWindow, window_id)
        batch.rail_window_id = window_id
        return batch

    # ==================================================================
    # 逐段确认
    # ==================================================================

    def confirm_stage(self, user: model.User | str, batch_id: str, stage: Stage,
                      at: str) -> Batch:
        user = self._user(user) if isinstance(user, str) else user
        if stage is Stage.RAIL_HANDOVER:
            self._require_role(user, Role.OPERATOR, Role.RAIL_TEAM)
        else:
            self._require_role(user, Role.OPERATOR)
        batch = self.repo.get(Batch, batch_id)
        self._assert_order_visible(user, batch.order_id)
        if batch.status == BatchStatus.CLOSED:
            raise DomainError("已终止批次不能继续确认节点")
        if stage.value in batch.confirmations:
            # 节点确认幂等：重复回执不改变状态
            return batch

        for previous in Stage.sequence():
            if previous is stage:
                break
            if previous.value not in batch.confirmations:
                raise DomainError(f"须先完成前置节点 {previous.value}")

        if stage is Stage.PUTAWAY and not batch.location_id:
            raise DomainError("上架前必须分配库位")

        if stage is Stage.RAIL_HANDOVER:
            window = self.repo.find(RailWindow, batch.rail_window_id or "")
            if window is None:
                raise DomainError("交铁路前必须指定班列窗口")
            if window.closed:
                raise CutoffError("班列窗口已截关")
            if at > window.cutoff_at:
                raise CutoffError(f"超过截关时间 {window.cutoff_at}")
            window.handed_batch_ids.append(batch.id)
            batch.status = BatchStatus.HANDED_OVER

        batch.confirmations[stage.value] = (at, user.id)
        return batch

    def close_window(self, user: model.User | str, window_id: str, at: str) -> RailWindow:
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR, Role.RAIL_TEAM)
        window = self.repo.get(RailWindow, window_id)
        if at < window.cutoff_at:
            raise DomainError("未到截关时间，不能关闭窗口")
        window.closed = True
        return window

    # ==================================================================
    # 设备计量：读数、版本、幂等、隔离、缺口
    # ==================================================================

    def _accepted_readings(self, meter_id: str) -> list[Reading]:
        readings = [r for r in self.repo.list(Reading)
                    if r.meter_id == meter_id and r.status is ReadingStatus.ACCEPTED]
        return sorted(readings, key=lambda r: (r.read_at, r.version))

    def _renumber_versions(self, meter_id: str) -> None:
        """按抄表时点重排版本号：补抄读数插入历史位置，后续版本顺延。"""
        for index, reading in enumerate(self._accepted_readings(meter_id), start=1):
            reading.version = index

    def schedule_meter_reading(self, user: model.User | str, meter_id: str,
                               scheduled_at: str, due_at: str) -> MeterGap:
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR, Role.METER_TEAM)
        self.repo.get(Meter, meter_id)
        gap = MeterGap(id=self.repo.next_id("GAP"), meter_id=meter_id,
                       scheduled_at=scheduled_at, due_at=due_at)
        return self.repo.add(gap)

    def report_meter_outage(self, user: model.User | str, meter_id: str,
                            start_at: str, end_at: str, note: str = "") -> MeterGap:
        """设备停机：登记抄表缺口。已有读数与已确认分摊不受影响。"""
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR, Role.METER_TEAM)
        self.repo.get(Meter, meter_id)
        gap = MeterGap(id=self.repo.next_id("GAP"), meter_id=meter_id,
                       scheduled_at=start_at, due_at=end_at)
        self.repo.add(gap)
        alert = Alert(id=self.repo.next_id("ALT"), kind="meter_gap", ref_id=gap.id,
                      message=f"仪表 {meter_id} 停机，需补抄 {start_at} 至 {end_at}：{note}",
                      due_at=end_at, created_at=_now())
        self.repo.add(alert)
        return gap

    def submit_reading(self, user: model.User | str, meter_id: str, read_at: str,
                       value: float, receipt_id: str) -> Reading:
        """提交读数回执。

        - 回执编号相同且数值一致：幂等，直接返回原读数；
        - 回执编号相同但数值（或仪表/时间）不一致：隔离，不生成版本；
        - 新回执：读数不得小于上一版本，采纳后顺延版本号并补齐缺口。
        """
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.METER_TEAM)
        self.repo.get(Meter, meter_id)
        if value < 0:
            raise DomainError("读数不能为负")

        prior = next((r for r in self.repo.list(Reading) if r.receipt_id == receipt_id), None)
        if prior is not None:
            same = (prior.meter_id == meter_id and prior.value == value
                    and prior.read_at == read_at)
            if same:
                return prior
            quarantined = Reading(
                id=self.repo.next_id("RD"), receipt_id=receipt_id, meter_id=meter_id,
                read_at=read_at, value=value, received_at=_now(),
                version=prior.version, status=ReadingStatus.QUARANTINED,
                reader_user_id=user.id,
                quarantine_reason=(
                    f"回执 {receipt_id} 数值冲突：已收 {prior.meter_id}@{prior.read_at}"
                    f"={prior.value}，又来 {meter_id}@{read_at}={value}"),
            )
            self.repo.add(quarantined)
            raise ConflictError(
                f"回执 {receipt_id} 编号相同数值不同，已隔离 {quarantined.id}")

        accepted = self._accepted_readings(meter_id)
        predecessor = next((r for r in reversed(accepted) if r.read_at <= read_at), None)
        successor = next((r for r in accepted if r.read_at > read_at), None)
        if predecessor is not None and value < predecessor.value:
            raise DomainError("读数不大于上一时点读数，疑似抄录错误")
        if successor is not None and value > successor.value:
            raise DomainError("补抄读数大于后续时点已采纳读数，数据矛盾")
        reading = Reading(
            id=self.repo.next_id("RD"), receipt_id=receipt_id, meter_id=meter_id,
            read_at=read_at, value=value, received_at=_now(),
            version=len(accepted) + 1, reader_user_id=user.id,
        )
        self.repo.add(reading)
        self._renumber_versions(meter_id)

        # 只有落在缺口时间窗内的补抄读数才能补齐缺口；
        # 之后时点的正常读数不代表缺失时点已抄。
        for gap in self.repo.list(MeterGap):
            if (gap.meter_id == meter_id and gap.resolved_reading_id is None
                    and gap.scheduled_at <= read_at <= gap.due_at):
                gap.resolved_reading_id = reading.id
                self._resolve_alert("meter_gap", gap.id)
        return reading

    def resolve_quarantine(self, user: model.User | str, quarantined_id: str,
                           accept: bool) -> Reading:
        """人工处理隔离读数：采纳则按新版本入账，否则维持隔离作废标记。"""
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR, Role.REVIEWER)
        reading = self.repo.get(Reading, quarantined_id)
        if reading.status is not ReadingStatus.QUARANTINED:
            raise DomainError("该读数不在隔离状态")
        if accept:
            accepted = self._accepted_readings(reading.meter_id)
            predecessor = next((r for r in reversed(accepted)
                                if r.read_at <= reading.read_at), None)
            successor = next((r for r in accepted if r.read_at > reading.read_at), None)
            if predecessor is not None and reading.value < predecessor.value:
                raise DomainError("隔离读数小于前一时点读数，不能采纳")
            if successor is not None and reading.value > successor.value:
                raise DomainError("隔离读数大于后续时点读数，不能采纳")
            reading.status = ReadingStatus.ACCEPTED
            self._renumber_versions(reading.meter_id)
        else:
            reading.status = ReadingStatus.SUPERSEDED
        return reading

    def list_quarantined(self) -> list[Reading]:
        return [r for r in self.repo.list(Reading) if r.status is ReadingStatus.QUARANTINED]

    # ==================================================================
    # 能耗分摊与碳排
    # ==================================================================

    def _latest_factor(self, period_end: str) -> CarbonFactor:
        factors = [f for f in self.repo.list(CarbonFactor) if f.valid_from <= period_end]
        if not factors:
            raise DomainError("缺少有效的碳排换算因子")
        return sorted(factors, key=lambda f: (f.valid_from, f.version))[-1]

    def _occupancy(self, meter: Meter, period_start: str,
                   period_end: str) -> list[tuple[Batch, int, Order]]:
        """区间内公共表服务温区中的在库批次及占用箱量。"""
        result: list[tuple[Batch, int, Order]] = []
        for batch in self.repo.list(Batch):
            if batch.temp_zone not in meter.serves_temp_zones:
                continue
            location = self.repo.find(Location, batch.location_id or "")
            if location is None:
                continue
            putaway = batch.confirmations.get(Stage.PUTAWAY.value)
            if putaway is None or putaway[0] > period_end:
                continue
            handover = batch.confirmations.get(Stage.RAIL_HANDOVER.value)
            if handover is not None and handover[0] < period_start:
                continue
            order = self.repo.get(Order, batch.order_id)
            result.append((batch, batch.quantity, order))
        return result

    def allocate_usage(self, user: model.User | str, meter_id: str, period_start: str,
                       period_end: str, unit_price: float = 0.0,
                       factor_id: str | None = None) -> Allocation:
        """按相邻两版已采纳读数计算用量并分摊到责任企业。"""
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.OPERATOR)
        meter = self.repo.get(Meter, meter_id)
        if period_start >= period_end:
            raise DomainError("分摊区间无效")
        for existing in self.repo.list(Allocation):
            if (existing.meter_id == meter_id
                    and existing.period_start == period_start
                    and existing.period_end == period_end):
                raise DomainError("该仪表区间已分摊，不能重复计量")

        readings = self._accepted_readings(meter_id)
        start_reading = next((r for r in readings if r.read_at == period_start), None)
        end_reading = next((r for r in readings if r.read_at == period_end), None)
        if start_reading is None or end_reading is None:
            raise DomainError("分摊区间两端必须各有一版已采纳读数")
        if end_reading.version != start_reading.version + 1:
            raise DomainError("区间两端必须是相邻版本的读数")
        # 区间内存在未补齐的抄表缺口（含设备停机）时不得分摊
        for gap in self.repo.list(MeterGap):
            if (gap.meter_id == meter_id and gap.resolved_reading_id is None
                    and period_start < gap.scheduled_at <= period_end):
                raise DomainError(f"抄表缺口 {gap.id} 未补齐，不能分摊")

        usage = round(end_reading.value - start_reading.value, 6)
        if usage < 0:
            raise DomainError("用量为负，读数顺序异常")
        factor = (self.repo.get(CarbonFactor, factor_id) if factor_id
                  else self._latest_factor(period_end))
        carbon_kg = round(usage * factor.kg_per_unit, 3)

        lines: list[AllocationLine] = []
        if meter.owner_company_id is not None:
            lines.append(AllocationLine(
                company_id=meter.owner_company_id, batch_id=None, share=usage,
                basis="私表用量全部归表主企业"))
        else:
            occupied = self._occupancy(meter, period_start, period_end)
            total = sum(qty for _, qty, _ in occupied)
            if total <= 0:
                raise DomainError("公共表区间内无温区占用依据，无法解释跨租户电费")
            grouped: dict[tuple[str, str | None], float] = defaultdict(float)
            for batch, qty, order in occupied:
                grouped[(order.owner_company_id, batch.id)] += usage * qty / total
            for (company_id, batch_id), share in sorted(grouped.items()):
                lines.append(AllocationLine(
                    company_id=company_id, batch_id=batch_id, share=round(share, 6),
                    basis=f"温区 {meter.serves_temp_zones} 占用箱量分摊"))

        allocation = Allocation(
            id=self.repo.next_id("AL"), meter_id=meter_id,
            period_start=period_start, period_end=period_end, usage=usage,
            unit=meter.unit, factor_id=factor.id, factor_version=factor.version,
            carbon_kg=carbon_kg, lines=lines, created_at=_now(),
            unit_price=unit_price,
            metadata={
                "start_reading_id": start_reading.id,
                "end_reading_id": end_reading.id,
                "start_version": start_reading.version,
                "end_version": end_reading.version,
                "factor_source": factor.source,
            },
        )
        return self.repo.add(allocation)

    # ==================================================================
    # 结算复核与争议（职责分离）
    # ==================================================================

    def confirm_review(self, user: model.User | str, allocation_id: str,
                       at: str | None = None) -> Allocation:
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.REVIEWER)
        allocation = self.repo.get(Allocation, allocation_id)
        if allocation.review is ReviewStatus.CONFIRMED:
            return allocation
        allocation.review = ReviewStatus.CONFIRMED
        allocation.status = AllocationStatus.CONFIRMED
        allocation.confirmed_by = user.id
        allocation.confirmed_at = at or _now()
        self._resolve_alert("review", allocation.id)
        return allocation

    def raise_dispute(self, user: model.User | str, allocation_id: str, reason: str,
                      requested_relief: float) -> Dispute:
        # 企业商务或身兼复核的企业人员可代本方提出争议，
        # 但提出人将在审批环节被排除，不能自提自批。
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.TENANT, Role.REVIEWER)
        allocation = self.repo.get(Allocation, allocation_id)
        if not allocation.lines_for(user.company_id):
            raise AuthError("只能对本方费用提出争议")
        if allocation.status not in (AllocationStatus.CONFIRMED, AllocationStatus.ADJUSTED,
                                     AllocationStatus.REJECTED):
            raise DomainError("只能对已复核确认的分摊提出争议")
        if not 0 < requested_relief <= allocation.usage:
            raise DomainError("减免用量超出区间用量")
        allocation.status = AllocationStatus.DISPUTED
        dispute = Dispute(
            id=self.repo.next_id("DP"), allocation_id=allocation_id,
            company_id=user.company_id, raised_by=user.id, raised_at=_now(),
            reason=reason, requested_relief=requested_relief)
        self.repo.add(dispute)
        allocation.dispute_id = dispute.id
        return dispute

    def decide_dispute(self, user: model.User | str, dispute_id: str, approve: bool,
                       granted_relief: float = 0.0, note: str = "") -> Dispute:
        """批准或驳回争议减免。

        录入计量的人不能批准：除要求复核角色外，提交过该仪表读数的
        用户、以及争议提出人本人均被拒绝。
        """
        user = self._user(user) if isinstance(user, str) else user
        self._require_role(user, Role.REVIEWER)
        dispute = self.repo.get(Dispute, dispute_id)
        if dispute.status is not DisputeStatus.OPEN:
            raise DomainError("争议已有结论")
        allocation = self.repo.get(Allocation, dispute.allocation_id)
        if dispute.raised_by == user.id:
            raise AuthError("争议提出人不能审批本人争议")
        entered = {r.reader_user_id for r in self.repo.list(Reading)
                   if r.meter_id == allocation.meter_id}
        if user.id in entered:
            raise AuthError("录入该仪表计量的人不能批准争议减免")

        dispute.decided_by = user.id
        dispute.decided_at = _now()
        dispute.decision_note = note
        if approve:
            if not 0 < granted_relief <= allocation.usage - allocation.adjustment:
                raise DomainError("批准减免量超出可减免用量")
            dispute.status = DisputeStatus.APPROVED
            dispute.granted_relief = granted_relief
            allocation.adjustment = round(allocation.adjustment + granted_relief, 6)
            allocation.status = AllocationStatus.ADJUSTED
        else:
            dispute.status = DisputeStatus.REJECTED
            allocation.status = AllocationStatus.CONFIRMED
        self._resolve_alert("review", allocation.id)
        return dispute

    # ==================================================================
    # 租户视图
    # ==================================================================

    def list_batches(self, user: model.User | str) -> list[Batch]:
        user = self._user(user) if isinstance(user, str) else user
        batches = self.repo.list(Batch)
        if user.role in (Role.OPERATOR, Role.AUDITOR, Role.REVIEWER, Role.RAIL_TEAM,
                         Role.METER_TEAM):
            return batches
        return [b for b in batches if self._can_see_order(user, b.order_id)]

    def list_allocations(self, user: model.User | str) -> list[Allocation]:
        """企业只能看到本方费用：返回副本中仅保留本方明细行。"""
        user = self._user(user) if isinstance(user, str) else user
        out: list[Allocation] = []
        for allocation in self.repo.list(Allocation):
            if user.role in (Role.OPERATOR, Role.AUDITOR, Role.REVIEWER):
                out.append(allocation)
                continue
            own = allocation.lines_for(user.company_id)
            if not own:
                continue
            visible = Allocation(
                id=allocation.id, meter_id=allocation.meter_id,
                period_start=allocation.period_start, period_end=allocation.period_end,
                usage=allocation.usage, unit=allocation.unit,
                factor_id=allocation.factor_id, factor_version=allocation.factor_version,
                carbon_kg=allocation.carbon_kg, lines=own, status=allocation.status,
                review=allocation.review, created_at=allocation.created_at,
                adjustment=allocation.adjustment, unit_price=allocation.unit_price)
            out.append(visible)
        return out

    # ==================================================================
    # 恢复：缺口、截关、结算复核
    # ==================================================================

    def _resolve_alert(self, kind: str, ref_id: str) -> None:
        for alert in self.repo.list(Alert):
            if alert.kind == kind and alert.ref_id == ref_id and not alert.resolved:
                alert.resolved = True

    def _raise_alert(self, kind: str, ref_id: str, message: str, due_at: str) -> Alert:
        """登记或刷新未决告警：同一事项消息可能随状态变化（如未交批次数）。"""
        for alert in self.repo.list(Alert):
            if alert.kind == kind and alert.ref_id == ref_id and not alert.resolved:
                alert.message = message
                alert.due_at = due_at
                return alert
        alert = Alert(id=self.repo.next_id("ALT"), kind=kind, ref_id=ref_id,
                      message=message, due_at=due_at, created_at=_now())
        return self.repo.add(alert)

    def recover(self, now: str | None = None) -> list[Alert]:
        """系统恢复后重建待办告警，重复执行不产生重复告警。"""
        moment = datetime.fromisoformat(now) if now else datetime.now(timezone.utc).replace(tzinfo=None)
        now_iso = moment.replace(microsecond=0).isoformat()
        alerts: list[Alert] = []

        # 1) 抄表缺口（含设备停机后的补抄）
        for gap in self.repo.list(MeterGap):
            if gap.resolved_reading_id is None:
                alerts.append(self._raise_alert(
                    "meter_gap", gap.id,
                    f"仪表 {gap.meter_id} 存在抄表缺口，应抄 {gap.scheduled_at}，限期 {gap.due_at}",
                    gap.due_at))
            else:
                self._resolve_alert("meter_gap", gap.id)

        # 2) 截关提醒：未来 24 小时内临窗或已超截关未关闭
        for window in self.repo.list(RailWindow):
            if window.closed:
                self._resolve_alert("cutoff", window.id)
                continue
            cutoff = datetime.fromisoformat(window.cutoff_at)
            if cutoff - _CUTOFF_LOOKAHEAD <= moment:
                pending = [bid for bid in self._assigned_batch_ids(window.id)
                           if Stage.RAIL_HANDOVER.value
                           not in self.repo.get(Batch, bid).confirmations]
                tail = f"，尚有 {len(pending)} 批未交" if pending else ""
                overdue = "已超过截关时间" if moment > cutoff else "临近截关"
                alerts.append(self._raise_alert(
                    "cutoff", window.id,
                    f"班列 {window.train_code}（{window.id}）{overdue}{tail}",
                    window.cutoff_at))

        # 3) 结算复核：待确认分摊与未决争议
        for allocation in self.repo.list(Allocation):
            needs = (allocation.review is ReviewStatus.PENDING
                     or allocation.status is AllocationStatus.DISPUTED)
            if needs:
                word = "争议待审批" if allocation.status is AllocationStatus.DISPUTED else "待复核"
                alerts.append(self._raise_alert(
                    "review", allocation.id,
                    f"分摊 {allocation.id}（仪表 {allocation.meter_id}）{word}",
                    allocation.period_end))
            elif allocation.review is ReviewStatus.CONFIRMED:
                self._resolve_alert("review", allocation.id)

        return sorted([a for a in alerts if not a.resolved],
                      key=lambda a: (a.due_at, a.id))

    def _assigned_batch_ids(self, window_id: str) -> list[str]:
        return [b.id for b in self.repo.list(Batch) if b.rail_window_id == window_id]

    def pending_alerts(self) -> list[Alert]:
        return [a for a in self.repo.list(Alert) if not a.resolved]

    # ==================================================================
    # 溯源：一笔能耗分摊还原全貌
    # ==================================================================

    def _lineage(self, batch_id: str) -> dict[str, list[str]]:
        children: dict[str, list[str]] = defaultdict(list)
        for batch in self.repo.list(Batch):
            for parent in batch.parent_ids:
                children[parent].append(batch.id)

        ancestors: list[str] = []
        stack = list(self.repo.get(Batch, batch_id).parent_ids)
        while stack:
            current = stack.pop()
            ancestors.append(current)
            stack.extend(self.repo.get(Batch, current).parent_ids)
        descendants: list[str] = []
        stack = list(children.get(batch_id, []))
        while stack:
            current = stack.pop()
            descendants.append(current)
            stack.extend(children.get(current, []))
        return {"ancestors": ancestors, "descendants": descendants}

    def trace_allocation(self, user: model.User | str, allocation_id: str) -> dict[str, Any]:
        """从一笔能耗分摊还原：读数版本、因子依据、货物去向与审批依据。"""
        user = self._user(user) if isinstance(user, str) else user
        allocation = self.repo.get(Allocation, allocation_id)
        meter = self.repo.get(Meter, allocation.meter_id)

        if user.role not in (Role.OPERATOR, Role.AUDITOR, Role.REVIEWER):
            if not allocation.lines_for(user.company_id):
                raise AuthError("只能查看本方费用")

        readings = [
            {
                "id": r.id, "receipt_id": r.receipt_id, "version": r.version,
                "read_at": r.read_at, "value": r.value, "status": r.status.value,
                "reader_user_id": r.reader_user_id,
            }
            for r in self._accepted_readings(allocation.meter_id)
            if r.id in (allocation.metadata["start_reading_id"],
                        allocation.metadata["end_reading_id"])
        ]
        factor = self.repo.get(CarbonFactor, allocation.factor_id)

        cargo: list[dict[str, Any]] = []
        for line in allocation.lines:
            if user.role not in (Role.OPERATOR, Role.AUDITOR, Role.REVIEWER) \
                    and line.company_id != user.company_id:
                continue  # 企业视角不泄露同表其他租户的货物
            entry: dict[str, Any] = {
                "company_id": line.company_id,
                "batch_id": line.batch_id,
                "share": line.share,
                "basis": line.basis,
            }
            if line.batch_id:
                batch = self.repo.get(Batch, line.batch_id)
                order = self.repo.get(Order, batch.order_id)
                owner = self.repo.get(model.Company, order.owner_company_id)
                lineage = self._lineage(batch.id)
                # 异常可能登记在本批次或其后继（破损/改配子批次）上，
                # 一并还原，才能解释分摊期占用电量的货物最终去向。
                exception_targets = [batch] + [
                    self.repo.get(Batch, bid)
                    for bid in lineage["descendants"]
                ]
                exceptions: list[dict[str, Any]] = []
                for target in exception_targets:
                    for ex_id in target.exception_ids:
                        ex = self.repo.get(BatchException, ex_id)
                        exceptions.append({
                            "batch_id": target.id,
                            "id": ex.id, "type": ex.exception_type.value,
                            "at_stage": ex.at_stage.value, "quantity": ex.quantity,
                            "target_order_id": ex.target_order_id,
                            "target_temp_zone": ex.target_temp_zone,
                            "note": ex.note,
                        })
                entry.update({
                    "order": {"id": order.id, "destination": order.destination,
                              "owner_company_id": order.owner_company_id,
                              "owner_name": owner.name},
                    "temp_zone": batch.temp_zone,
                    "location_id": batch.location_id,
                    "quantity": batch.quantity,
                    "status": batch.status.value,
                    "confirmations": [
                        {"stage": stage, "at": batch.confirmations[stage.value][0],
                         "by": batch.confirmations[stage.value][1]}
                        for stage in Stage.sequence()
                        if stage.value in batch.confirmations
                    ],
                    "lineage": lineage,
                    "exceptions": exceptions,
                })
            cargo.append(entry)

        dispute: dict[str, Any] | None = None
        if allocation.dispute_id:
            dp = self.repo.get(Dispute, allocation.dispute_id)
            dispute = {
                "id": dp.id, "status": dp.status.value, "reason": dp.reason,
                "raised_by": dp.raised_by, "requested_relief": dp.requested_relief,
                "decided_by": dp.decided_by, "granted_relief": dp.granted_relief,
                "decision_note": dp.decision_note,
            }

        return {
            "allocation_id": allocation.id,
            "meter": {"id": meter.id, "name": meter.name, "kind": meter.kind.value,
                      "owner_company_id": meter.owner_company_id,
                      "serves_temp_zones": meter.serves_temp_zones},
            "period": {"start": allocation.period_start, "end": allocation.period_end},
            "usage": allocation.usage,
            "adjustment": allocation.adjustment,
            "payable_usage": allocation.payable_usage(),
            "unit": allocation.unit,
            "reading_versions": sorted(readings, key=lambda r: r["version"]),
            "carbon": {"factor_id": factor.id, "factor_version": factor.version,
                       "source": factor.source, "kg_per_unit": factor.kg_per_unit,
                       "total_kg": allocation.carbon_kg},
            "review": {"status": allocation.review.value,
                       "allocation_status": allocation.status.value,
                       "confirmed_by": allocation.confirmed_by,
                       "confirmed_at": allocation.confirmed_at},
            "dispute": dispute,
            "cargo": cargo,
        }
