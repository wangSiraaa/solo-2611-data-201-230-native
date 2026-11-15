#!/usr/bin/env bash
# 端到端复现：建库 -> 受限批量执行（多批大小 / 两来源 / 错误路径）-> 独立核对。
# 仅依赖 Python 3.11 标准库与内置 sqlite3。
set -u
cd "$(dirname "$0")"

PY=python3
OUT=output
DB="$OUT/report.db"
SCHEMA=fixtures/schema.json

rm -rf "$OUT"
mkdir -p "$OUT"

echo "== 0. 建立真实小数据库并做 schema/plan 白名单校验 =="
$PY prepare.py "$DB"
echo

echo "== 1. 主 plan（EXISTS + IN + NOT IN 共享 payments 一次/批的读取），多批大小 =="
for size in 1 2 3 7 1000; do
  # 用临时 plan 调整 batch_size，语义结果必须逐字节一致。
  $PY - "$SCHEMA" "$size" <<'PYEOF'
import json, sys, dataclasses
from pathlib import Path
from batchjoin.plan import load_schema, load_plan
schema = load_schema(sys.argv[1])
plan = load_plan("fixtures/plan.json", schema)
plan = dataclasses.replace(plan, batch_size=int(sys.argv[2]))
Path("output").mkdir(exist_ok=True)
Path(f"output/plan-b{plan.batch_size}.json").write_text(
    json.dumps({
        "outer_table": plan.outer_table,
        "outer_order": plan.outer_order,
        "batch_size": plan.batch_size,
        "expressions": json.loads(Path("fixtures/plan.json").read_text())["expressions"],
    }, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PYEOF
  $PY run.py "$DB" "output/plan-b${size}.json" "$SCHEMA" \
      "output/results-b${size}.json" "output/stats-b${size}.json"
done
echo

echo "== 2. 两来源 plan：payments 3 表达式 + flags 1 表达式，每批每来源各一条查询 =="
$PY run.py "$DB" fixtures/two-source-plan.json "$SCHEMA" \
    "$OUT/results-two-source.json" "$OUT/stats-two-source.json"
echo

echo "== 3. 标量多行错误 plan：预期退出码 3，且不产生结果/统计文件 =="
set +e
$PY run.py "$DB" fixtures/scalar-plan.json "$SCHEMA" \
    "$OUT/results-scalar.json" "$OUT/stats-scalar.json"
rc=$?
set -e
if [ "$rc" -ne 3 ]; then
  echo "期望退出码 3，实际 $rc" >&2
  exit 1
fi
if [ -e "$OUT/results-scalar.json" ] || [ -e "$OUT/stats-scalar.json" ]; then
  echo "错误查询发布了部分结果" >&2
  exit 1
fi
echo "已确认退出码 3 且无部分结果产物"
echo

echo "== 4. 独立 oracle 与不变量测试 =="
$PY check.py
echo

echo "全部产物位于 $OUT/（结果 JSON、各批统计），测试摘要见上方。"
