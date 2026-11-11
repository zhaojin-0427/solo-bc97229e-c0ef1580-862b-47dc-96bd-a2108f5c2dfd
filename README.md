# 工时归属修订与成本中心快照 API（FastAPI + SQLite）

财务数仓场景：人事补录不得把工时写入**已失效的成本中心**，而月结时的口径又必须冻结。
本服务在「递增修订号 + 只追加（append-only）存储」之上引入**不可变主数据快照**：

- **快照发布**：接收成本中心及生效起止日期的主数据快照，整批发布为一个不可变、带递增版本号的快照；
- **带版本提交**：归属批次必须指定一个快照版本，批内每个区间都必须被该版本中**同成本中心**有效期的并集**完整覆盖**，否则整批拒绝；
- **口径冻结**：修订记录持久化所用快照版本，后续主数据发布不改变任何旧修订的查询结果；
- **原子并发**：快照发布与修订提交都在单个 `BEGIN IMMEDIATE` 事务内绑定完整单一版本，不存在半版快照或半批归属；
- 日期区间统一为**左闭右开** `[effective_from, effective_to)`，允许空档。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常处理
│   ├── service.py     # 快照发布、覆盖校验、修订提交、归属查询（核心业务规则）
│   ├── database.py    # SQLite 连接、WAL、建表、旧版库迁移
│   ├── schemas.py     # Pydantic 请求/响应模型（含 Swagger 示例）
│   └── errors.py      # 业务错误类型
├── tests/test_api.py  # 端到端测试（pytest + TestClient，含并发与迁移用例）
├── scripts/demo.sh    # 命令行端到端演示
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 一、Docker Compose 启动（交付方式）

```bash
docker compose up -d --build
```

- 服务地址：<http://localhost:8000>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>（各接口均内置示例请求体，可直接 `Try it out`）
- ReDoc 文档：<http://localhost:8000/redoc>
- SQLite 数据文件位于命名卷 `attribution-data`（容器内 `/data/attribution.db`），容器重建后快照与修订历史保留。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷（清空所有快照与修订）
```

## 二、通过交互式文档演示

打开 <http://localhost:8000/docs>，按以下顺序操作：

1. **POST `/api/snapshots`** 发布主数据快照 → 返回 `snapshot_version_id: 1`
2. **POST `/api/revisions`** 带上 `snapshot_version_id: 1` 提交合法归属批次 → 返回 `revision_id: 1`
3. 再次 POST `/api/revisions`，引用一个在快照中有效期已结束的成本中心 → 返回 `400 SNAPSHOT_COVERAGE_VIOLATION`，整批拒绝、修订号不被消耗
4. POST `/api/snapshots` 发布版本 2（在其中注销该成本中心）→ 返回 `snapshot_version_id: 2`
5. **GET `/api/revisions/1`** 审计旧修订 → 仍绑定快照版本 1；用 `revision_id: 1` 调 **POST `/api/attribution/query`** → 结果与版本 2 发布前完全一致
6. 用 `snapshot_version_id: 2` 再提已注销的成本中心 → 返回 `422 COST_CENTER_NOT_IN_SNAPSHOT`

等价的命令行演示（含全部拒绝场景与审计接口）：

```bash
./scripts/demo.sh
```

## 三、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/snapshots` | 发布一版成本中心主数据快照，返回不可变版本号（201） |
| GET | `/api/snapshots` | 列出全部快照版本 |
| GET | `/api/snapshots/{id}` | 查询单个快照版本头信息 |
| POST | `/api/snapshots/{id}/entries` | 查看某快照版本的成本中心有效期（可按成本中心过滤，审计） |
| POST | `/api/revisions` | 绑定快照版本提交归属批次，成功返回递增修订号（201） |
| GET | `/api/revisions` | 列出全部修订（含绑定的快照版本） |
| GET | `/api/revisions/{id}` | 修订审计详情：修订头（含快照版本）+ 原始区间 |
| POST | `/api/revisions/{id}/segments` | 查看某修订提交的原始区间（可按员工过滤） |
| POST | `/api/attribution/query` | 按指定修订号逐条返回工时成本中心 / 未归属 |

### 请求示例

发布快照：

```json
POST /api/snapshots
{
  "note": "2026 年度成本中心主数据 v1",
  "entries": [
    {"cost_center": "CC-SH-01", "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-GZ-02", "effective_from": "2026-04-01", "effective_to": "2027-01-01"}
  ]
}
```

带版本提交修订：

```json
POST /api/revisions
{
  "snapshot_version_id": 1,
  "note": "HR 调岗补录 2026-11",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-GZ-02",
     "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
  ]
}
```

归属查询与响应：

```json
POST /api/attribution/query
{
  "revision_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8}
  ]
}
```

```json
[
  {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8.0,
   "revision_id": 2, "cost_center": "CC-GZ-02",
   "attributed": true, "matched_revision_id": 2},
  {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8.0,
   "revision_id": 2, "cost_center": "CC-SH-01",
   "attributed": true, "matched_revision_id": 1}
]
```

