"""端到端测试：临时数据库，覆盖快照发布、版本化提交、审计、并发与旧库迁移。

运行：ATTRIBUTION_DB_PATH 指向临时文件后 pytest。
"""
from __future__ import annotations

import importlib
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.errors import AppError


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))

    import app.database as database
    import app.service as service
    import app.main as main

    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c


def _publish(client, entries, note=None):
    return client.post("/api/snapshots", json={"entries": entries, "note": note})


def _submit(client, snapshot_id, entries, note=None):
    return client.post(
        "/api/revisions",
        json={"snapshot_version_id": snapshot_id, "entries": entries, "note": note},
    )


# 常用主数据：CC-A 上半年有效；CC-B 两段（3 月有空档）；CC-C 相邻两段
SNAPSHOT_V1 = [
    {"cost_center": "CC-A", "effective_from": "2026-01-01", "effective_to": "2026-07-01"},
    {"cost_center": "CC-B", "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
    {"cost_center": "CC-B", "effective_from": "2026-04-01", "effective_to": "2026-07-01"},
    {"cost_center": "CC-C", "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
    {"cost_center": "CC-C", "effective_from": "2026-03-01", "effective_to": "2026-07-01"},
]


# ---------- 基础 ----------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_empty_snapshot_and_batch_rejected(client):
    assert _publish(client, []).status_code == 422
    assert client.post(
        "/api/revisions", json={"snapshot_version_id": 1, "entries": []}
    ).status_code == 422


# ---------- 快照发布：版本递增、不可变、失败不耗版本号 ----------

def test_snapshot_versions_increment_and_are_listed(client):
    r1 = _publish(client, SNAPSHOT_V1, note="v1")
    r2 = _publish(client, [
        {"cost_center": "CC-A", "effective_from": "2027-01-01", "effective_to": "2028-01-01"}
    ], note="v2")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["snapshot_version_id"] == 1
    assert r2.json()["snapshot_version_id"] == 2
    assert r1.json()["entry_count"] == 5

    listed = client.get("/api/snapshots").json()
    assert [s["snapshot_version_id"] for s in listed] == [1, 2]
    assert client.get("/api/snapshots/1").json()["note"] == "v1"


def test_snapshot_invalid_range_rejected_and_version_not_consumed(client):
    r = _publish(client, [
        {"cost_center": "CC-A", "effective_from": "2026-02-01", "effective_to": "2026-02-01"}
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DATE_RANGE"
    assert client.get("/api/snapshots").json() == []

    ok = _publish(client, SNAPSHOT_V1)
    assert ok.status_code == 201
    assert ok.json()["snapshot_version_id"] == 1  # 失败发布未消耗版本号


def test_snapshot_not_found_404(client):
    r = _submit(client, 99, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"}
    ])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"
    assert client.get("/api/snapshots/99").status_code == 404


# ---------- 带版本提交：覆盖规则 ----------

def test_revision_requires_snapshot_and_records_it(client):
    _publish(client, SNAPSHOT_V1)
    r = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-07-01"}
    ], note="初始归属")
    assert r.status_code == 201
    body = r.json()
    assert body["revision_id"] == 1
    assert body["snapshot_version_id"] == 1

    listed = client.get("/api/revisions").json()
    assert listed[0]["snapshot_version_id"] == 1


def test_unknown_cost_center_rejected_422(client):
    _publish(client, SNAPSHOT_V1)
    r = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-GHOST",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"}
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "COST_CENTER_NOT_IN_SNAPSHOT"
    assert client.get("/api/revisions").json() == []


def test_coverage_exact_fit_and_within_allowed(client):
    _publish(client, SNAPSHOT_V1)
    # 与有效期等长（左闭右开边界精确贴合）
    r1 = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-07-01"}
    ])
    assert r1.status_code == 201
    # 完全落在有效期内
    r2 = _submit(client, 1, [
        {"employee_id": "E2", "cost_center": "CC-A",
         "effective_from": "2026-02-01", "effective_to": "2026-03-01"}
    ])
    assert r2.status_code == 201


