# 批次与计量服务设计

面向广州东部公铁联运枢纽高标仓的仓干配一体化批次与绿色计量服务。
纯 Python 3.11，标准库实现，无外部依赖。

## 模块结构

```
src/hub/
├── model.py        领域模型：枚举、实体（dataclass）、异常
├── codec.py        实体 ↔ JSON 快照编解码（含枚举/集合/联合类型）
├── repository.py   内存仓储：增删查、编号序列、快照存取
└── service.py      HubService：全部业务用例与权限规则
tests/
├── test_batch_lineage.py       批次谱系、逐段确认、异常、截关
├── test_metering.py            幂等/隔离/版本/缺口/分摊/碳排
├── test_access_settlement.py   租户隔离、职责分离、复核与争议
└── test_recovery_trace.py      快照恢复、recover 告警、端到端溯源
```

## 领域对象

| 对象 | 要点 |
| --- | --- |
| `Company` / `User` | 责任企业与用户；企业带 `visible_order_ids` 数据范围 |
| `Order` | 订单，归属货主企业 |
| `Batch` | 箱货批次，`quantity` 恒为现存数量；`parent_ids` 构成谱系；`confirmations` 记录每段确认时间与确认人 |
| `BatchException` | 改配/破损/温区降级，记录发生节点、数量与去向 |
| `Location` | 库位，带温区（frozen/chilled/ambient）与可选挂靠电表 |
| `RailWindow` | 班列窗口，含截关时间与已交批次清单 |
| `Meter` | 计量点：冷链私表（归某企业）或公共总表（服务多个温区） |
| `Reading` | 读数回执：回执编号、值、抄表时点、版本、状态（采纳/隔离/作废） |
| `MeterGap` | 抄表缺口：应抄时点、限期、补齐读数 |
| `CarbonFactor` | 碳排换算因子，版本化 |
| `Allocation` / `AllocationLine` | 一笔用量及按企业/批次的分摊行，含因子版本、复核与争议状态、减免量 |
| `Dispute` | 企业争议与审批结论 |
| `Alert` | 恢复后待办：meter_gap / cutoff / review |

## 核心规则索引

### 1. 批次链路

- 收货即确认“到场”，需要运营角色；数量必须为正。
- `confirm_stage` 强制节点顺序（`Stage.sequence()`），缺前置节点直接拒绝；
  重复确认同一节点幂等返回。
- 上架（PUTAWAY）前必须分配与批次同温区的库位。
- 拆分：子批次继承父批次**全部**已完成节点；父批次现存数量扣减。
- 合并：新批次 `parent_ids` 指向全部父批次，只继承**共同**完成的节点；
  仅允许同订单、同 SKU、同温区合并。
- 异常（`report_exception`）：
  - 受影响数量切为子批次，只继承 `at_stage` 之前的确认；
  - 改配：子批次转目标订单与班列窗口；
  - 温区降级：子批次换温区，需落到匹配温区的库位；
  - 破损：子批次直接终止，不进入异常节点及以后，但之前节点保留；
  - 父批次现存数量相应扣减，扣尽即关闭。
- 交铁路（RAIL_HANDOVER）：必须已指定窗口、窗口未关闭、未晚于 `cutoff_at`；
  成功后批次置为 handed_over，窗口记录收箱。

### 2. 读数与版本

- METER_TEAM 角色提交读数；读数非负、不小于前一时点（累积表）。
- 回执编号 `receipt_id` 去重：
  - 同编号且（仪表、时点、数值）完全一致 → 幂等，返回原读数；
  - 同编号任一要素不同 → 生成 QUARANTINED 读数并抛 `ConflictError`，
    不占用版本号；运营/复核可 `resolve_quarantine` 采纳（重排版本）或作废。
- 版本按**抄表时点**排序：恢复后补抄历史时点会插入正确位置，后续版本顺延。
- 停机 `report_meter_outage` 与计划抄表都形成 `MeterGap`；
  只有落在缺口时间窗 `[scheduled_at, due_at]` 内的补抄才算补齐。

### 3. 分摊与碳排

- 用量 = 区间两端相邻版本读数之差；非相邻版本、端点缺读数、用量为负均拒绝。
- 区间（含端点之后）存在未补齐缺口 → 拒绝分摊；同仪表同区间重复分摊 → 拒绝。
- 私表：整笔用量归表主；公共表：按区间内在库（已上架且未在区间前交铁路）
  批次的温区占用箱量比例分摊，并按批次/企业生成可解释的明细行（`basis`）。
- 碳排 = 用量 × 版本化因子（取区间结束前有效的最新版本），
  分摊固化 `factor_id / factor_version / source`；减免同步调减费用与碳排。
- 费用按原占比在减免后重新计算（`amount_for` / `carbon_for`）。

### 4. 权限与结算

- 角色：OPERATOR（运营）、TENANT（企业）、METER_TEAM、RAIL_TEAM、
  REVIEWER（复核）、AUDITOR（只读全量）。
- 企业只能看到 `visible_order_ids`（或货主关系）范围内的订单与批次；
  `list_allocations` 给企业的副本只保留本方分摊行，不泄露同表租户。
- 分摊须 REVIEWER `confirm_review` 才进入可结算状态；
  企业只能对已确认分摊中的**本方份额**提争议。
- `decide_dispute` 排除两类人：提交过该仪表读数的人（录入人），
  以及争议提出人本人；第三方复核角色才可批准减免。

### 5. 恢复与溯源

- `Repository.save_snapshot/load_snapshot` 以 JSON 持久化全部实体与编号序列；
  恢复后业务编号不与历史冲突。
- `recover(now)` 依据快照内状态幂等重建三类告警：
  - meter_gap：未补齐缺口（停机/计划抄表）；
  - cutoff：未来 24 小时临窗或已超时，且消息带实时未交批次数；
  - review：待复核分摊与未决争议。
  重复执行不新增告警，事项推进后旧告警自动解除。
- `trace_allocation` 输出一笔分摊的完整证据链：
  仪表 → 相邻两版读数与回执 → 碳排因子来源/版本 →
  各企业批次的库位温区、逐段确认、谱系祖先/后代、异常去向 →
  复核人与争议审批依据。企业视角自动遮蔽其他租户行。

## 运行

```bash
python3 -m compileall -q src
python3 -m unittest discover -s tests -v
```
