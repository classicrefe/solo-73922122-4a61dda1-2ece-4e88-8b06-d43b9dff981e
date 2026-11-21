# 轴承换装里程核算服务

检修数仓侧核算 API：批量接收换装工单，从空轴位重建换装历史，按**件号**累计服役里程——
轴承换到另一台车后，里程随件号连续累计。

技术栈：FastAPI + SQLite（WAL），Docker Compose 一键启动，Swagger UI 在 `/docs` 演示。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

本地开发（无需 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DB_PATH=./data/app.db uvicorn app.main:app --reload
pytest                 # 运行自测
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 批量提交换装工单（整批校验、整批提交、整批退回） |
| GET | `/parts/{part_no}` | 按件号查询：已结束装车段（起止工单、里程差）、累计里程；在役段只报位置 |
| GET | `/cars/{car_no}/state` | 车辆当前各轴位装件 |
| GET | `/orders` | 当前有效工单列表（每号取最高修订号） |

### 工单格式

```json
{
  "orders": [
    {
      "order_no": "WO-1001",
      "revision": 1,
      "ts": "2026-11-01T08:00:00Z",
      "car_no": "TC01",
      "mileage": 120000,
      "changes": [
        {"position": "A1", "off_part": null, "on_part": "BRG-0001"},
        {"position": "A2", "off_part": "BRG-0007", "on_part": "BRG-0002"}
      ]
    }
  ]
}
```

- `ts`：UTC 时刻（ISO 8601，须带时区）。
- `off_part` / `on_part`：换下 / 换上件号，**空号（null 或空串）代表空位**。
- `revision`：递增修订号；更正同一工单时 +1。

### 幂等与修订号

- **同号同版同内容**重送：幂等跳过，只计一次（返回 200，`applied` 为空）。
- **旧版**（修订号低于已存）：整批退回 `STALE_REVISION`。
- **同版异内容**：整批退回 `REVISION_CONTENT_MISMATCH`。
- **更高修订号**：视为更正，替换旧版并全量重建历史。

### 冲突校验（任一冲突即整批退回，HTTP 409，未写入任何内容）

历史从空轴位按 UTC 时刻分组重放，**同一时刻的换下/换上整体判定**（允许同刻跨车
互换、转移），**每个轴位同一时刻最多变动一次**。补录（插入更早时刻的工单）或更正
（更高修订号）后全量重建并重新校验：

| 代码 | 含义 |
|---|---|
| `OFF_PART_MISMATCH` | 旧件对不上：换下件与轴位现装件不一致 |
| `PART_DOUBLE_INSTALL` | 同一件占两处 |
| `MILEAGE_REGRESSION` | 同车里程倒退 |
| `SAME_TS_CONFLICT` | 同刻不一致：同轴位重复变动 / 同车同刻里程不一致 |
| `STALE_REVISION` / `REVISION_CONTENT_MISMATCH` / `DUPLICATE_IN_BATCH` | 版本与批内重复 |

409 响应体 `detail.errors` 列出全部冲突明细（类型、工单号、车号、轴位、件号、说明）。

### 查询示例

```bash
curl -s localhost:8000/parts/BRG-0001 | jq
```

```json
{
  "part_no": "BRG-0001",
  "total_mileage": 600,
  "finished_segments": [
    {"car_no": "TC01", "position": "A1", "start_order": "WO-1001",
     "start_ts": "...", "start_mileage": 120000, "end_order": "WO-1010",
     "end_ts": "...", "end_mileage": 120600, "mileage_delta": 600}
  ],
  "active": {"car_no": "TC02", "position": "B1"}
}
```

`total_mileage` 只累计**已结束**装车段的里程差；在役段只报位置（车号 + 轴位）。

## 设计说明

- **单一事实源是工单表**：`work_orders`（每号仅保留最高修订号）+ `order_changes`。
  `segments` 是每次成功提交后从空轴位全量重放得到的派生表，保证补录 / 更正后
  历史始终一致。
- **事务与并发**：批次校验与落库由进程内写锁串行化；落库在单个
  `BEGIN IMMEDIATE … COMMIT` 事务内完成；SQLite WAL 模式下查询只读最近完整提交的
  快照——提交交错时也不会读到半成品批次。
- **时刻处理**：统一归一为 UTC 微秒精度 ISO 字符串，字典序即时间序；同一时刻的
  工单成组整体判定。

## 已知限制

- 全量重建派生表，工单量极大时可改为增量重放（当前规模下简单可靠）。
- 里程为整数公里；件号 / 车号 / 轴位按字符串精确匹配。