def test_coverage_end_exceeded_rejects_whole_batch(client):
    _publish(client, SNAPSHOT_V1)
    r = _submit(client, 1, [
        # 合法条目
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        # 非法条目：止日超出 CC-A 有效期
        {"employee_id": "E2", "cost_center": "CC-A",
         "effective_from": "2026-06-01", "effective_to": "2026-08-01"},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SNAPSHOT_COVERAGE_VIOLATION"
    # 整批回滚：修订号与数据均不变
    assert client.get("/api/revisions").json() == []


def test_coverage_start_before_validity_rejected(client):
    _publish(client, SNAPSHOT_V1)
    r = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2025-12-31", "effective_to": "2026-02-01"}
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SNAPSHOT_COVERAGE_VIOLATION"


def test_coverage_gap_between_periods_rejected(client):
    _publish(client, SNAPSHOT_V1)
    # CC-B 两段之间 3 月有空档：[02-15, 04-15) 跨越空档
    r = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-02-15", "effective_to": "2026-04-15"}
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SNAPSHOT_COVERAGE_VIOLATION"
    # 落在单段内则允许
    ok = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-01-15", "effective_to": "2026-02-15"}
    ])
    assert ok.status_code == 201


def test_coverage_union_of_adjacent_periods_allowed(client):
    _publish(client, SNAPSHOT_V1)
    # CC-C 两段首尾相接，合并后覆盖整个上半年
    r = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-C",
         "effective_from": "2026-01-01", "effective_to": "2026-07-01"}
    ])
    assert r.status_code == 201


def test_coverage_is_per_cost_center(client):
    # CC-A 在 7 月失效，但 CC-X 在下半年有效；不能用 CC-X 的有效期给 CC-A 兜底
    _publish(client, [
        {"cost_center": "CC-A", "effective_from": "2026-01-01", "effective_to": "2026-07-01"},
        {"cost_center": "CC-X", "effective_from": "2026-07-01", "effective_to": "2027-01-01"},
    ])
    r = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-06-01", "effective_to": "2026-08-01"}
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SNAPSHOT_COVERAGE_VIOLATION"


def test_static_batch_rules_still_enforced(client):
    _publish(client, SNAPSHOT_V1)
    # 起日不早于止日
    r1 = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-02-01", "effective_to": "2026-02-01"}
    ])
    assert r1.status_code == 400
    assert r1.json()["error"]["code"] == "INVALID_DATE_RANGE"
    # 批内同员工重叠
    r2 = _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-02-01", "effective_to": "2026-04-01"},
    ])
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "OVERLAPPING_SEGMENTS"
    assert client.get("/api/revisions").json() == []


# ---------- 不可变：后续快照发布不改变旧修订 ----------

def test_later_snapshot_publication_does_not_change_old_revision(client):
    _publish(client, SNAPSHOT_V1, note="v1")
    _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-07-01"}
    ], note="按 v1 提交")
    query = {
        "revision_id": 1,
        "records": [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}],
    }
    before = client.post("/api/attribution/query", json=query).json()
    assert before[0]["cost_center"] == "CC-A"

    # v2 中 CC-A 被注销（不再出现）
    _publish(client, [
        {"cost_center": "CC-ONLY", "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
    ], note="v2-CC-A 失效")

    # 旧修订查询结果不变，审计仍指向快照 v1
    after = client.post("/api/attribution/query", json=query).json()
    assert after == before
    audit = client.get("/api/revisions/1").json()
    assert audit["revision"]["snapshot_version_id"] == 1
    assert len(audit["segments"]) == 1

    # 用 v1 仍可提交 CC-A 的历史区间（旧版本不可变）；用 v2 则被拒
    ok_old = _submit(client, 1, [
        {"employee_id": "E2", "cost_center": "CC-A",
         "effective_from": "2026-03-01", "effective_to": "2026-04-01"}
    ])
    assert ok_old.status_code == 201
    rejected_new = _submit(client, 2, [
        {"employee_id": "E2", "cost_center": "CC-A",
         "effective_from": "2026-03-01", "effective_to": "2026-04-01"}
    ])
    assert rejected_new.status_code == 422
    assert rejected_new.json()["error"]["code"] == "COST_CENTER_NOT_IN_SNAPSHOT"


def test_time_travel_query_semantics_unchanged(client):
    _publish(client, SNAPSHOT_V1)
    _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
    ])
    _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-04-01", "effective_to": "2026-06-01"}
    ])
    r = client.post("/api/attribution/query", json={
        "revision_id": 2,
        "records": [
            {"employee_id": "E1", "work_date": "2026-01-01", "hours": 8},
            {"employee_id": "E1", "work_date": "2026-04-01", "hours": 8},
            {"employee_id": "E1", "work_date": "2026-03-15", "hours": 8},
            {"employee_id": "E9", "work_date": "2026-01-01", "hours": 8},
        ],
    })
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["cost_center"] == "CC-A" and rows[0]["matched_revision_id"] == 1
    assert rows[1]["cost_center"] == "CC-B" and rows[1]["matched_revision_id"] == 2
    assert rows[2]["cost_center"] == "CC-A"
    assert rows[3]["cost_center"] is None and rows[3]["attributed"] is False

    # revision_id=0 空口径
    r0 = client.post("/api/attribution/query", json={
        "revision_id": 0,
        "records": [{"employee_id": "E1", "work_date": "2026-01-15", "hours": 8}],
    })
    assert r0.json()[0]["cost_center"] is None
    assert client.post("/api/attribution/query", json={
        "revision_id": 99,
        "records": [{"employee_id": "E1", "work_date": "2026-01-15", "hours": 8}],
    }).status_code == 404


