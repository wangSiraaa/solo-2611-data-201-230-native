"""核心装载器：真实 COPY + SAVEPOINT 二分隔离坏行。

算法(每张表按逻辑记录切块，每块一个事务)：
  1. 用 psycopg 原生 cursor.copy() 对整块做一次真实 COPY，包在 SAVEPOINT 内。
  2. 整块成功 -> RELEASE，块内所有行接受。
  3. 整块失败 -> ROLLBACK TO SAVEPOINT(不污染事务、不回滚此前已确认好行)，
     再按左右半块递归；左半完全处理完才处理右半，始终保持源顺序。
  4. 递归到单行仍失败 -> 记为 rejects，不终止整批。

所有接受/拒绝判定都由数据库真实约束给出(PK 23505、NOT NULL 23502、
CHECK 23514、外键 23503、numeric 输入 22P02 等)，Python 不做近似预判。
每次 COPY 尝试(含行区间、成败、SQLSTATE、约束名)都写入 copy_attempt_log。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .config import TABLE_COLUMNS, TABLES, InputConfig
from .csv_source import CsvRecord, read_logical_records


@dataclass
class RowError:
    sqlstate: str | None
    constraint_name: str | None
    message: str


@dataclass
class TableOutcome:
    total: int = 0
    accepted: int = 0
    rejected: int = 0


class LoadError(RuntimeError):
    pass


class Loader:
    def __init__(
        self,
        main_conn,          # 业务连接：每块一个事务
        audit_conn,         # 自动提交连接：写 copy_attempt_log，失败尝试也能留存
        run_id: str,
        input_cfg: InputConfig,
        block_rows: int,
    ) -> None:
        if block_rows < 1:
            raise ValueError("block_rows 必须 >= 1")
        self.conn = main_conn
        self.audit = audit_conn
        self.run_id = run_id
        self.input = input_cfg
        self.block_rows = block_rows
        self._sp_seq = 0
        self.outcomes = {t: TableOutcome() for t in TABLES}

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def run(self) -> dict[str, TableOutcome]:
        # 固定顺序：先 customers 全部完成，再 orders。
        for table in TABLES:
            path = self.input.files[table]
            records = read_logical_records(path, self.input.encoding)
            self.outcomes[table].total = len(records)
            self._load_table(table, path, records)
        return self.outcomes

    # ------------------------------------------------------------------ #
    # 表级：按逻辑记录切成原始块
    # ------------------------------------------------------------------ #
    def _load_table(self, table: str, path: Path, records: list[CsvRecord]) -> None:
        if not records:
            return
        for start in range(0, len(records), self.block_rows):
            block = records[start : start + self.block_rows]
            self._process_block(table, path, block)

    # ------------------------------------------------------------------ #
    # 块级：一个事务；好行 + 本块回执一起提交
    # ------------------------------------------------------------------ #
    def _process_block(self, table: str, path: Path, block: list[CsvRecord]) -> None:
        receipts: list[dict] = []
        rejects: list[dict] = []
        # 递归在这两个列表里落地每条记录的结论，保证逐输入记录唯一回执。
        self._attempt(table, block, 0, len(block), depth=0,
                      receipts=receipts, rejects=rejects)

        # 完整性自检：块内每条逻辑记录恰好一个结论。
        if len(receipts) != len(block):
            raise LoadError(
                f"{table} 块逻辑行 {block[0].logical_row}.."
                f"{block[-1].logical_row} 结论数 {len(receipts)} != 记录数 {len(block)}"
            )
        # 源顺序写出回执与拒绝。
        self._persist_block(table, path.name, receipts, rejects)
        self.conn.commit()

        oc = self.outcomes[table]
        oc.accepted += sum(1 for r in receipts if r["accepted"])
        oc.rejected += sum(1 for r in receipts if not r["accepted"])

    # ------------------------------------------------------------------ #
    # 递归二分
    # ------------------------------------------------------------------ #
    def _attempt(
        self,
        table: str,
        block: list[CsvRecord],
        lo: int,
        hi: int,
        depth: int,
        receipts: list[dict],
        rejects: list[dict],
    ) -> None:
        subset = block[lo:hi]
        sp = self._next_savepoint()
        self.conn.execute(f"SAVEPOINT {sp}")
        err = self._copy(table, subset, depth)
        if err is None:
            # 整块(或子区间)满足全部约束 -> 接受，RELEASE 保留这些好行。
            self.conn.execute(f"RELEASE SAVEPOINT {sp}")
            for rec in subset:
                receipts.append(self._receipt(rec, accepted=True, err=None))
            return

        # 失败：回到保存点，事务保持可用，此前已确认的好行不受影响。
        self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        self.conn.execute(f"RELEASE SAVEPOINT {sp}")

        if hi - lo == 1:
            rec = block[lo]
            receipts.append(self._receipt(rec, accepted=False, err=err))
            rejects.append(self._reject(table, rec, err))
            return

        mid = (lo + hi) // 2
        # 严格源顺序：左半处理完才处理右半。
        self._attempt(table, block, lo, mid, depth + 1, receipts, rejects)
        self._attempt(table, block, mid, hi, depth + 1, receipts, rejects)

    # ------------------------------------------------------------------ #
    # 原生 psycopg COPY：一次尝试覆盖 subset 的全部逻辑行
    # ------------------------------------------------------------------ #
    def _copy(self, table: str, subset: list[CsvRecord], depth: int) -> RowError | None:
        cols = TABLE_COLUMNS[table]
        stmt = f"COPY {table} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv)"
        payload = self._build_csv(subset)
        row_lo = subset[0].logical_row
        row_hi = subset[-1].logical_row
        err: RowError | None = None
        try:
            with self.conn.cursor().copy(stmt) as cp:
                cp.write(payload)
        except psycopg.Error as e:  # 真实数据库约束/类型错误
            err = RowError(
                sqlstate=e.sqlstate,
                constraint_name=e.diag.constraint_name,
                message=e.diag.message_primary or str(e),
            )
        self._log_attempt(table, depth, row_lo, row_hi, len(subset), err)
        return err

    # ------------------------------------------------------------------ #
    # 持久化：import_receipts 与 rejects 在同事务随好行一起提交
    # ------------------------------------------------------------------ #
    def _persist_block(
        self,
        table: str,
        file_name: str,
        receipts: list[dict],
        rejects: list[dict],
    ) -> None:
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT INTO import_receipts
                (run_id, table_name, logical_row, accepted, source_key,
                 sqlstate, constraint_name)
            VALUES (%(run_id)s, %(table_name)s, %(logical_row)s, %(accepted)s,
                    %(source_key)s, %(sqlstate)s, %(constraint_name)s)
            """,
            [
                {
                    "run_id": self.run_id,
                    "table_name": table,
                    "logical_row": r["logical_row"],
                    "accepted": r["accepted"],
                    "source_key": r["source_key"],
                    "sqlstate": r["sqlstate"],
                    "constraint_name": r["constraint_name"],
                }
                for r in receipts
            ],
        )
        cur.executemany(
            """
            INSERT INTO rejects
                (run_id, table_name, file_name, logical_row, source_key,
                 sqlstate, constraint_name, message)
            VALUES (%(run_id)s, %(table_name)s, %(file_name)s, %(logical_row)s,
                    %(source_key)s, %(sqlstate)s, %(constraint_name)s, %(message)s)
            """,
            [
                {
                    "run_id": self.run_id,
                    "table_name": table,
                    "file_name": file_name,
                    "logical_row": r["logical_row"],
                    "source_key": r["source_key"],
                    "sqlstate": r["sqlstate"],
                    "constraint_name": r["constraint_name"],
                    "message": r["message"],
                }
                for r in rejects
            ],
        )

    # ------------------------------------------------------------------ #
    # 审计：每次 COPY 尝试单独自动提交，失败尝试不随块回滚丢失
    # ------------------------------------------------------------------ #
    def _log_attempt(
        self,
        table: str,
        depth: int,
        row_lo: int,
        row_hi: int,
        row_count: int,
        err: RowError | None,
    ) -> None:
        self.audit.execute(
            """
            INSERT INTO copy_attempt_log
                (run_id, table_name, depth, row_lo, row_hi, row_count,
                 outcome, sqlstate, constraint_name, message)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                self.run_id,
                table,
                depth,
                row_lo,
                row_hi,
                row_count,
                "success" if err is None else "failed",
                None if err is None else err.sqlstate,
                None if err is None else err.constraint_name,
                None if err is None else err.message,
            ),
        )

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    def _next_savepoint(self) -> str:
        self._sp_seq += 1
        return f"sp_{self._sp_seq}"

    @staticmethod
    def _build_csv(subset: list[CsvRecord]) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        for rec in subset:
            # None 写成空字段；PostgreSQL CSV COPY 将空字段视为 NULL。
            writer.writerow(["" if v is None else v for v in rec.values])
        return buf.getvalue().encode("UTF-8")

    def _receipt(self, rec: CsvRecord, *, accepted: bool, err: RowError | None) -> dict:
        return {
            "logical_row": rec.logical_row,
            "accepted": accepted,
            "source_key": rec.raw_key,
            "sqlstate": None if err is None else err.sqlstate,
            "constraint_name": None if err is None else err.constraint_name,
        }

    def _reject(self, table: str, rec: CsvRecord, err: RowError) -> dict:
        return {
            "table_name": table,
            "logical_row": rec.logical_row,
            "source_key": rec.raw_key,
            "sqlstate": err.sqlstate,
            "constraint_name": err.constraint_name,
            "message": err.message,
        }
