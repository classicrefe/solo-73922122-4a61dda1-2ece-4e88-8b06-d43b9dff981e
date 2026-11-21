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

## `/docs` 演示脚本（登记 → 查询 → 更正）

`docker compose up --build` 后打开 <http://localhost:8000/docs>，按顺序执行
（也可用下方 curl；数据持久化在 `bearing-data` 卷中，`docker compose down -v` 清空）。

**第一步：提交工单**——同铭牌号 `BRG-9` 的旧件两段服役后在 `WO-3` 末卸，
新件于同刻 `WO-4` 首装：

```bash
curl -s -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "orders": [
    {"order_no": "WO-1", "revision": 1, "ts": "2026-11-01T08:00:00Z", "car_no": "TC1",
     "mileage": 1000, "changes": [{"position": "A1", "off_part": null, "on_part": "BRG-9"}]},
    {"order_no": "WO-2", "revision": 1, "ts": "2026-11-05T08:00:00Z", "car_no": "TC1",
     "mileage": 1600, "changes": [{"position": "A1", "off_part": "BRG-9", "on_part": null},
                                  {"position": "A2", "off_part": null, "on_part": "BRG-9"}]},
    {"order_no": "WO-3", "revision": 1, "ts": "2026-11-10T08:00:00Z", "car_no": "TC1",
     "mileage": 2200, "changes": [{"position": "A2", "off_part": "BRG-9", "on_part": null}]},
    {"order_no": "WO-4", "revision": 1, "ts": "2026-11-10T08:00:00Z", "car_no": "TC2",
     "mileage": 8000, "changes": [{"position": "B1", "off_part": null, "on_part": "BRG-9"}]}
  ]}'
```

**第二步：分代登记**（末卸 `WO-3`、首装 `WO-4`，同刻允许）：

```bash
curl -s -X POST localhost:8000/generations -H 'Content-Type: application/json' -d '{
  "part_no": "BRG-9", "old_end_order": "WO-3", "new_start_order": "WO-4"}'
```

**第三步：按代查询**——直接查 `/parts/BRG-9` 会得到 400，须带代次：

```bash
curl -s 'localhost:8000/parts/BRG-9?generation=1'   # 旧件两段，累计 (1600-1000)+(2200-1600)=1200
curl -s 'localhost:8000/parts/BRG-9?generation=2'   # 新件在役 TC2/B1
curl -s localhost:8000/cars/TC2/state               # 在役件标注 generation=2
```

**第四步：幂等 / 冲突 / 更正**：

