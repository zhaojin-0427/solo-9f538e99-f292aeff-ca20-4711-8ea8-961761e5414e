# 空运危险品保管链 API

面向断网后乱序补传场景的保管链（chain of custody）服务。数据员可以据此判断：
在某个时刻之前，哪些交接仍然可信、断链发生在哪里、当前可确认的保管方是谁；
货站还可以按**同一起始路线**统计走过该路线的运单后来分别去了哪里。

技术栈：**FastAPI + SQLite（WAL 模式）+ Uvicorn**，可通过 `docker compose` 一键启动。

## 核心规则

1. **固定起始保管方**：创建运单时登记 `origin_party`，初始可确认保管方即该方。
2. **批量提交**：每笔扫描携带 `scan_id`、正整数 `revision`、UTC 时刻 `ts_utc`、
   交出方 `giver`、接收方 `receiver`。时刻必须带时区偏移（如 `...Z`、`+08:00`），统一归一化为 UTC 存储。
3. **撤回**：只提交 `scan_id` 与更高的 `revision`，`event_type=WITHDRAWAL`，
   不得携带时刻或当事方；撤回只能针对已存在的扫描 ID。
4. **修订裁决**：
   - 同一 `scan_id` 仅采用**最高修订号**的版本；更低修订号提交返回 `409 stale`。
   - 同修订号、异文（时刻/交出方/接收方/类型不同）**整批拒绝**（`409`），已提交数据原样保留。
   - 同修订号、同文重复提交幂等，不计入写入数。
5. **链核对**（按 UTC 时刻升序，乱序补传自动归位）：
   - 交接的 `giver` 必须等于当前保管方（首笔对照起始保管方，其后对照上一笔的 `receiver`），
     不匹配即为 **GAP 断链**。
   - **同一时刻存在多笔交接时不猜测顺序**：该时刻整组标记为 **SIMULTANEOUS** 断链，组内全部未证实。
   - 断链之后的所有扫描一律 `verified=false`，直到补报、更正或撤回补齐缺口；
     重算后断链位置可以前移、后移或消失。
6. **原子性与一致性**：
   - 每个批次在单个 `BEGIN IMMEDIATE` 事务内完成，任何冲突整批回滚，**失败保留旧链**。
   - 查询只读已提交快照（WAL），交错的扫描提交与查询永远看不到半写状态或混合修订。
   - 数据持久化在 SQLite 文件中，进程重启后状态不变。

## 路线去向汇总（route summary）

货站想知道：走过同一段路线的运单，后来分别去了哪里。

- 在 `/docs` 调用 `POST /api/route-summary`，输入**从起始保管方开始的有序名单**
  （`parties`，允许包含重复保管方）。仅统计**可信保管链以这段路线开头**的运单。
- 响应按下一保管方分组返回运单数（`next_custodians`），并单独给出
  `no_further_scan_count`：在该路线终点处**无后续可信交接**的运单数——
  包括之后再无扫描，以及紧接着发生 GAP / SIMULTANEOUS 断链（断链后的扫描不计为去向）。
- 名单可含重复保管方，顺序有意义；每张单在一次查询中只计入一个分组。
- 查询只读取**同一份已提交的持久化汇总**（一次 `GROUP BY`），不逐单查链。

### 持久化汇总与旧卷补建

- 每张运单的可信链被物化为其**每一个路线前缀**的贡献行，存于
  `route_prefix_contribs`；`waybill_chain_versions` 记录每单的链版本。
- **沿用旧 SQLite 卷**：启动时若发现旧卷缺少汇总表，会在**后台线程**为已有运单
  补建汇总；补建期间建单和录入扫描照常可用（在线写入在同一事务内就地维护汇总）。
- 补建完成前查询返回 `503`（`status=BUILDING`），可用
  `GET /api/route-summary/status` 查看待补建运单数；**首次可查时必然计入补建期间
  已提交的全部变动**。
- 此后建单，或补报、更正、撤回扫描，只在该写事务内**重算本单**在各段路线前缀中的
  贡献，不影响其他运单。补报若让可信路线提前断开，原去向与更深前缀中的贡献会被一并扣除。
- 扫描/运单与其计数在**同一事务**生效，失败回滚保留旧结果；汇总已建成（READY）后
  重启可直接查询，无需重建。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs
# 控制台：http://localhost:8000/
```

数据库保存在命名卷 `chain-data`（容器内 `/data/chain.db`），容器重启不丢数据。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：APP_DB_PATH=/custom/path/chain.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 创建运单并固定起始保管方 |
| GET | `/api/waybills` | 列出全部运单 |
| POST | `/api/waybills/{waybill_id}/scans/batch` | 批量提交交接 / 更正 / 撤回 |
| GET | `/api/waybills/{waybill_id}/chain` | 查询可信前缀、断链原因、当前保管方 |
| POST | `/api/route-summary` | 按起始路线统计下一保管方分组与无后续扫描数（补建中返回 503） |
| GET | `/api/route-summary/status` | 汇总补建状态（BUILDING / READY）与待补建运单数 |
| GET | `/docs` | Swagger UI，可直接录入与查询 |

### 路线汇总请求 / 响应示例

```json
// POST /api/route-summary
{"parties": ["HKG-WAREHOUSE", "CX-CARGO"]}
```

```json
{
  "ready": true,
  "status": "READY",
  "route": ["HKG-WAREHOUSE", "CX-CARGO"],
  "matched_waybills": 12,
  "next_custodians": [
    {"next_party": "PVG-WAREHOUSE", "waybill_count": 7},
    {"next_party": "SIN-HUB", "waybill_count": 3}
  ],
  "no_further_scan_count": 2
}
```

补建未完成时返回 HTTP `503`：

```json
{"ready": false, "status": "BUILDING",
 "error": "summary_not_ready",
 "detail": "route summary not ready yet: status=BUILDING, pending_waybills=150"}
```

### 交接批次示例

```json
{
  "scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z",
     "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T10:30:00Z",
     "giver": "CX-CARGO", "receiver": "PVG-WAREHOUSE"}
  ]
}
```

### 撤回示例（修订号必须更高）

```json
{"scans": [{"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL"}]}
```

### 查询响应示例（发生断链时）

```json
{
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "current_custodian": "CX-CARGO",
  "verified_prefix": [ {"scan_id": "S1", "verified": true, "...": "..."} ],
  "unverified_scans": [ {"scan_id": "S3", "verified": false,
                         "unverified_reason": "GAP"} ],
  "chain_intact": false,
  "break_reason": "GAP",
  "break_detail": "giver 'X' does not hold custody; expected 'CX-CARGO' ...",
  "break_scan_ids": ["S3"]
}
```

`break_reason` 取值：`NONE` / `GAP`（保管方不承接）/ `SIMULTANEOUS`（同时刻多笔，顺序不可判定）。
`current_custodian` 只沿已证实交接推进，因此断链后仍可信地停留在最后一个已证实接收方。

## 测试

```bash
pip install pytest httpx
pytest -q
```

共 42 个用例，覆盖：乱序补传归位、时区归一化、最高修订裁决、同修订异文整批回滚、
撤回与补报修复断链、GAP/SIMULTANEOUS 检测、输入校验、真实线程下的写串行化、
交错读不到混合修订、进程重启持久化；以及路线汇总的下一保管方分组、
无后续扫描/即断链计数、重复保管方路线、补报/更正/撤回只更新本单前缀贡献、
失败批次保留旧计数、旧卷后台补建（BUILDING→503→READY）、补建期间写入在首次
可查时计入、READY 卷重启可直接查询、汇总与逐单重算结果一致。