未归属时 `cost_center` 为 `null`、`attributed` 为 `false`。`revision_id=0` 表示空口径。

## 四、核心设计

### 1. 快照为什么不可变

`snapshots` 与 `snapshot_entries` 只追加、从不 UPDATE/DELETE。发布即在单个
`BEGIN IMMEDIATE` 事务内分配版本号并写入全部有效期：SQLite 写锁把并发发布串行化，
每个版本要么整版可见，要么不存在。修订通过 `revisions.snapshot_version_id`
外键绑定一个确定版本，读到的主数据口径永久固定。

### 2. 覆盖校验如何阻止失效成本中心

事务内取出该快照版本下每个成本中心的全部有效期，合并为互不重叠的并集段
（首尾相接自动合并），再对批内每条归属区间判断：

- 成本中心在快照中完全不存在 → `COST_CENTER_NOT_IN_SNAPSHOT`（422）；
- 找不到包含区间起点的并集段，或该段右端早于区间止日 →
  `SNAPSHOT_COVERAGE_VIOLATION`（400），错误详情给出 `covered_until` / `required_until`；
- 覆盖判断严格按**同一成本中心**进行，其他成本中心的有效期不能兜底；
- 权威校验在写锁事务内执行，任何校验失败都在写入前回滚，修订号不被消耗。

### 3. 旧口径为什么不会变

1. **只追加，不改写历史**。快照、修订、区间均无 UPDATE/DELETE；
2. **查询带时间旅行条件**。修订 N 的查询只扫描 `revision_id <= N` 的区间，
   同日期点取修订号最大者，后续修订与后续快照天然不可见：

   ```sql
   WHERE employee_id = :emp
     AND effective_from <= :work_date
     AND effective_to   > :work_date   -- 左闭右开
     AND revision_id <= :query_revision
   ORDER BY revision_id DESC, id DESC
   LIMIT 1
   ```

3. **整批原子性**。静态校验先行快速失败；版本号/修订号分配与全量插入在同一
   `BEGIN IMMEDIATE` 事务内，失败即回滚，自增号不会被失败批次消耗。

### 4. 并发：完整单一版本、无半批

- 任意时刻只有一个写事务持有 SQLite 写锁，并发发布/提交自动排队；
- 修订在同一事务内「确认快照存在 → 读取完整快照 → 覆盖校验 → 写入修订与全部区间」，
  快照内容不可变，因此不存在读到半版快照的可能；
- 事务提交前其他请求看不到任何部分写入，回滚后库内无残留。

### 5. 旧版数据库兼容

`init_db()` 检测到只有旧版 `revisions` 表的库时，会自动创建快照结构，
用历史区间中出现过的成本中心构造引导快照（版本 1，宽有效期），
并把旧修订回填绑定到该版本；迁移只补结构、不改业务数据，旧查询结果保持不变。

## 五、本地开发与测试（不用 Docker）

```bash
pip install -r requirements-dev.txt
pytest -q
ATTRIBUTION_DB_PATH=./data/dev.db uvicorn app.main:app --reload
```

`ATTRIBUTION_DB_PATH` 可指定 SQLite 文件位置，默认 `./data/attribution.db`。
测试套件共 20 个用例，覆盖快照发布、覆盖规则各边界、口径冻结、审计、
8 线程并发发布/提交以及旧库迁移。

## 六、错误响应格式

```json
{
  "error": {
    "code": "SNAPSHOT_COVERAGE_VIOLATION",
    "message": "第 1 条归属区间 [2026-04-15, 2026-05-01) 未被快照版本 1 中成本中心 CC-DEAD 的有效期完整覆盖（并集在 2026-04-01 出现空档/失效）",
    "details": {
      "index": 0,
      "employee_id": "E001",
      "cost_center": "CC-DEAD",
      "snapshot_version_id": 1,
      "segment": ["2026-04-15", "2026-05-01"],
      "covered_until": "2026-04-01",
      "required_until": "2026-05-01"
    }
  }
}
```

| code | HTTP | 触发条件 |
| --- | --- | --- |
| `INVALID_DATE_RANGE` | 400 | 起日不早于止日（快照与批次通用） |
| `OVERLAPPING_SEGMENTS` | 400 | 批内同一员工区间重叠 |
| `SNAPSHOT_COVERAGE_VIOLATION` | 400 | 区间未被同成本中心有效期并集完整覆盖（含失效/空档/溢出） |
| `COST_CENTER_NOT_IN_SNAPSHOT` | 422 | 成本中心在指定快照版本中不存在 |
| `SNAPSHOT_NOT_FOUND` | 404 | 引用了不存在的快照版本 |
| `REVISION_NOT_FOUND` | 404 | 查询/审计了不存在的修订号 |
| Pydantic 校验错误 | 422 | 空批次、字段缺失、日期格式错误等 |
