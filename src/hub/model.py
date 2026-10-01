"""批次与计量服务的领域模型。

只表达枢纽业务本身，不涉及存取方式：实体均为 ``@dataclass``，
枚举采用 ``str + Enum`` 以便直接 JSON 序列化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


DOMAIN = "guangzhou-east-hub-batch-metering"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """枢纽内的职责角色。录入与审批分属不同角色。"""

    OPERATOR = "operator"            # 枢纽运营人员
    TENANT = "tenant"                # 仓储/干线/末端租户企业
    METER_TEAM = "meter_team"        # 设备计量班组（公共电量/冷链温区）
    RAIL_TEAM = "rail_team"          # 铁路截关班组
    REVIEWER = "reviewer"            # 费用与碳排复核（可批准争议减免）
    AUDITOR = "auditor"             # 只读审计，可还原任意一笔分摊


class Stage(str, Enum):
    """仓干配链路的逐段节点，按到场先后排列。"""

    ARRIVAL = "arrival"              # 车辆到场
    UNLOAD = "unload"                # 卸货
    QC = "qc"                        # 质检
    PUTAWAY = "putaway"             # 上架
    RAIL_HANDOVER = "rail_handover"  # 交铁路（截关）

    @classmethod
    def sequence(cls) -> list["Stage"]:
        return [cls.ARRIVAL, cls.UNLOAD, cls.QC, cls.PUTAWAY, cls.RAIL_HANDOVER]

    def next_stage(self) -> "Stage | None":
        order = Stage.sequence()
        index = order.index(self)
        return order[index + 1] if index + 1 < len(order) else None


class BatchStatus(str, Enum):
    ACTIVE = "active"        # 仍有未完成链路
    HANDED_OVER = "handed_over"  # 全部现存数量已交铁路
    CLOSED = "closed"        # 破损核销/改配拆尽，链路提前终止


class ExceptionType(str, Enum):
    REASSIGN = "reassign"    # 改配：转去其他订单/班列
    DAMAGE = "damage"        # 破损：数量核销，不进入后续节点
    DOWNGRADE = "downgrade"  # 温区降级：换温区继续履约


class MeterKind(str, Enum):
    COLD_ZONE = "cold_zone"  # 冷链温区电表
    PUBLIC = "public"        # 公共设备总表
    PIECE = "piece"          # 计件/箱量仪表


class ReadingStatus(str, Enum):
    ACCEPTED = "accepted"    # 已采纳，进入计量版本
    QUARANTINED = "quarantined"  # 编号相同数值不同，先隔离
    SUPERSEDED = "superseded"     # 被新版本替代


class AllocationStatus(str, Enum):
    PENDING = "pending"      # 待复核
    CONFIRMED = "confirmed"  # 复核通过，可结算
    DISPUTED = "disputed"    # 被企业提出争议
    ADJUSTED = "adjusted"    # 争议批准后按减免额调整
    REJECTED = "rejected"    # 争议被驳回，恢复原金额


class DisputeStatus(str, Enum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


@dataclass
class Company:
    """责任企业（仓储租户、干线或末端承运企业）。"""

    id: str
    name: str
    role: Role
    # 可查看的订单/货物范围；运营方与审计为 None（全量）
    visible_order_ids: set[str] | None = None


@dataclass
class User:
    id: str
    name: str
    company_id: str
    role: Role


# ---------------------------------------------------------------------------
# 货物与批次
# ---------------------------------------------------------------------------


@dataclass
class Order:
    id: str
    owner_company_id: str          # 货主/委托企业
    destination: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Batch:
    """箱货批次。拆分/合并通过 parent_ids 形成谱系。

    quantity 恒为当前现存数量；破损会减少数量，改配/合并通过
    新建子批次承接，父批次对应数量立即扣减。
    """

    id: str
    order_id: str
    sku: str
    quantity: int
    temp_zone: str                 # 温区，如 frozen / chilled / ambient
    location_id: str | None        # 当前库位
    parent_ids: list[str] = field(default_factory=list)
    status: BatchStatus = BatchStatus.ACTIVE
    created_at: str = ""
    # 各节点的确认时间与确认人，节点名 -> (ISO 时间, 用户 id)
    confirmations: dict[str, tuple[str, str]] = field(default_factory=dict)
    # 本批次关联的异常事件 id
    exception_ids: list[str] = field(default_factory=list)
    rail_window_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def confirmed_stages(self) -> set[Stage]:
        return {Stage(name) for name in self.confirmations}


@dataclass
class BatchException:
    """改配 / 破损 / 温区降级等异常事件。

    异常只作用于事件发生的节点：已完成节点保持不变，
    受影响数量不进入后续节点。
    """

    id: str
    batch_id: str
    exception_type: ExceptionType
    at_stage: Stage
    quantity: int
    created_at: str
    # 改配目标（新订单/新班列窗口）；降级目标温区
    target_order_id: str | None = None
    target_window_id: str | None = None
    target_temp_zone: str | None = None
    note: str = ""


@dataclass
class Location:
    id: str
    zone: str           # 库区
    temp_zone: str      # 温区
    meter_id: str | None = None  # 该库位/温区挂靠的电表


# ---------------------------------------------------------------------------
# 班列窗口
# ---------------------------------------------------------------------------


@dataclass
class RailWindow:
    """班列装车/截关窗口。cutoff 之后不能再交运。"""

    id: str
    train_code: str
    destination: str
    cutoff_at: str          # ISO 时间
    depart_at: str
    handed_batch_ids: list[str] = field(default_factory=list)
    closed: bool = False


# ---------------------------------------------------------------------------
# 计量
# ---------------------------------------------------------------------------


@dataclass
class Meter:
    """设备计量点。

    公共总表可挂多个租户温区（用于按占用量分摊）；
    冷链温区表通常归属单一责任企业。
    """

    id: str
    kind: MeterKind
    name: str
    owner_company_id: str | None       # 私表归属；公共表为 None
    zone: str | None = None
    unit: str = "kWh"
    # 公共表：表下温区/库位，按各批次占用量分摊
    serves_temp_zones: list[str] = field(default_factory=list)


@dataclass
class Reading:
    """一次仪表读数回执。

    receipt_id 为外部回执编号：编号相同、数值相同视为重复回执（幂等）；
    编号相同而数值不同，不得采纳，先隔离待人工处理。
    """

    id: str
    receipt_id: str
    meter_id: str
    read_at: str
    value: float
    received_at: str
    version: int
    status: ReadingStatus = ReadingStatus.ACCEPTED
    reader_user_id: str | None = None
    note: str = ""
    quarantine_reason: str = ""


@dataclass
class MeterGap:
    """抄表缺口：应抄而缺的读数（系统恢复后续抄）。"""

    id: str
    meter_id: str
    scheduled_at: str
    due_at: str
    resolved_reading_id: str | None = None


# ---------------------------------------------------------------------------
# 能耗分摊、碳排与结算
# ---------------------------------------------------------------------------


@dataclass
class CarbonFactor:
    """碳排换算依据。版本化，结算时记录所用因子版本，保证可追溯。"""

    id: str
    source: str          # 依据文件/标准
    kg_per_unit: float  # 每计量单位的碳排千克数
    valid_from: str
    version: int


@dataclass
class AllocationLine:
    """分摊明细：某笔用量分到某企业/某批次的数量。"""

    company_id: str
    batch_id: str | None
    share: float         # 分摊到的用量
    basis: str           # 分摊依据描述（如 占用箱量 120/300）


@dataclass
class Allocation:
    """一笔能耗（相邻有效读数之差）向责任企业的分摊。

    私表用量全部归表主企业；公共表按区间内各批次温区占用量比例分摊。
    """

    id: str
    meter_id: str
    period_start: str
    period_end: str
    usage: float
    unit: str
    factor_id: str
    factor_version: int
    carbon_kg: float
    lines: list[AllocationLine]
    status: AllocationStatus = AllocationStatus.PENDING
    review: ReviewStatus = ReviewStatus.PENDING
    created_at: str = ""
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    dispute_id: str | None = None
    # 争议批准后的减免用量（相应减少费用与碳排）
    adjustment: float = 0.0
    # 计费单价（每单位用量），仅用于结算复核演示
    unit_price: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def lines_for(self, company_id: str) -> list[AllocationLine]:
        return [line for line in self.lines if line.company_id == company_id]

    def usage_for(self, company_id: str) -> float:
        return round(sum(line.share for line in self.lines_for(company_id)), 6)

    def payable_usage(self) -> float:
        """减免后实际计费用量（按原占比分配减免额）。"""
        return round(max(self.usage - self.adjustment, 0.0), 6)

    def amount_for(self, company_id: str) -> float:
        if not self.lines:
            return 0.0
        ratio = self.usage_for(company_id) / self.usage if self.usage else 0.0
        return round(self.payable_usage() * ratio * self.unit_price, 2)

    def carbon_for(self, company_id: str) -> float:
        if not self.lines:
            return 0.0
        ratio = self.usage_for(company_id) / self.usage if self.usage else 0.0
        return round(self.payable_usage() * (self.carbon_kg / self.usage) * ratio, 3)


@dataclass
class Dispute:
    """企业对分摊/费用提出的争议。录入人不得审批本人争议。"""

    id: str
    allocation_id: str
    company_id: str
    raised_by: str
    raised_at: str
    reason: str
    requested_relief: float          # 请求减免用量
    status: DisputeStatus = DisputeStatus.OPEN
    decided_by: str | None = None
    decided_at: str | None = None
    granted_relief: float = 0.0
    decision_note: str = ""


# ---------------------------------------------------------------------------
# 恢复与告警
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    """恢复后需要继续处理的事项：抄表缺口、截关提醒、结算复核。"""

    id: str
    kind: str            # meter_gap / cutoff / review
    ref_id: str
    message: str
    due_at: str
    created_at: str
    resolved: bool = False


class DomainError(Exception):
    """所有业务规则违反的基类。"""


class AuthError(DomainError):
    """越权访问或违反职责分离。"""


class ConflictError(DomainError):
    """读数编号冲突等需要隔离的冲突。"""


class CutoffError(DomainError):
    """超过班列截关时间。"""
