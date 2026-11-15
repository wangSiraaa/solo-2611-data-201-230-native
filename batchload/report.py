"""report：从数据库读取回执、拒绝明细与原生 COPY 调用日志。

COPY 调用日志按尝试顺序展示每次覆盖的逻辑行区间、深度、成败与 SQLSTATE，
可直接证明：全好块只做一次批量尝试，坏块沿二分逐步定位。
"""
from __future__ import annotations

import psycopg

from .config import TABLES, ConnInfo


def _print_table(rows: list[tuple], headers: tuple[str, ...]) -> None:
    widths = [len(h) for h in headers]
    str_rows = []
    for r in rows:
        cells = ["" if v is None else str(v) for v in r]
        widths = [max(w, len(c)) for w, c in zip(widths, cells)]
        str_rows.append(cells)
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(line)
    print("| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)) + " |")
    print(line)
    for cells in str_rows:
        print("| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |")
    print(line)


def print_summary(conn: ConnInfo, dbname: str, run_id: str) -> None:
    with psycopg.connect(conn.dsn(dbname)) as c:
        print(f"== run_id = {run_id}  库 = {dbname} ==")
        rows = c.execute(
            "SELECT table_name, count(*) AS total, "
            "count(*) FILTER (WHERE accepted) AS accepted, "
            "count(*) FILTER (WHERE NOT accepted) AS rejected "
            "FROM import_receipts WHERE run_id=%s GROUP BY table_name "
            "ORDER BY table_name",
            (run_id,),
        ).fetchall()
        grand = [sum(r[i] for r in rows) for i in (1, 2, 3)]
        _print_table(rows + [("合计", *grand)],
                     ("table", "total", "accepted", "rejected"))


def print_receipts(conn: ConnInfo, dbname: str, run_id: str) -> None:
    with psycopg.connect(conn.dsn(dbname)) as c:
        for table in TABLES:
            rows = c.execute(
                "SELECT logical_row, accepted, source_key, sqlstate, constraint_name "
                "FROM import_receipts WHERE run_id=%s AND table_name=%s "
                "ORDER BY logical_row",
                (run_id, table),
            ).fetchall()
            print(f"\n-- {table} 回执(按逻辑行号) --")
            _print_table(rows, ("logical_row", "accepted", "source_key",
                                "sqlstate", "constraint_name"))


def print_rejects(conn: ConnInfo, dbname: str, run_id: str) -> None:
    with psycopg.connect(conn.dsn(dbname)) as c:
        rows = c.execute(
            "SELECT table_name, file_name, logical_row, source_key, sqlstate, "
            "constraint_name, message "
            "FROM rejects WHERE run_id=%s "
            "ORDER BY table_name, logical_row",
            (run_id,),
        ).fetchall()
        print("\n== rejects：每条拒绝的完整溯源 ==")
        if not rows:
            print("(无拒绝)")
            return
        _print_table(rows, ("table", "file", "log_row", "key",
                            "sqlstate", "constraint", "message"))


def print_copy_log(conn: ConnInfo, dbname: str, run_id: str, block_rows: int | None = None) -> None:
    with psycopg.connect(conn.dsn(dbname)) as c:
        for table in TABLES:
            rows = c.execute(
                "SELECT depth, row_lo, row_hi, row_count, outcome, sqlstate, "
                "constraint_name, attempt_id "
                "FROM copy_attempt_log WHERE run_id=%s AND table_name=%s "
                "ORDER BY attempt_id",
                (run_id, table),
            ).fetchall()
            print(f"\n== {table} 原生 COPY 调用日志(尝试顺序) ==")
            print(f"共 {len(rows)} 次 COPY 尝试")
            _print_table(
                rows,
                ("depth", "row_lo", "row_hi", "rows", "outcome",
                 "sqlstate", "constraint", "attempt#"),
            )
            depth0 = [r for r in rows if r[0] == 0]
            good_blocks = [r for r in depth0 if r[4] == "success"]
            bad_blocks = [r for r in depth0 if r[4] == "failed"]
            deepest = [r for r in rows if r[3] == 1]
            print(
                f"  原始块(depth=0) {len(depth0)} 个：一次成功 {len(good_blocks)} 个、"
                f"触发二分 {len(bad_blocks)} 个；最终落到单行尝试 {len(deepest)} 次。"
            )
            if good_blocks:
                spans = ", ".join(f"[{r[1]}..{r[2]}]" for r in good_blocks)
                print(
                    "  全好块每个只做一次批量 COPY(无更深尝试)：" + spans
                )
            if bad_blocks:
                spans = ", ".join(f"[{r[1]}..{r[2]}]" for r in bad_blocks)
                print(
                    "  坏块在 SAVEPOINT 回滚后沿左右半块递归到单行：" + spans
                )
            if block_rows is not None and good_blocks:
                # 契约核对：全好块每个仅一次真实 COPY。除最后一个可能是尾块外，
                # 其余全好块行数必须等于配置的 block_rows(证明按整块真实 COPY)。
                last_hi = max(r[2] for r in depth0)
                bad = [
                    r for r in good_blocks
                    if r[3] != block_rows and r[2] != last_hi
                ]
                tail = [r for r in good_blocks if r[2] == last_hi and r[3] <= block_rows]
                print(
                    "  全好块均为真实整块 COPY(满块行数=block_rows，尾块可较小)："
                    + ("是" if not bad and tail == good_blocks[-len(tail):]
                       else f"否，异常块 {bad}")
                )


def print_full_report(conn: ConnInfo, dbname: str, run_id: str,
                      block_rows: int | None = None) -> None:
    print_summary(conn, dbname, run_id)
    print_receipts(conn, dbname, run_id)
    print_rejects(conn, dbname, run_id)
    print_copy_log(conn, dbname, run_id, block_rows=block_rows)
