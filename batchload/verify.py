"""verify：从数据库实际状态对账，并用独立验证库核对接受集合。

两部分：
  A. 目标库内部对账
       - import_receipts 逐输入记录唯一，且 总数=接受+拒绝；
       - 被接受的逻辑记录其主键确实存在于业务表，被拒绝的主键确实不在(按表)；
       - rejects 与拒绝回执一一对应。
  B. 独立参考库(全新临时库)按源顺序逐条 INSERT
       - 不做任何 Python 预判，成功/失败同样由真实约束决定；
       - 参考库得到的接受/拒绝主键集合必须与目标库完全一致。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import psycopg

from .config import TABLE_COLUMNS, TABLES, ConnInfo, InputConfig
from .csv_source import read_logical_records
from .db import apply_schema, create_database, drop_database
from .loader import LoadError


@dataclass
class VerifyReport:
    ok: bool = True
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def check(self, condition: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        self.checks.append(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))
        if not condition:
            self.ok = False
            self.failures.append(label + (f" ({detail})" if detail else ""))


def _placeholders(n: int) -> str:
    return ", ".join(["%s"] * n)


def verify_target(
    conn: ConnInfo, dbname: str, run_id: str, input_cfg: InputConfig
) -> VerifyReport:
    report = VerifyReport()
    with psycopg.connect(conn.dsn(dbname)) as c:
        # ---- 回执唯一性(主键约束已保证，这里再显式核对无重复逻辑行) ----
        for table in TABLES:
            path = input_cfg.files[table]
            records = read_logical_records(path, input_cfg.encoding)
            n_input = len(records)

            row = c.execute(
                "SELECT count(*), count(*) FILTER (WHERE accepted), "
                "count(*) FILTER (WHERE NOT accepted) "
                "FROM import_receipts WHERE run_id=%s AND table_name=%s",
                (run_id, table),
            ).fetchone()
            n_recv, n_acc, n_rej = row
            report.check(n_recv == n_input, f"{table} 回执数=输入数",
                         f"receipts={n_recv} input={n_input}")
            report.check(n_recv == n_acc + n_rej, f"{table} 总数=接受+拒绝",
                         f"{n_recv}={n_acc}+{n_rej}")

            dup = c.execute(
                "SELECT count(*) - count(DISTINCT logical_row) "
                "FROM import_receipts WHERE run_id=%s AND table_name=%s",
                (run_id, table),
            ).fetchone()[0]
            report.check(dup == 0, f"{table} 逐输入记录回执唯一", f"dup={dup}")

            # ---- 接受主键确实在业务表、拒绝主键确实不在 ----
            pk = TABLE_COLUMNS[table][0]
            accepted_keys = {
                r[0]
                for r in c.execute(
                    "SELECT source_key FROM import_receipts "
                    "WHERE run_id=%s AND table_name=%s AND accepted",
                    (run_id, table),
                ).fetchall()
            }
            db_keys = {
                r[0] for r in c.execute(f"SELECT {pk} FROM {table}").fetchall()
            }
            report.check(
                accepted_keys == db_keys,
                f"{table} 接受集合=业务表实际主键集合",
                f"only_in_receipts={sorted(accepted_keys - db_keys)} "
                f"only_in_table={sorted(db_keys - accepted_keys)}",
            )

            # ---- rejects 与拒绝回执一一对应 ----
            rej_receipt = {
                (r[0], r[1])
                for r in c.execute(
                    "SELECT logical_row, source_key FROM import_receipts "
                    "WHERE run_id=%s AND table_name=%s AND NOT accepted",
                    (run_id, table),
                ).fetchall()
            }
            rej_table = {
                (r[0], r[1])
                for r in c.execute(
                    "SELECT logical_row, source_key FROM rejects "
                    "WHERE run_id=%s AND table_name=%s",
                    (run_id, table),
                ).fetchall()
            }
            report.check(
                rej_receipt == rej_table,
                f"{table} rejects 与拒绝回执一致",
                f"receipt={sorted(map(str, rej_receipt))} rejects={sorted(map(str, rej_table))}",
            )
    return report


def build_reference(
    conn: ConnInfo, input_cfg: InputConfig, ref_dbname: str
) -> dict[str, dict[str, set[str]]]:
    """全新独立库，按源顺序逐条 INSERT，返回每表 接受/拒绝 主键集合。"""
    drop_database(conn, ref_dbname)
    create_database(conn, ref_dbname)
    apply_schema(conn, ref_dbname)  # 只需业务两表；不建审计表也能 INSERT
    result: dict[str, dict[str, set[str]]] = {}
    with psycopg.connect(conn.dsn(ref_dbname), autocommit=True) as c:
        for table in TABLES:  # 先 customers 后 orders
            cols = TABLE_COLUMNS[table]
            accepted: set[str] = set()
            rejected: set[str] = set()
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({_placeholders(len(cols))})"
            for rec in read_logical_records(input_cfg.files[table], input_cfg.encoding):
                try:
                    c.execute(sql, rec.values)
                    accepted.add(rec.raw_key)
                except psycopg.Error:
                    rejected.add(rec.raw_key)
            result[table] = {"accepted": accepted, "rejected": rejected}
    return result


def verify_against_reference(
    conn: ConnInfo, dbname: str, run_id: str, input_cfg: InputConfig,
    ref_dbname: str | None = None,
) -> tuple[VerifyReport, dict[str, dict[str, set[str]]]]:
    report = verify_target(conn, dbname, run_id, input_cfg)
    ref_name = ref_dbname or f"verify_ref_{uuid.uuid4().hex[:12]}"
    reference = build_reference(conn, input_cfg, ref_name)
    with psycopg.connect(conn.dsn(dbname)) as c:
        for table in TABLES:
            target_acc = {
                r[0]
                for r in c.execute(
                    "SELECT source_key FROM import_receipts "
                    "WHERE run_id=%s AND table_name=%s AND accepted",
                    (run_id, table),
                ).fetchall()
            }
            target_rej = {
                r[0]
                for r in c.execute(
                    "SELECT source_key FROM import_receipts "
                    "WHERE run_id=%s AND table_name=%s AND NOT accepted",
                    (run_id, table),
                ).fetchall()
            }
            report.check(
                target_acc == reference[table]["accepted"],
                f"{table} 接受集合与逐条 INSERT 参考库一致",
                f"target={sorted(target_acc)} ref={sorted(reference[table]['accepted'])}",
            )
            report.check(
                target_rej == reference[table]["rejected"],
                f"{table} 拒绝集合与逐条 INSERT 参考库一致",
                f"target={sorted(target_rej)} ref={sorted(reference[table]['rejected'])}",
            )
    # 清理临时参考库
    try:
        drop_database(conn, ref_name)
    except Exception:
        pass
    if not report.ok:
        raise LoadError("verify 失败:\n" + "\n".join(report.failures))
    return report, reference
