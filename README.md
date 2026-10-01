# 高标仓跨境批次与绿色计量协同

广州东部公铁联运枢纽高标仓的批次与计量服务：记录订单、箱货拆分合并、库位温区、
班列窗口、设备计量点、读数版本、责任企业和碳排换算依据，支撑跨企业核对、能耗分摊、
争议减免与系统恢复。

## 服务能力

| 需求 | 实现入口 |
| --- | --- |
| 订单 / 批次拆分合并 / 库位温区 | `register_order`、`register_batch`、`split_batch`、`merge_batches`、`place_batch` |
| 到场→卸货→质检→上架→交铁路逐段确认 | `confirm_stage`（`STAGES` 定序，确认幂等） |
| 班列窗口与截关 | `register_window`、`assign_window`，交铁路强校验截关 |
| 改配 / 破损 / 设备停机只影响未完成链路 | `report_incident`（`reassign` / `damage` / `meter_stop`） |
| 计量点 / 读数版本 | `register_meter`、`submit_reading`、`correct_reading` |
| 相同回执幂等、同编号异值隔离、重复周期拦截 | `submit_reading`（`QuarantineConflict` / `DoubleMeasurement`） |
| 跨租户电费分摊与碳排换算 | `submit_driver`、`publish_carbon_factor`、`create_allocation` |
| 录入与审批职责分离 | `grant_relief`（审批人不得是该笔读数录入人） |
| 企业只见本方货物与费用 | `visible_batches`、`visible_settlements`、`trace_settlement` |
| 一笔分摊还原货物去向 / 仪表版本 / 审批依据 | `trace_settlement` |
| 恢复后续跑抄表缺口、截关提醒、结算复核 | `EventStore`（JSONL）+ `RecoveryRunner` |

## 架构

所有状态变化都是只追加事件，命令只做校验、`_apply` 统一推进状态：

```
命令（校验规则） -> 事件信封 {seq,type,data} -> 内存状态
                                    └-> JSONL 事件文件（崩溃恢复时重放）
```

恢复任务（`meter_gap` / `cutoff_reminder` / `settlement_review`）同样是事件，
到期执行一次并落 `task_completed`，因此进程反复重启不会重复提醒或重复复核。

代码布局：

- `src/hub/service.py` — 批次谱系、五段链路、计量版本、分摊结算、权限与追溯
- `src/hub/recovery.py` — JSONL 事件存储与恢复运行器
- `src/hub/errors.py` — 领域异常（截关、跳序、双重计量、隔离、越权等）
- `tests/` — 35 条规则测试（合成数据，不含真实身份信息）
- `contracts/context.schema.json` — 领域资料结构；`contracts/event.schema.json` — 事件信封
- `fixtures/context.json` — 领域参与方、事实与约束（版本 2）

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m py_compile $(find src -name '*.py')
```

两条命令只读取仓库内文件，不需要连接外部业务系统。
