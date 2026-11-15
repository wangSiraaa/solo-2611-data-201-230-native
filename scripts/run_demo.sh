#!/usr/bin/env bash
# 端到端演示:plan / load / verify 全场景。
# 只在独立实验库(默认 route_lab)上运行,不连接任何业务库。
# 用法: scripts/run_demo.sh
# 环境变量: ROUTECOPY_DSN(默认本机 socket 的 route_lab)
set -u
cd "$(dirname "$0")/.."

PY="${PYTHON:-/workspace/.venv/bin/python}"
export PYTHONPATH="$PWD/src"
export ROUTECOPY_DSN="${ROUTECOPY_DSN:-host=/tmp/pgrun port=54329 user=postgres dbname=route_lab}"
PSQL="psql -h /tmp/pgrun -p 54329 -U postgres -d route_lab -q"
RC="$PY -m routecopy"
OUT=out
mkdir -p "$OUT"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
fail=0
expect_rc() {  # expect_rc <期望码> <实际码> <说明>
  if [ "$1" != "$2" ]; then echo "!! $3: 期望退出码 $1,实际 $2"; fail=1; fi
}

step "场景 A:events — plan / load / verify"
$PSQL -f scripts/reset_schema.sql >/dev/null
$RC plan --config fixtures/config.json --parent route_fixture.events \
  --input fixtures/rows.jsonl --plan-out $OUT/plan-events.json --report-out $OUT/plan-events.md
expect_rc 0 $? "plan events"
$RC load --plan $OUT/plan-events.json --input fixtures/rows.jsonl \
  --out $OUT/load-events.json --report-out $OUT/load-events.md
expect_rc 0 $? "load events"
$RC verify --plan $OUT/plan-events.json --expect loaded \
  --out $OUT/verify-events.json --report-out $OUT/verify-events.md
expect_rc 0 $? "verify events"

step "场景 B:strict(无 DEFAULT)— 缺失目的地拒绝"
$RC plan --config fixtures/config.json --parent route_fixture.strict \
  --input fixtures/rows.jsonl --plan-out $OUT/plan-strict.json --report-out $OUT/plan-strict.md
expect_rc 0 $? "plan strict"
$RC load --plan $OUT/plan-strict.json --input fixtures/rows.jsonl \
  --out $OUT/load-strict.json --report-out $OUT/load-strict.md
expect_rc 0 $? "load strict"
$RC verify --plan $OUT/plan-strict.json --expect loaded \
  --out $OUT/verify-strict.json --report-out $OUT/verify-strict.md
expect_rc 0 $? "verify strict"

step "场景 C:constraint-failure — CHECK 失败整批回滚"
$PSQL -f scripts/reset_schema.sql >/dev/null
$RC plan --config fixtures/config.json --parent route_fixture.events \
  --input fixtures/constraint-failure.jsonl \
  --plan-out $OUT/plan-constraint-failure.json --report-out $OUT/plan-constraint-failure.md
expect_rc 0 $? "plan constraint-failure"
$RC load --plan $OUT/plan-constraint-failure.json --input fixtures/constraint-failure.jsonl \
  --out $OUT/load-constraint-failure.json --report-out $OUT/load-constraint-failure.md
expect_rc 3 $? "load constraint-failure 应回滚"
$RC verify --plan $OUT/plan-constraint-failure.json --expect empty \
  --out $OUT/verify-constraint-failure.json --report-out $OUT/verify-constraint-failure.md
expect_rc 0 $? "verify constraint-failure(库应为空)"

step "场景 D:修改边界后旧计划失效,以新计划运行"
$PSQL -f scripts/reset_schema.sql >/dev/null
$RC plan --config fixtures/config.json --parent route_fixture.events \
  --input fixtures/rows.jsonl --plan-out $OUT/plan-stale-v1.json --report-out $OUT/plan-stale-v1.md
expect_rc 0 $? "plan v1"
$PSQL -f scripts/alter_boundary.sql >/dev/null
echo "-- 边界已修改: middle TO (20,0) -> TO (25,0)"
$RC load --plan $OUT/plan-stale-v1.json --input fixtures/rows.jsonl \
  --out $OUT/load-stale-v1.json --report-out $OUT/load-stale-v1.md
expect_rc 4 $? "旧计划应被拒绝(目录已变)"
$PY - <<'EOF'
import json
rows = [json.loads(l) for l in open("fixtures/rows.jsonl")]
rows[0]["value"] = 11
with open("out/tmp-rows-tampered.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
EOF
$RC load --plan $OUT/plan-stale-v1.json --input $OUT/tmp-rows-tampered.jsonl \
  --out $OUT/load-tampered.json --report-out $OUT/load-tampered.md
expect_rc 4 $? "输入被篡改应被拒绝"
rm -f $OUT/tmp-rows-tampered.jsonl
$RC plan --config fixtures/config.json --parent route_fixture.events \
  --input fixtures/rows.jsonl --plan-out $OUT/plan-stale-v2.json --report-out $OUT/plan-stale-v2.md
expect_rc 0 $? "plan v2"
$RC load --plan $OUT/plan-stale-v2.json --input fixtures/rows.jsonl \
  --out $OUT/load-stale-v2.json --report-out $OUT/load-stale-v2.md
expect_rc 0 $? "load v2"
$RC verify --plan $OUT/plan-stale-v2.json --expect loaded \
  --out $OUT/verify-stale-v2.json --report-out $OUT/verify-stale-v2.md
expect_rc 0 $? "verify v2"

step "场景 E:类型不符与非法输入行进入拒绝清单(仅 plan)"
$RC plan --config fixtures/config.json --parent route_fixture.events \
  --input tests/type-mismatch.jsonl \
  --plan-out $OUT/plan-type-mismatch.json --report-out $OUT/plan-type-mismatch.md
expect_rc 0 $? "plan type-mismatch"

echo
if [ "$fail" = 0 ]; then
  echo "全部场景通过。报告见 $OUT/"
else
  echo "存在失败场景,见上方 !!"
fi
exit $fail