# ---------- 审计接口 ----------

def test_audit_endpoints(client):
    _publish(client, SNAPSHOT_V1, note="v1")
    _submit(client, 1, [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
        {"employee_id": "E2", "cost_center": "CC-B",
         "effective_from": "2026-04-01", "effective_to": "2026-05-01"},
    ])

    # 快照条目：全量与按成本中心过滤
    all_entries = client.post("/api/snapshots/1/entries", json={}).json()
    assert len(all_entries) == 5
    cc_b = client.post("/api/snapshots/1/entries", json={"cost_center": "CC-B"}).json()
    assert len(cc_b) == 2 and all(e["cost_center"] == "CC-B" for e in cc_b)
    assert client.post("/api/snapshots/99/entries", json={}).status_code == 404

    # 修订详情
    audit = client.get("/api/revisions/1").json()
    assert audit["revision"]["revision_id"] == 1
    assert audit["revision"]["snapshot_version_id"] == 1
    assert len(audit["segments"]) == 2
    assert client.get("/api/revisions/99").status_code == 404

    # 原始区间过滤
    segs = client.post("/api/revisions/1/segments", json={"employee_id": "E1"}).json()
    assert len(segs) == 1 and segs[0]["employee_id"] == "E1"
    assert client.post("/api/revisions/99/segments", json={}).status_code == 404


# ---------- 并发：完整单一版本、无半批 ----------

def test_concurrent_snapshot_publishes_are_complete_and_distributed(client):
    from datetime import date

    from app import service
    from app.schemas import CostCenterValidityIn, SnapshotPublishRequest

    n = 8
    barrier = threading.Barrier(n)
    errors: list[Exception] = []
    ids: list[int] = []
    lock = threading.Lock()

    def worker(i: int):
        barrier.wait()
        try:
            req = SnapshotPublishRequest(entries=[CostCenterValidityIn(
                cost_center=f"CC-{i}",
                effective_from=date(2026, 1, 1),
                effective_to=date(2027, 1, 1),
            )], note=f"cc-{i}")
            out = service.publish_snapshot(req)
            with lock:
                ids.append(out["snapshot_version_id"])
        except Exception as exc:  # pragma: no cover - 并发下不应发生
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(ids) == list(range(1, n + 1))
    # 每个版本内容完整且互不串版
    for vid in ids:
        entries = service.list_snapshot_entries(vid, None)
        assert len(entries) == 1
        assert entries[0]["snapshot_version_id"] == vid


