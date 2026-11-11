"""核心业务逻辑：快照发布、批次校验、修订提交、按修订口径归属查询。

并发与原子性：
- 快照发布与修订提交都在单个 ``BEGIN IMMEDIATE`` 事务内完成版本号分配与
  全量数据写入，SQLite 写锁将并发写者完全串行化；
- 修订提交在同一事务内读取并锁定所绑定的快照版本，快照要么整版可见、
  要么不存在，不存在「半版快照」或「半批归属」。
"""
from __future__ import annotations

from collections import defaultdict
from sqlite3 import Connection

from .database import get_conn
from .errors import AppError
from .schemas import (
    AttributionQueryRequest,
    AttributionResult,
    CostCenterValidityIn,
    RevisionSubmitRequest,
    SegmentIn,
    SnapshotPublishRequest,
)


# ---------------------------------------------------------------------------
# 纯函数校验（不触碰数据库，便于单测与快速失败）
# ---------------------------------------------------------------------------

def _validate_date_ranges(
    entries: list[SegmentIn] | list[CostCenterValidityIn],
    kind: str,
) -> None:
    """起日必须严格早于止日（左闭右开 [from, to)）。"""
    for idx, e in enumerate(entries):
        if e.effective_from >= e.effective_to:
            raise AppError(
                code="INVALID_DATE_RANGE",
                message=(
                    f"第 {idx + 1} 条{kind}起日 {e.effective_from.isoformat()} "
                    f"不早于止日 {e.effective_to.isoformat()}，左闭右开区间要求起日严格早于止日"
                ),
                details={
                    "index": idx,
                    "effective_from": e.effective_from.isoformat(),
                    "effective_to": e.effective_to.isoformat(),
                },
            )


def validate_batch(entries: list[SegmentIn]) -> None:
    """归属批次写入前的全部静态校验。任何一条不通过即抛 AppError，不触碰数据库。

    规则：
      1. 起日必须严格早于止日（左闭右开 [from, to)）；
      2. 同一员工在本批内的区间不得重叠（首尾相接允许：a.to == b.from 不重叠）。
    """
    _validate_date_ranges(entries, "归属区间")

    # 批内同员工区间重叠检查（半开区间重叠 <=> a.from < b.to and b.from < a.to）
    by_employee: dict[str, list[tuple[int, SegmentIn]]] = defaultdict(list)
    for idx, e in enumerate(entries):
        by_employee[e.employee_id].append((idx, e))

    for employee_id, items in by_employee.items():
        ordered = sorted(items, key=lambda pair: (pair[1].effective_from, pair[1].effective_to))
        for (i_a, a), (i_b, b) in zip(ordered, ordered[1:]):
            if a.effective_from < b.effective_to and b.effective_from < a.effective_to:
                raise AppError(
                    code="OVERLAPPING_SEGMENTS",
                    message=(
                        f"员工 {employee_id} 在本批存在重叠区间："
                        f"第 {i_a + 1} 条 [{a.effective_from.isoformat()}, {a.effective_to.isoformat()}) "
                        f"与第 {i_b + 1} 条 [{b.effective_from.isoformat()}, {b.effective_to.isoformat()})"
                    ),
                    details={
                        "employee_id": employee_id,
                        "indices": [i_a, i_b],
                    },
                )


def validate_snapshot(entries: list[CostCenterValidityIn]) -> None:
    """快照主数据的静态校验：仅要求左闭右开（from < to）。

    同一成本中心允许多段、相邻或重叠——归属批次按各段并集判断覆盖。
    """
    _validate_date_ranges(entries, "成本中心有效期")


