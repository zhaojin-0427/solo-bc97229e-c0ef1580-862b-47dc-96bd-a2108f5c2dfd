"""Pydantic 请求/响应模型。

日期统一使用 ISO 8601（yyyy-mm-dd），SQLite 内以 TEXT 存储；
ISO 日期字符串的字典序与时间序一致，可直接做半开区间比较。
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SegmentIn(BaseModel):
    """人事导出的一条员工-成本中心生效区间（左闭右开）。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "employee_id": "E001",
                "cost_center": "CC-SH-01",
                "effective_from": "2026-01-01",
                "effective_to": "2026-04-01",
            }
        }
    )

    employee_id: str = Field(..., min_length=1, description="员工编号", examples=["E001"])
    cost_center: str = Field(..., min_length=1, description="成本中心编号", examples=["CC-SH-01"])
    effective_from: date = Field(..., description="生效起日（含）", examples=["2026-01-01"])
    effective_to: date = Field(..., description="生效止日（不含）", examples=["2026-04-01"])

    @field_validator("employee_id", "cost_center")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class CostCenterValidityIn(BaseModel):
    """主数据中的一条成本中心生效区间（左闭右开）。

    同一成本中心可在同一快照内出现多条（多段有效期），相邻/重叠均允许，
    提交归属批次时按各段的并集做覆盖判断。
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "cost_center": "CC-SH-01",
                "effective_from": "2026-01-01",
                "effective_to": "2027-01-01",
            }
        }
    )

    cost_center: str = Field(..., min_length=1, description="成本中心编号", examples=["CC-SH-01"])
    effective_from: date = Field(..., description="生效起日（含）", examples=["2026-01-01"])
    effective_to: date = Field(..., description="生效止日（不含）", examples=["2027-01-01"])

    @field_validator("cost_center")
    @classmethod
    def _strip_cost_center(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class SnapshotPublishRequest(BaseModel):
    """发布一批成本中心主数据快照，整批成功后生成一个不可变版本。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "note": "2026 年度成本中心主数据（11 月发布）",
                "entries": [
                    {"cost_center": "CC-SH-01",
                     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
                    {"cost_center": "CC-BJ-01",
                     "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
                    {"cost_center": "CC-GZ-02",
                     "effective_from": "2026-04-01", "effective_to": "2027-01-01"},
                ],
            }
        }
    )

    entries: list[CostCenterValidityIn] = Field(
        ..., min_length=1, description="成本中心生效区间（至少一条）"
    )
    note: str | None = Field(
        default=None, description="备注，如主数据发布批次", examples=["2026 年度成本中心主数据"]
    )


class SnapshotOut(BaseModel):
    snapshot_version_id: int
    entry_count: int
    note: str | None
    created_at: str


class SnapshotEntryOut(BaseModel):
    snapshot_version_id: int
    cost_center: str
    effective_from: date
    effective_to: date


class RevisionSubmitRequest(BaseModel):
    """一批补录/归属区间。整批要么全部成功，要么整批拒绝。

    必须指定一个已发布的主数据快照版本；批内每条区间都必须被该版本中
    同一成本中心有效期的并集完整覆盖，否则整批拒绝。
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "snapshot_version_id": 1,
                "note": "HR 调岗补录 2026-11",
                "entries": [
                    {"employee_id": "E001", "cost_center": "CC-GZ-02",
                     "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
                ],
            }
        }
    )

    snapshot_version_id: int = Field(
        ..., ge=1, description="提交所依据的主数据快照版本号（完整绑定单一版本）"
    )
    entries: list[SegmentIn] = Field(..., min_length=1, description="本批归属区间（至少一条）")
    note: str | None = Field(default=None, description="备注，如人事调岗补录批次号",
                             examples=["HR 调岗补录 2026-11"])


class RevisionOut(BaseModel):
    revision_id: int
    entry_count: int
    snapshot_version_id: int
    note: str | None
    created_at: str


class WorkHourRecord(BaseModel):
    """工时系统导出的一条工时记录。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"employee_id": "E001", "work_date": "2026-02-15", "hours": 8.0}
        }
    )

    employee_id: str = Field(..., min_length=1, examples=["E001"])
    work_date: date = Field(..., description="工作日期", examples=["2026-02-15"])
    hours: float = Field(..., description="小时数（原样回显，不参与归属判断）", examples=[8.0])

    @field_validator("employee_id")
    @classmethod
    def _strip_employee(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class AttributionQueryRequest(BaseModel):
    """按指定修订号的口径查询工时归属。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "revision_id": 1,
                "records": [
                    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
                    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
                ],
            }
        }
    )

    revision_id: int = Field(..., ge=0, description="月结口径修订号；0 表示尚无任何补录的空口径")
    records: list[WorkHourRecord] = Field(..., min_length=1, description="待归属的工时记录")


class AttributionResult(BaseModel):
    employee_id: str
    work_date: date
    hours: float
    revision_id: int
    cost_center: str | None = Field(..., description="成本中心；None 表示未归属")
    attributed: bool
    matched_revision_id: int | None = Field(..., description="命中区间所属的修订号，便于审计")


class SegmentsQueryRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"employee_id": "E001"}})

    employee_id: str | None = Field(default=None, description="可选，按员工过滤")


class SnapshotEntriesQueryRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"cost_center": "CC-GZ-02"}})

    cost_center: str | None = Field(default=None, description="可选，按成本中心过滤")


class RevisionAuditOut(BaseModel):
    """修订审计详情：修订头（含绑定快照版本）+ 原始区间。"""

    revision: RevisionOut
    segments: list[dict]