def test_concurrent_revision_submits_no_half_batch(client):
    from datetime import date

    from app import service
    from app.schemas import (
        CostCenterValidityIn,
        RevisionSubmitRequest,
        SegmentIn,
        SnapshotPublishRequest,
    )

    # CC-A 仅上半年有效：坏区间 [06-01, 09-01) 止日溢出，必被覆盖规则拒绝
    service.publish_snapshot(SnapshotPublishRequest(entries=[CostCenterValidityIn(
        cost_center="CC-A",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 7, 1),
    )]))

    n = 8
    barrier = threading.Barrier(n + 1)
    results: list[str] = []
    lock = threading.Lock()

    def worker(i: int, valid: bool):
        barrier.wait()
        try:
            if valid:
                req = RevisionSubmitRequest(
                    snapshot_version_id=1,
                    entries=[SegmentIn(
                        employee_id=f"E{i}", cost_center="CC-A",
                        effective_from=date(2026, 1, 1), effective_to=date(2026, 2, 1),
                    )],
                )
                service.submit_revision(req)
                with lock:
                    results.append("ok")
            else:
                # 区间起点在 CC-A 有效期内、止日溢出 -> 覆盖违规而非成本中心缺失
                bad = RevisionSubmitRequest(
                    snapshot_version_id=1,
                    entries=[SegmentIn(
                        employee_id=f"E{i}", cost_center="CC-A",
                        effective_from=date(2026, 6, 1), effective_to=date(2026, 9, 1),
                    )],
                )
                try:
                    service.submit_revision(bad)
                except AppError:
                    with lock:
                        results.append("rejected")
        except Exception as exc:  # pragma: no cover
            with lock:
                results.append(f"error:{exc!r}")

    threads = [threading.Thread(target=worker, args=(i, i != 3)) for i in range(n)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert sorted(results) == ["ok"] * (n - 1) + ["rejected"]
    revisions = service.list_revisions()
    assert len(revisions) == n - 1
    assert all(r["snapshot_version_id"] == 1 for r in revisions)
    # 无半批：每个修订的区间数都等于其申报条目数
    for r in revisions:
        segs = service.list_segments(r["revision_id"], None)
        assert len(segs) == r["entry_count"] == 1


# ---------- 旧版数据库迁移 ----------

def test_legacy_database_bootstrap_migration(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    # 手工构造旧版库（无快照表、revisions 无 snapshot_version_id）
    raw = sqlite3.connect(str(db_file))
    raw.executescript(
        """
        CREATE TABLE revisions (
            revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            entry_count INTEGER NOT NULL,
            note        TEXT
        );
        CREATE TABLE attribution_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER NOT NULL REFERENCES revisions(revision_id),
            employee_id TEXT NOT NULL,
            cost_center TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to   TEXT NOT NULL,
            CHECK (effective_from < effective_to)
        );
        INSERT INTO revisions (entry_count, note) VALUES (1, 'legacy');
        INSERT INTO attribution_segments
            (revision_id, employee_id, cost_center, effective_from, effective_to)
        VALUES (1, 'E1', 'CC-OLD', '2026-01-01', '2026-02-01');
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))
    import app.database as database
    import app.service as service
    import app.main as main
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with TestClient(main.app) as c:
        # 引导快照版本 1 包含历史成本中心
        snaps = c.get("/api/snapshots").json()
        assert len(snaps) == 1 and snaps[0]["entry_count"] == 1
        # 旧修订可审计，回填绑定引导快照
        audit = c.get("/api/revisions/1").json()
        assert audit["revision"]["snapshot_version_id"] == 1
        assert audit["segments"][0]["cost_center"] == "CC-OLD"
        # 旧口径查询照常
        q = c.post("/api/attribution/query", json={
            "revision_id": 1,
            "records": [{"employee_id": "E1", "work_date": "2026-01-15", "hours": 8}],
        }).json()
        assert q[0]["cost_center"] == "CC-OLD"
        # 引导快照有效期足够宽，新提交可继续引用
        r = c.post("/api/revisions", json={
            "snapshot_version_id": 1,
            "entries": [{"employee_id": "E2", "cost_center": "CC-OLD",
                         "effective_from": "2030-01-01", "effective_to": "2030-02-01"}],
        })
        assert r.status_code == 201
        assert r.json()["revision_id"] == 2
