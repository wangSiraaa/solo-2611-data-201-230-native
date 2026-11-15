#!/usr/bin/env bash
#
# 一条本机命令完成完整演示：
#   ./demo.sh
#
# 做的事情：
#   1. 在用户态(无 sudo / root)初始化并启动独立 PostgreSQL 16 集群(端口 55432)；
#   2. 建两个互不影响的目标库，分别用 block_rows=5 与 block_rows=2 导入；
#   3. 打印回执、拒绝溯源与原生 COPY 调用日志(证明全好块一次 COPY、坏块二分)；
#   4. verify：库内对账 + 独立参考库按源顺序逐条 INSERT 核对接受集合；
#   5. 断言两种 block_rows 得到完全相同的业务结果(日志可不同)。
#
# 仅导入 customers / orders 两表，不做 schema 迁移、自动修数或重试服务。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PGBIN="/usr/lib/postgresql/16/bin"
PGDATA="$ROOT/.pgdata"
PGPORT="${PGPORT:-55432}"
PGHOST="127.0.0.1"
DB5="batchload5"
DB2="batchload2"
RUN5="run-block5"
RUN2="run-block2"

# ---------------------------------------------------------------------------
# 0. Python 依赖(只装 psycopg[binary])
# ---------------------------------------------------------------------------
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -c "import psycopg" 2>/dev/null || pip install -r requirements.txt

# ---------------------------------------------------------------------------
# 1. 用户态 PostgreSQL 16 集群(幂等)
# ---------------------------------------------------------------------------
if [[ ! -d "$PGDATA" ]]; then
  echo ">> initdb -> $PGDATA"
  mkdir -p "$PGDATA"
  "$PGBIN/initdb" -D "$PGDATA" -U node --auth=trust --no-locale --encoding=UTF8 >/dev/null
  {
    echo "port = $PGPORT"
    echo "unix_socket_directories = '/tmp'"
    echo "listen_addresses = '127.0.0.1'"
  } >> "$PGDATA/postgresql.conf"
fi
mkdir -p "$ROOT/.pgrun"
if ! "$PGBIN/pg_isready" -h "$PGHOST" -p "$PGPORT" >/dev/null 2>&1; then
  echo ">> 启动 PostgreSQL 16 (port $PGPORT)"
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$ROOT/.pgrun/server.log" -w start >/dev/null
fi
export PGPORT PGHOST
echo ">> $(psql "postgresql://node@/postgres?host=/tmp&port=$PGPORT" -tAc 'select version();')"

# ---------------------------------------------------------------------------
# 2 & 3. 两种 block_rows 真实 COPY 导入(全新库，互不影响)
# ---------------------------------------------------------------------------
for BR in 5 2; do
  DB="batchload${BR}"; RID="run-block${BR}"
  echo
  echo "############################################################"
  echo "# block_rows=$BR  库=$DB  run_id=$RID"
  echo "############################################################"
  python -m batchload --dbname "$DB" init --recreate
  python -m batchload --dbname "$DB" load-batch --block-rows "$BR" --run-id "$RID"
done

# ---------------------------------------------------------------------------
# 4. 报告：回执 / 拒绝溯源 / 原生 COPY 调用日志
# ---------------------------------------------------------------------------
echo
echo "########################  REPORT block_rows=5  ########################"
python -m batchload --dbname "$DB5" report --run-id "$RUN5" --block-rows 5

# ---------------------------------------------------------------------------
# 5. verify：两种 block_rows 都与独立逐条 INSERT 参考库对账
# ---------------------------------------------------------------------------
echo
echo "########################  VERIFY block_rows=5  ########################"
python -m batchload --dbname "$DB5" verify --run-id "$RUN5"
echo
echo "########################  VERIFY block_rows=2  ########################"
python -m batchload --dbname "$DB2" verify --run-id "$RUN2"

# ---------------------------------------------------------------------------
# 6. 业务结果一致性断言：两库业务表实际主键集合必须相同
# ---------------------------------------------------------------------------
echo
echo "########################  一致性断言  ########################"
snapshot() {
  local db="$1"
  psql "postgresql://node@/${db}?host=/tmp&port=$PGPORT" -tAc \
    "SELECT 'customers:'||string_agg(customer_id,',' ORDER BY customer_id) FROM customers;
     SELECT 'orders:'||string_agg(order_id,',' ORDER BY order_id) FROM orders;" | sort
}
A="$(snapshot "$DB5")"; B="$(snapshot "$DB2")"
echo "block_rows=5:"; echo "$A" | sed 's/^/  /'
echo "block_rows=2:"; echo "$B" | sed 's/^/  /'
if [[ "$A" == "$B" ]]; then
  echo ">> 业务结果一致(日志不同)：PASS"
else
  echo ">> 业务结果不一致：FAIL" >&2; exit 1
fi

echo
echo "演示完成。停止集群：$PGBIN/pg_ctl -D $PGDATA stop"
