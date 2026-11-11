"""FastAPI 入口：主数据快照、归属修订 API 路由与应用装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .database import init_db
from .errors import AppError
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    RevisionAuditOut,
    RevisionOut,
    RevisionSubmitRequest,
    SegmentsQueryRequest,
    SnapshotEntriesQueryRequest,
    SnapshotEntryOut,
    SnapshotOut,
    SnapshotPublishRequest,
)
from .service import (
    attribute_hours,
    get_revision_audit,
    get_snapshot,
    list_revisions,
    list_segments,
    list_snapshot_entries,
    list_snapshots,
    publish_snapshot,
    submit_revision,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时确保表结构存在（幂等，含旧版库迁移）。
    init_db()
    yield


app = FastAPI(
    title="工时归属修订与成本中心快照 API",
    description=(
        "财务数仓场景下的人事补录归属服务，主数据口径冻结、历史修订不可变。\n\n"
        "- **主数据快照**：成本中心及生效起止日期按批发布，生成不可变递增版本；\n"
        "- **带版本提交**：归属批次必须指定快照版本，批内每个区间都要被该版本中"
        "同成本中心有效期的并集完整覆盖，否则整批拒绝；\n"
        "- **口径冻结**：修订记录保存所用快照版本，后续主数据发布不改变旧修订查询；\n"
        "- **原子并发**：发布与提交均在单事务内绑定完整单一版本，无半版快照、无半批。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                **({"details": exc.details} if exc.details else {}),
            }
        },
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 主数据快照
# ---------------------------------------------------------------------------

@app.post(
    "/api/snapshots",
    response_model=SnapshotOut,
    status_code=201,
    tags=["主数据快照"],
    summary="发布一版成本中心主数据快照（不可变版本）",
    description=(
        "接收成本中心及生效起止日期的主数据快照，整批写入后返回递增快照版本号。\n\n"
        "- 日期区间为左闭右开 `[effective_from, effective_to)`，起日必须严格早于止日；\n"
        "- 同一成本中心可包含多段有效期（相邻/重叠允许），覆盖判断按并集进行；\n"
        "- 版本号分配与全量写入在单个 `BEGIN IMMEDIATE` 事务内完成，"
        "并发发布串行化，每个版本要么完整可见、要么不存在；\n"
        "- 已发布版本只追加、不可修改，后续发布不影响引用旧版本的修订。"
    ),
)
def create_snapshot(req: SnapshotPublishRequest) -> SnapshotOut:
    return SnapshotOut(**publish_snapshot(req))


@app.get(
    "/api/snapshots",
    response_model=list[SnapshotOut],
    tags=["主数据快照"],
    summary="列出全部快照版本",
)
def get_snapshots() -> list[SnapshotOut]:
    return [SnapshotOut(**row) for row in list_snapshots()]


@app.get(
    "/api/snapshots/{snapshot_version_id}",
    response_model=SnapshotOut,
    tags=["主数据快照"],
    summary="查询单个快照版本头信息",
)
def get_one_snapshot(snapshot_version_id: int) -> SnapshotOut:
    return SnapshotOut(**get_snapshot(snapshot_version_id))


@app.post(
    "/api/snapshots/{snapshot_version_id}/entries",
    response_model=list[SnapshotEntryOut],
    tags=["主数据快照"],
    summary="审计：查看某快照版本内的成本中心有效期",
)
def get_snapshot_entries(
    snapshot_version_id: int, req: SnapshotEntriesQueryRequest | None = None
) -> list[SnapshotEntryOut]:
    cost_center = req.cost_center if req else None
    return [SnapshotEntryOut(**row) for row in
            list_snapshot_entries(snapshot_version_id, cost_center)]


# ---------------------------------------------------------------------------
# 归属修订
# ---------------------------------------------------------------------------

@app.post(
    "/api/revisions",
    response_model=RevisionOut,
    status_code=201,
    tags=["归属修订"],
    summary="绑定快照版本，提交一批员工-成本中心归属区间",
    description=(
        "整批校验通过后写入，返回递增修订号，并持久化所用快照版本。\n\n"
        "拒绝条件（整批回滚，数据与修订号均不变）：\n"
        "1. 起日不早于止日（`effective_from >= effective_to`）；\n"
        "2. 批内同一员工的区间相互重叠（首尾相接不算重叠）；\n"
        "3. `snapshot_version_id` 指向不存在的快照（404）；\n"
        "4. 批内任一区间未被该快照版本中**同成本中心**有效期的并集完整覆盖"
        "（成本中心缺失返回 422，覆盖不足返回 400）。"
    ),
)
def create_revision(req: RevisionSubmitRequest) -> RevisionOut:
    return RevisionOut(**submit_revision(req))


@app.get(
    "/api/revisions",
    response_model=list[RevisionOut],
    tags=["归属修订"],
    summary="列出全部修订号（含绑定的快照版本）",
)
def get_revisions() -> list[RevisionOut]:
    return [RevisionOut(**row) for row in list_revisions()]


@app.get(
    "/api/revisions/{revision_id}",
    response_model=RevisionAuditOut,
    tags=["归属修订"],
    summary="审计：查询某修订详情（快照版本 + 原始区间）",
)
def get_revision(revision_id: int) -> RevisionAuditOut:
    return RevisionAuditOut(**get_revision_audit(revision_id))


@app.post(
    "/api/revisions/{revision_id}/segments",
    tags=["归属修订"],
    summary="审计：查看某个修订号下提交的原始区间",
)
def get_revision_segments(revision_id: int, req: SegmentsQueryRequest | None = None) -> list[dict]:
    employee_id = req.employee_id if req else None
    return list_segments(revision_id, employee_id)


@app.post(
    "/api/attribution/query",
    response_model=list[AttributionResult],
    tags=["归属查询"],
    summary="按指定修订号的口径逐条查询工时归属",
    description=(
        "对每条工时记录返回成本中心或未归属。只可见 `revision_id` 及之前的补录；"
        "同日期点存在多个历史区间时取修订号最大者。`revision_id=0` 表示空口径。"
        "修订绑定的快照版本不可变，后续快照发布不影响本查询结果。"
    ),
)
def query_attribution(req: AttributionQueryRequest) -> list[AttributionResult]:
    return attribute_hours(req)
