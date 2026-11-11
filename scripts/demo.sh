#!/usr/bin/env bash
# 端到端演示：成本中心快照发布 + 带版本归属提交 + 覆盖拦截 + 口径冻结 + 审计。
# 用法：
#   docker compose up -d --build       # 先启动服务
#   ./scripts/demo.sh                  # 本机演示（默认 BASE=http://localhost:8000）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

post() { # $1=path  $2=json body
  curl -sS -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

code() { # 仅打印 HTTP 状态码；$1=path $2=json
  curl -sS -o /dev/null -w '%{http_code}' -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

echo "==================================================================="
echo "0. 健康检查  GET /health"
echo "==================================================================="
curl -sS "${BASE}/health" | pretty
echo

echo "==================================================================="
echo "1. 发布主数据快照版本 1（不可变）"
echo "   CC-SH-01 全年有效；CC-GZ-02 4 月起有效；CC-DEAD 仅 Q1 有效"
echo "==================================================================="
post /api/snapshots '{
  "note": "2026 年度成本中心主数据 v1",
  "entries": [
    {"cost_center": "CC-SH-01", "effective_from": "2026-01-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-GZ-02", "effective_from": "2026-04-01", "effective_to": "2027-01-01"},
    {"cost_center": "CC-DEAD",  "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "2. 非法快照：起日不早于止日 -> 整批拒绝 400，版本号不被消耗"
echo "==================================================================="
post /api/snapshots '{
  "entries": [
    {"cost_center": "CC-X", "effective_from": "2026-05-01", "effective_to": "2026-05-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "3. 修订 1：绑定快照版本 1 提交初始归属（全部区间被同成本中心覆盖）"
echo "==================================================================="
post /api/revisions '{
  "snapshot_version_id": 1,
  "note": "HR 初始归属 Q1",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-SH-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"},
    {"employee_id": "E002", "cost_center": "CC-SH-01",
     "effective_from": "2026-01-01", "effective_to": "2026-04-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "4. 阻止写入失效成本中心：CC-DEAD 在 4 月已失效"
echo "   区间 [2026-04-15, 2026-05-01) 未被完整覆盖 -> 整批拒绝 400"
echo "==================================================================="
echo "HTTP $(code /api/revisions '{
  "snapshot_version_id": 1,
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-DEAD",
     "effective_from": "2026-04-15", "effective_to": "2026-05-01"}
  ]
}')"
post /api/revisions '{
  "snapshot_version_id": 1,
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-DEAD",
     "effective_from": "2026-04-15", "effective_to": "2026-05-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "5. 其余整批拒绝场景"
echo "   a) 快照中不存在的成本中心 -> 422"
echo "   b) 不存在的快照版本       -> 404"
echo "   c) 批内同员工区间重叠     -> 400"
echo "==================================================================="
echo "a) HTTP $(code /api/revisions '{
  "snapshot_version_id": 1,
  "entries": [{"employee_id": "E001", "cost_center": "CC-GHOST",
               "effective_from": "2026-01-15", "effective_to": "2026-02-01"}]
}')"
echo "b) HTTP $(code /api/revisions '{
  "snapshot_version_id": 99,
  "entries": [{"employee_id": "E001", "cost_center": "CC-SH-01",
               "effective_from": "2026-01-15", "effective_to": "2026-02-01"}]
}')"
echo "c) HTTP $(code /api/revisions '{
  "snapshot_version_id": 1,
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-SH-01",
     "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
    {"employee_id": "E001", "cost_center": "CC-SH-01",
     "effective_from": "2026-02-01", "effective_to": "2026-04-01"}
  ]
}')"
echo

echo "==================================================================="
echo "6. 修订 2：调岗补录 E001 4~6 月转 CC-GZ-02（该中心 4 月起生效，覆盖通过）"
echo "==================================================================="
post /api/revisions '{
  "snapshot_version_id": 1,
  "note": "HR 调岗补录：E001 四月起转广州成本中心",
  "entries": [
    {"employee_id": "E001", "cost_center": "CC-GZ-02",
     "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "7. 归属查询：修订 2 口径（含左闭右开边界、空档、未归属员工）"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-04-01", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-07-01", "hours": 8},
    {"employee_id": "E999", "work_date": "2026-02-10", "hours": 4}
  ]
}' | pretty
echo

echo "==================================================================="
echo "8. 发布主数据快照版本 2：CC-DEAD 注销、CC-GZ-02 也被移除"
echo "==================================================================="
post /api/snapshots '{
  "note": "2026 主数据 v2：清理失效成本中心",
  "entries": [
    {"cost_center": "CC-SH-01", "effective_from": "2026-01-01", "effective_to": "2028-01-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "9. 口径冻结：用修订 1/2 重查，结果与 v2 发布前完全一致；"
echo "   审计显示旧修订仍绑定快照版本 1（后续发布不改变旧修订）"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 1,
  "records": [{"employee_id": "E001", "work_date": "2026-02-15", "hours": 8}]
}' | pretty
curl -sS "${BASE}/api/revisions/2" | pretty
echo "用 v2 再提 CC-GZ-02 -> 成本中心已不存在，HTTP $(code /api/revisions '{
  "snapshot_version_id": 2,
  "entries": [{"employee_id": "E001", "cost_center": "CC-GZ-02",
               "effective_from": "2026-05-01", "effective_to": "2026-06-01"}]
}')"
echo

echo "==================================================================="
echo "10. 审计接口汇总"
echo "    GET  /api/snapshots          全部快照版本"
echo "    POST /api/snapshots/1/entries 快照 1 的成本中心有效期"
echo "    GET  /api/revisions          全部修订（含绑定快照版本）"
echo "    GET  /api/revisions/2        修订 2 详情 + 原始区间"
echo "==================================================================="
curl -sS "${BASE}/api/snapshots" | pretty
post /api/snapshots/1/entries '{}' | pretty
curl -sS "${BASE}/api/revisions" | pretty
echo
echo "演示完成。交互式文档：${BASE}/docs"
