"""批次与计量服务的领域异常。"""


class DomainError(Exception):
    """所有领域规则冲突的基类。"""


class UnknownReference(DomainError):
    """引用的订单、批次、仪表或结算不存在。"""


class IdConflict(DomainError):
    """编号已被占用且内容不一致。"""


class SequenceViolation(DomainError):
    """链路节点未按到场、卸货、质检、上架、交铁路顺序确认。"""


class CutoffViolation(DomainError):
    """交铁路时间晚于班列截关。"""


class IncidentScopeError(DomainError):
    """改配、破损或设备停机试图影响已经完成的链路。"""


class DoubleMeasurement(DomainError):
    """同一计量点在同一计量周期被重复计量。"""


class QuarantineConflict(DomainError):
    """回执编号相同但读数数值不同，读数已被隔离。"""


class AllocationError(DomainError):
    """缺少驱动因子或计量依据，无法完成跨租户分摊。"""


class PermissionDenied(DomainError):
    """租户越权访问，或违反录入与审批职责分离。"""
