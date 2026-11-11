"""SQLite 连接与建表。

设计上只追加（append-only）：snapshots / snapshot_entries / revisions /
attribution_segments 四张表均不做 UPDATE/DELETE。

- 成本中心主数据以「不可变快照」形式发布，每个快照带递增版本号；
- 归属修订提交时必须绑定一个已发布的快照版本（revisions.snapshot_version_id）；
- 任一历史修订可见的归属区间与主数据口径因此永久不变，后续发布不影响旧查询。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 允许通过环境变量指定数据库文件路径（测试与 Docker 都会用到）。
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "attribution.db"


def get_db_path() -> Path:
    return Path(os.environ.get("ATTRIBUTION_DB_PATH", str(DEFAULT_DB_PATH)))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发；外键约束开启。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接：正常结束提交，异常回滚并关闭。"""
    conn = _connect(get_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
-- 成本中心主数据快照（一次发布一条，内容不可变）
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    entry_count         INTEGER NOT NULL,
    note                TEXT
);

-- 快照内的成本中心生效区间（左闭右开）；同一成本中心可有多段，允许相邻/重叠
CREATE TABLE IF NOT EXISTS snapshot_entries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_version_id  INTEGER NOT NULL REFERENCES snapshots(snapshot_version_id),
    cost_center          TEXT    NOT NULL,
    effective_from       TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间左端点（含）
    effective_to         TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间右端点（不含）
    CHECK (effective_from < effective_to)
);

CREATE INDEX IF NOT EXISTS idx_snap_entries_version
    ON snapshot_entries (snapshot_version_id);

CREATE INDEX IF NOT EXISTS idx_snap_entries_cc
    ON snapshot_entries (snapshot_version_id, cost_center, effective_from, effective_to);

-- 归属修订批次；绑定提交时所用的完整单一快照版本
CREATE TABLE IF NOT EXISTS revisions (
    revision_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    entry_count          INTEGER NOT NULL,
    snapshot_version_id  INTEGER NOT NULL REFERENCES snapshots(snapshot_version_id),
    note                 TEXT
);

CREATE TABLE IF NOT EXISTS attribution_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id     INTEGER NOT NULL REFERENCES revisions(revision_id),
    employee_id     TEXT    NOT NULL,
    cost_center     TEXT    NOT NULL,
    effective_from  TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间左端点（含）
    effective_to    TEXT    NOT NULL,  -- ISO yyyy-mm-dd，区间右端点（不含）
    CHECK (effective_from < effective_to)
);

-- 查询路径：按员工 + 日期点 + 修订号过滤后取最大修订号
CREATE INDEX IF NOT EXISTS idx_segments_emp_dates_rev
    ON attribution_segments (employee_id, effective_from, effective_to, revision_id);

CREATE INDEX IF NOT EXISTS idx_segments_revision
    ON attribution_segments (revision_id);
"""

# 旧版库（只有 revisions 表、没有快照表）的迁移语句。
# 仅补结构，不改动任何既有数据；旧修订会回填到引导快照版本 1。
LEGACY_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    entry_count         INTEGER NOT NULL,
    note                TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_entries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_version_id  INTEGER NOT NULL REFERENCES snapshots(snapshot_version_id),
    cost_center          TEXT    NOT NULL,
    effective_from       TEXT    NOT NULL,
    effective_to         TEXT    NOT NULL,
    CHECK (effective_from < effective_to)
);
CREATE INDEX IF NOT EXISTS idx_snap_entries_version
    ON snapshot_entries (snapshot_version_id);
CREATE INDEX IF NOT EXISTS idx_snap_entries_cc
    ON snapshot_entries (snapshot_version_id, cost_center, effective_from, effective_to);
"""


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> None:
    """创建表与索引（幂等），并对旧版库做向前兼容迁移。"""
    with get_conn() as conn:
        # 旧库先补快照结构，再统一执行 IF NOT EXISTS 建表。
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revisions'"
        ).fetchone() is not None and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'"
        ).fetchone() is None
        if legacy:
            conn.executescript(LEGACY_MIGRATION_SQL)
            if "snapshot_version_id" not in _column_names(conn, "revisions"):
                # 用历史归属区间里出现过的全部成本中心（全年宽口径）构造引导快照，
                # 使旧修订满足新的覆盖规则并可继续被审计查询。
                conn.execute(
                    "INSERT INTO snapshots (entry_count, note) "
                    "SELECT COUNT(DISTINCT cost_center), 'legacy-bootstrap' "
                    "FROM attribution_segments"
                )
                bootstrap_id = conn.execute(
                    "SELECT snapshot_version_id FROM snapshots WHERE note = 'legacy-bootstrap'"
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO snapshot_entries
                        (snapshot_version_id, cost_center, effective_from, effective_to)
                    SELECT ?, cost_center, '1900-01-01', '2999-12-31'
                    FROM (SELECT DISTINCT cost_center FROM attribution_segments)
                    """,
                    (bootstrap_id,),
                )
                # SQLite 限制：ALTER ADD COLUMN 不允许带非空默认值的 REFERENCES；
                # 外键一致性由引导快照（版本 1 必然存在）保证，新表仍带完整外键。
                conn.execute(
                    "ALTER TABLE revisions ADD COLUMN snapshot_version_id INTEGER "
                    f"NOT NULL DEFAULT {bootstrap_id}"
                )
        conn.executescript(SCHEMA_SQL)