```bash
# 同对工单重送 → 200 返回原登记；换一对 → 409 GENERATION_CONFLICT
curl -s -X POST localhost:8000/generations -H 'Content-Type: application/json' -d '{
  "part_no": "BRG-9", "old_end_order": "WO-3", "new_start_order": "WO-4"}'

# 更正 WO-4 使其不再装 BRG-9 → 整批 409（GENERATION_ORDER_NOT_MATCHING），查询保留原结果
curl -s -X POST localhost:8000/batches -H 'Content-Type: application/json' -d '{
  "orders": [{"order_no": "WO-4", "revision": 2, "ts": "2026-11-10T08:00:00Z",
              "car_no": "TC2", "mileage": 8000,
              "changes": [{"position": "B1", "off_part": null, "on_part": "BRG-OTHER"}]}]}'
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 批量提交换装工单（整批校验、整批提交、整批退回） |
| POST | `/generations` | 分代登记：旧件最后一次卸车工单 + 新件第一次装车工单 |
| GET | `/generations` | 列出全部分代登记 |
| GET | `/generations/{part_no}` | 按件号查询分代登记 |
| GET | `/parts/{part_no}` | 按件号查询：已结束装车段（起止工单、里程差）、累计里程；在役段只报位置。已分代件号必须带 `?generation=1\|2` |
| GET | `/cars/{car_no}/state` | 车辆当前各轴位装件（已分代件号标注代次） |
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

## 同铭牌号分代（两件实物）

供应商把同一个铭牌号用在两件轴承上时，数据员登记**旧件最后一次卸车**与
**新件第一次装车**的两张工单，系统据此把该件号的装车段切成两代分别核算里程。
工单里始终沿用原件号，不改名。

### 登记

```bash
curl -s -X POST localhost:8000/generations -H 'Content-Type: application/json' -d '{
  "part_no": "BRG-0001",
  "old_end_order": "WO-2003",
  "new_start_order": "WO-2004"
}'
```

校验规则（任一不过即 409 拒绝登记，不写任何内容）：

1. 两张工单都须在当前**有效装车工单**中，且分别包含该件号的一次**换下** / **换上**；
2. 旧件末卸时刻 **不晚于** 新件首装时刻，**允许同刻**（两张不同工单、同一时刻）；
3. 装车段归属：段结束时刻 `<=` 末卸时刻 → **一代（旧件）**；
   段开始时刻 `>=` 首装时刻 → **二代（新件）**；每段须**恰好**归入一代。
   间隔段、跨分界段、开始早于首装的在役段一律判 `GENERATION_SEGMENT_AMBIGUOUS` 拒绝。
4. 同件号重送**同一对**工单：幂等返回原登记（200）；换**另一对**工单：
   `GENERATION_CONFLICT`（409），登记不可改写。

### 登记后查询

已分代件号查询装车段、累计里程、在役位置都**必须指定代次**，否则 400
（`GENERATION_REQUIRED`）；未分代件号给 `generation` 参数同样 400
（`GENERATION_NOT_SPLIT`）。

```bash
curl -s 'localhost:8000/parts/BRG-0001?generation=1' | jq   # 旧件
curl -s 'localhost:8000/parts/BRG-0001?generation=2' | jq   # 新件
```

响应中每个装车段与在役段都带 `generation` 字段（1 / 2，未分代为 `null`）；
`GET /cars/{car_no}/state` 的在役件同样标注代次，避免同号两件位置混淆。

### 补录 / 更正后重新核对

批次补录或工单更正提交后，系统用**重建后的装车段**对全部分代登记重新核对：

- 通过：按新分界重新给段打代次，查询按新分界取数（分界工单仅时刻被更正时，
  登记中的时刻快照随事务同步刷新）；
- 不通过（指定的装卸事件消失、装卸角色不再匹配、时刻先后倒置、段无法唯一归属）：
  **整批工单退回**，错误体指出相关工单号 / 段，查询保留原结果。

分代登记与批次提交共用同一把写锁；交错提交时按"将要生效"的工单集合与分代登记
合并判定，每个写操作要么完整生效要么完整退回。

### 分代相关错误码

| 代码 | 含义 |
|---|---|
| `GENERATION_ORDER_NOT_FOUND` | 指定的卸车 / 装车工单不在有效工单中 |
| `GENERATION_ORDER_NOT_MATCHING` | 工单不包含该件号对应的卸车（或装车）事件 |
| `GENERATION_ORDER_INVERTED` | 旧件末卸时刻晚于新件首装时刻，先后倒置 |
| `GENERATION_SEGMENT_AMBIGUOUS` | 存在无法唯一归入任一代的装车段（间隔 / 跨分界 / 在役不匹配） |
| `GENERATION_CONFLICT` | 同件号已登记另一对工单 |
| `GENERATION_REQUIRED` / `GENERATION_NOT_SPLIT` | 查询时代次参数缺失 / 多余（HTTP 400） |

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
- **分代登记是另一类事实数据**：`part_generations` 每件号至多一条、不可改写
  （换一对工单即冲突）。`segments.generation` 是派生标签，随每次批次提交或
  登记提交重新生成；登记与批次共用写锁，并在同一事务内与派生段一起生效。
- **批次与分代的联动**：批次落库前先重建，再用重建段重新核对全部分代登记，
  任一登记不再成立则整批退回；因此"指定装卸消失 / 倒置 / 段无法唯一归属"
  都不可能进入已提交状态，查询结果始终自洽。
- **事务与并发**：批次校验与落库由进程内写锁串行化；落库在单个
  `BEGIN IMMEDIATE … COMMIT` 事务内完成；SQLite WAL 模式下查询只读最近完整提交的
  快照——提交交错时也不会读到半成品批次。
- **时刻处理**：统一归一为 UTC 微秒精度 ISO 字符串，字典序即时间序；同一时刻的
  工单成组整体判定。

## 已知限制

- 全量重建派生表，工单量极大时可改为增量重放（当前规模下简单可靠）。
- 里程为整数公里；件号 / 车号 / 轴位按字符串精确匹配。
- 分代模型固定为一代（旧件）+ 二代（新件）；同一铭牌号若出现三件及以上实物，
  需扩展登记模型（当前需求只涉及两件）。
- 分代登记不提供删除 / 改写接口；如需废止，只能在数据治理流程中清库重建。
