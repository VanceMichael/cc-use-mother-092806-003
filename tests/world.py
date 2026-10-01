"""测试共用的场景搭建辅助。"""

from __future__ import annotations

from src.hub.service import HubService


def build_world(service: HubService | None = None) -> HubService:
    """构造一个含参与方、订单、批次、窗口、仪表和碳因子的基础场景。"""
    s = service or HubService()
    # 参与方：运营、两家租户企业、计量录入人、审核人（另安排一名录入人用于隔离测试）
    s.register_party("op", "operator")
    s.register_party("co_a", "tenant", company="甲企业")
    s.register_party("co_b", "tenant", company="乙企业")
    s.register_party("rec1", "meter_recorder")
    s.register_party("rec2", "meter_recorder")
    s.register_party("aud", "approver")

    s.register_order("ORD-1", "甲企业", "op")
    s.register_order("ORD-2", "乙企业", "op")
    s.register_batch("B-A1", "ORD-1", 100, zone="冷藏2-8℃")
    s.register_batch("B-A2", "ORD-1", 60, zone="冷藏2-8℃")
    s.register_batch("B-B1", "ORD-2", 40, zone="冷冻-18℃")

    s.register_window("W501", "中欧班列X8056", "2026-10-15T18:00")
    s.register_window("W502", "中亚班列X8058", "2026-10-20T18:00")

    s.register_meter("M-COLD", "cold", zone="冷藏2-8℃")
    s.register_meter("M-PUB", "common", zone="公共照明动力")

    s.publish_carbon_factor(
        "CF-2025", 0.581, "华南区域电网2025年度平均排放因子（示例值）"
    )
    return s


def walk_to_rail(s: HubService, batch_id: str, window_id: str, at: str = "2026-10-14T09:00") -> None:
    """让一个批次走完全部五段链路并绑定窗口。"""
    s.assign_window(batch_id, window_id)
    for stage in ("arrival", "unload", "qc", "putaway", "rail_handover"):
        s.confirm_stage(batch_id, stage, "op", at=at)