def _merge_intervals(
    intervals: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """合并若干左闭右开 ISO 日期区间，返回互不重叠、按起日排序的并集。

    首尾相接（a.to == b.from）合并为一段，因此覆盖判断只关注真实空档。
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[str, str]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # 重叠或首尾相接
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def validate_coverage(
    entries: list[SegmentIn],
    snapshot_rows: list,
    snapshot_version_id: int,
) -> None:
    """批内每条区间必须被快照中同成本中心有效期的并集完整覆盖。

    ``snapshot_rows`` 为该快照版本下的全部成本中心有效期行（含
    cost_center / effective_from / effective_to，ISO 字符串）。
    任何一条不满足即抛 AppError，整批拒绝。
    """
    by_cc: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in snapshot_rows:
        by_cc[row["cost_center"]].append((row["effective_from"], row["effective_to"]))
    coverage = {cc: _merge_intervals(ivs) for cc, ivs in by_cc.items()}

    for idx, e in enumerate(entries):
        union = coverage.get(e.cost_center)
        if not union:
            raise AppError(
                code="COST_CENTER_NOT_IN_SNAPSHOT",
                message=(
                    f"第 {idx + 1} 条归属区间引用的成本中心 {e.cost_center} "
                    f"在快照版本 {snapshot_version_id} 中不存在，禁止写入"
                ),
                status_code=422,
                details={
                    "index": idx,
                    "cost_center": e.cost_center,
                    "snapshot_version_id": snapshot_version_id,
                },
            )

        seg_from, seg_to = e.effective_from.isoformat(), e.effective_to.isoformat()
        # 在并集段中定位包含 seg_from 的段，再验证其右端不早于 seg_to。
        covering = next(
            (iv for iv in union if iv[0] <= seg_from and iv[1] > seg_from),
            None,
        )
        if covering is None or covering[1] < seg_to:
            detail = None if covering is None else {
                "covered_until": covering[1],
                "required_until": seg_to,
            }
            raise AppError(
                code="SNAPSHOT_COVERAGE_VIOLATION",
                message=(
                    f"第 {idx + 1} 条归属区间 [{seg_from}, {seg_to}) 未被快照版本 "
                    f"{snapshot_version_id} 中成本中心 {e.cost_center} 的有效期完整覆盖"
                    + ("（并集在 " + covering[1] + " 出现空档/失效）" if covering else "")
                ),
                details={
                    "index": idx,
                    "employee_id": e.employee_id,
                    "cost_center": e.cost_center,
                    "snapshot_version_id": snapshot_version_id,
                    "segment": [seg_from, seg_to],
                    **(detail or {}),
                },
            )


# ---------------------------------------------------------------------------
# 快照发布（不可变版本）
# ---------------------------------------------------------------------------

def publish_snapshot(req: SnapshotPublishRequest) -> dict:
    """发布一版成本中心主数据快照，返回递增快照版本号。

    版本号分配与全部有效期写入在同一个 BEGIN IMMEDIATE 事务内完成：
    并发发布被 SQLite 写锁串行化，每个版本要么完整可见，要么不存在。
    """
    validate_snapshot(req.entries)

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO snapshots (entry_count, note) VALUES (?, ?)",
            (len(req.entries), req.note),
        )
        version_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO snapshot_entries
                (snapshot_version_id, cost_center, effective_from, effective_to)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    version_id,
                    e.cost_center,
                    e.effective_from.isoformat(),
                    e.effective_to.isoformat(),
                )
                for e in req.entries
            ],
        )
        row = conn.execute(
            "SELECT snapshot_version_id, entry_count, note, created_at "
            "FROM snapshots WHERE snapshot_version_id = ?",
            (version_id,),
        ).fetchone()
    return dict(row)


def list_snapshots() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT snapshot_version_id, entry_count, note, created_at "
            "FROM snapshots ORDER BY snapshot_version_id"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_snapshot_or_404(conn: Connection, version_id: int):
    row = conn.execute(
        "SELECT snapshot_version_id, entry_count, note, created_at "
        "FROM snapshots WHERE snapshot_version_id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise AppError(
            code="SNAPSHOT_NOT_FOUND",
            message=f"快照版本 {version_id} 不存在",
            status_code=404,
            details={"snapshot_version_id": version_id},
        )
    return row


def get_snapshot(version_id: int) -> dict:
    with get_conn() as conn:
        return dict(_get_snapshot_or_404(conn, version_id))


def list_snapshot_entries(version_id: int, cost_center: str | None) -> list[dict]:
    with get_conn() as conn:
        _get_snapshot_or_404(conn, version_id)
        if cost_center:
            rows = conn.execute(
                """
                SELECT snapshot_version_id, cost_center, effective_from, effective_to
                FROM snapshot_entries
                WHERE snapshot_version_id = ? AND cost_center = ?
                ORDER BY cost_center, effective_from
                """,
                (version_id, cost_center.strip()),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT snapshot_version_id, cost_center, effective_from, effective_to
                FROM snapshot_entries
                WHERE snapshot_version_id = ?
                ORDER BY cost_center, effective_from
                """,
                (version_id,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 归属修订提交（绑定单一快照版本）
# ---------------------------------------------------------------------------

def submit_revision(req: RevisionSubmitRequest) -> dict:
    """提交一批归属区间，成功返回新的递增修订号。

    流程（单事务、全有或全无）：
      1. 事务外完成静态校验（日期、批内重叠），快速失败且不消耗修订号；
      2. ``BEGIN IMMEDIATE`` 取写锁，并发提交/发布在此串行；
      3. 事务内确认快照版本存在并读取其完整内容，执行覆盖校验——
         快照内容只追加不可变，读到的必然是完整单一版本；
      4. 写入修订头（记录 snapshot_version_id）与全部区间；任一步失败即回滚。
    """
    validate_batch(req.entries)

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _get_snapshot_or_404(conn, req.snapshot_version_id)
        snapshot_rows = conn.execute(
            """
            SELECT cost_center, effective_from, effective_to
            FROM snapshot_entries
            WHERE snapshot_version_id = ?
            """,
            (req.snapshot_version_id,),
        ).fetchall()
        # 权威覆盖校验放在写锁事务内：并发发布新版本不会改变已发布版本的内容。
        validate_coverage(req.entries, snapshot_rows, req.snapshot_version_id)

        cur = conn.execute(
            "INSERT INTO revisions (entry_count, snapshot_version_id, note) VALUES (?, ?, ?)",
            (len(req.entries), req.snapshot_version_id, req.note),
        )
        revision_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO attribution_segments
                (revision_id, employee_id, cost_center, effective_from, effective_to)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    revision_id,
                    e.employee_id,
                    e.cost_center,
                    e.effective_from.isoformat(),
                    e.effective_to.isoformat(),
                )
                for e in req.entries
            ],
        )
        row = conn.execute(
            "SELECT revision_id, entry_count, snapshot_version_id, note, created_at "
            "FROM revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# 归属查询（时间旅行，旧口径不受后续快照/修订影响）
# ---------------------------------------------------------------------------

def _lookup_one(
    conn: Connection, revision_id: int, employee_id: str, work_date_iso: str
) -> tuple[str | None, int | None]:
    """在 revision_id 口径下定位单条工时的成本中心。

    仅可见 revision_id <= 给定修订号 的区间；同日期点命中多个历史区间时，
    取修订号最大者（后补录覆盖先补录），区间外/空档则未归属。
    """
    row = conn.execute(
        """
        SELECT cost_center, revision_id AS matched_revision_id
        FROM attribution_segments
        WHERE employee_id = ?
          AND effective_from <= ?
          AND effective_to   > ?
          AND revision_id <= ?
        ORDER BY revision_id DESC, id DESC
        LIMIT 1
        """,
        (employee_id, work_date_iso, work_date_iso, revision_id),
    ).fetchone()
    if row is None:
        return None, None
    return row["cost_center"], row["matched_revision_id"]


def attribute_hours(req: AttributionQueryRequest) -> list[AttributionResult]:
    """逐条返回工时记录在指定修订口径下的成本中心（或未归属）。"""
    with get_conn() as conn:
        max_row = conn.execute("SELECT COALESCE(MAX(revision_id), 0) AS r FROM revisions").fetchone()
        if req.revision_id > max_row["r"]:
            raise AppError(
                code="REVISION_NOT_FOUND",
                message=f"修订号 {req.revision_id} 不存在，当前最大修订号为 {max_row['r']}",
                status_code=404,
            )

        results: list[AttributionResult] = []
        for rec in req.records:
            cost_center, matched = _lookup_one(
                conn, req.revision_id, rec.employee_id, rec.work_date.isoformat()
            )
            results.append(
                AttributionResult(
                    employee_id=rec.employee_id,
                    work_date=rec.work_date,
                    hours=rec.hours,
                    revision_id=req.revision_id,
                    cost_center=cost_center,
                    attributed=cost_center is not None,
                    matched_revision_id=matched,
                )
            )
    return results


# ---------------------------------------------------------------------------
# 审计查询
# ---------------------------------------------------------------------------

def list_revisions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT revision_id, entry_count, snapshot_version_id, note, created_at "
            "FROM revisions ORDER BY revision_id"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_revision_or_404(conn: Connection, revision_id: int):
    row = conn.execute(
        "SELECT revision_id, entry_count, snapshot_version_id, note, created_at "
        "FROM revisions WHERE revision_id = ?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise AppError(
            code="REVISION_NOT_FOUND",
            message=f"修订号 {revision_id} 不存在",
            status_code=404,
        )
    return row


def get_revision_audit(revision_id: int) -> dict:
    """修订审计详情：修订头（含所用快照版本）+ 原始区间，一次查全。"""
    with get_conn() as conn:
        head = dict(_get_revision_or_404(conn, revision_id))
        segments = [
            dict(r)
            for r in conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id,),
            ).fetchall()
        ]
    return {"revision": head, "segments": segments}


def list_segments(revision_id: int, employee_id: str | None) -> list[dict]:
    with get_conn() as conn:
        _get_revision_or_404(conn, revision_id)
        if employee_id:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ? AND employee_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id, employee_id.strip()),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT revision_id, employee_id, cost_center, effective_from, effective_to
                FROM attribution_segments
                WHERE revision_id = ?
                ORDER BY employee_id, effective_from
                """,
                (revision_id,),
            ).fetchall()
    return [dict(r) for r in rows]
