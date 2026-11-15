"""端到端集成测试：在本机独立 PostgreSQL 16 集群上跑真实 COPY/SAVEPOINT。

直接复用 batchload 包，不 mock 数据库——所有接受/拒绝都由真实约束决定。
运行： .venv/bin/python -m unittest -v
"""
from __future__ import annotations

import unittest
import uuid
from pathlib import Path

import psycopg

from batchload.config import ConnInfo, InputConfig
from batchload.csv_source import read_logical_records
from batchload.db import drop_database, init_database
from batchload.loader import Loader
from batchload.verify import verify_against_reference

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"

EXPECT_ACCEPT = {
    "customers": {"c1", "c2", "c4"},
    "orders": {"o1", "o2", "o5", "o6"},
}
EXPECT_REJECT = {
    "customers": {"c1", "c3"},
    "orders": {"o2", "o3", "o4", "o1"},
}
# 每条拒绝期望的 SQLSTATE(由真实数据库约束决定)。
EXPECT_SQLSTATE = {
    ("customers", 3): "23505",   # 重复 c1
    ("customers", 4): "23502",   # name 空 -> NOT NULL
    ("orders", 2): "22P02",      # amount='wrong'
    ("orders", 4): "23503",      # 未知客户
    ("orders", 5): "23514",      # 负金额
    ("orders", 7): "23505",      # 重复 o1
}


class LoaderIntegrationTest(unittest.TestCase):
    conn = ConnInfo()

    @classmethod
    def setUpClass(cls) -> None:
        cls.input_cfg = InputConfig.load(FIX / "input.json")
        cls._dbs: list[str] = []

    def _fresh_db(self) -> str:
        db = f"test_{uuid.uuid4().hex[:10]}"
        init_database(self.conn, db, recreate=True)
        self._dbs.append(db)
        return db

    def _load(self, db: str, block_rows: int, run_id: str | None = None):
        run_id = run_id or f"t-{uuid.uuid4().hex[:8]}"
        with psycopg.connect(self.conn.dsn(db)) as main, psycopg.connect(
            self.conn.dsn(db), autocommit=True
        ) as audit:
            loader = Loader(main, audit, run_id, self.input_cfg, block_rows)
            outcomes = loader.run()
        return run_id, outcomes

    def tearDown(self) -> None:
        for db in self._dbs:
            try:
                drop_database(self.conn, db)
            except Exception:
                pass
        self._dbs.clear()

    # ------------------------------------------------------------------ #
    def test_business_result_both_block_sizes(self) -> None:
        for br in (2, 5):
            db = self._fresh_db()
            run_id, oc = self._load(db, br)
            self.assertEqual(oc["customers"].total, 5)
            self.assertEqual(oc["customers"].accepted, 3)
            self.assertEqual(oc["customers"].rejected, 2)
            self.assertEqual(oc["orders"].total, 8)
            self.assertEqual(oc["orders"].accepted, 4)
            self.assertEqual(oc["orders"].rejected, 4)
            with psycopg.connect(self.conn.dsn(db)) as c:
                cust = {r[0] for r in c.execute("SELECT customer_id FROM customers")}
                ords = {r[0] for r in c.execute("SELECT order_id FROM orders")}
            self.assertEqual(cust, EXPECT_ACCEPT["customers"])
            self.assertEqual(ords, EXPECT_ACCEPT["orders"])

    def test_reject_provenance_and_sqlstate(self) -> None:
        db = self._fresh_db()
        run_id, _ = self._load(db, 5)
        with psycopg.connect(self.conn.dsn(db)) as c:
            rows = c.execute(
                "SELECT table_name, logical_row, source_key, sqlstate, "
                "constraint_name, file_name FROM rejects WHERE run_id=%s "
                "ORDER BY table_name, logical_row",
                (run_id,),
            ).fetchall()
        self.assertEqual(len(rows), 6)
        by_loc = {(r[0], r[1]): r for r in rows}
        for (table, lrow), state in EXPECT_SQLSTATE.items():
            rec = by_loc[(table, lrow)]
            self.assertEqual(rec[3], state)            # sqlstate
            self.assertTrue(rec[5].endswith(".csv"))  # file 溯源
        # 有真实约束名的两类必须带上约束名
        self.assertEqual(by_loc[("customers", 3)][4], "customers_pkey")
        self.assertEqual(by_loc[("orders", 5)][4], "orders_amount_check")
        self.assertEqual(by_loc[("orders", 4)][4], "orders_customer_id_fkey")

    def test_bad_then_good_same_key_keeps_good(self) -> None:
        # o2 先坏(金额非法)后好；好行必须胜出且坏行不得抢占订单号。
        db = self._fresh_db()
        run_id, _ = self._load(db, 5)
        with psycopg.connect(self.conn.dsn(db)) as c:
            amount, note = c.execute(
                "SELECT amount, note FROM orders WHERE order_id='o2'"
            ).fetchone()
            state_bad = c.execute(
                "SELECT sqlstate FROM import_receipts "
                "WHERE run_id=%s AND table_name='orders' AND logical_row=2",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(str(amount), "5.00")
        self.assertEqual(note, "good later")
        self.assertEqual(state_bad, "22P02")

    def test_embedded_newline_and_quoted_comma_preserved(self) -> None:
        db = self._fresh_db()
        self._load(db, 2)
        with psycopg.connect(self.conn.dsn(db)) as c:
            o6 = c.execute("SELECT note FROM orders WHERE order_id='o6'").fetchone()[0]
            o5 = c.execute("SELECT note FROM orders WHERE order_id='o5'").fetchone()[0]
        self.assertEqual(o6, "two\nlines")
        self.assertEqual(o5, "quoted, value")

    def test_logical_not_physical_row_numbers(self) -> None:
        # o6 跨两个物理行，但逻辑行号仍是 8，且总逻辑记录数为 8。
        recs = read_logical_records(self.input_cfg.files["orders"], "UTF-8")
        self.assertEqual(len(recs), 8)
        self.assertEqual(recs[-1].logical_row, 8)
        self.assertEqual(recs[-1].raw_key, "o6")

    def test_all_good_block_single_bulk_copy(self) -> None:
        # 干净订单文件、block_rows 大于记录数：orders 仅一次 depth=0 成功 COPY。
        good_cfg = InputConfig(
            encoding="UTF-8",
            files={
                "customers": self.input_cfg.files["customers"],
                "orders": FIX / "orders_good.csv",
            },
        )
        db = self._fresh_db()
        run_id = f"g-{uuid.uuid4().hex[:8]}"
        with psycopg.connect(self.conn.dsn(db)) as main, psycopg.connect(
            self.conn.dsn(db), autocommit=True
        ) as audit:
            Loader(main, audit, run_id, good_cfg, 5).run()
        with psycopg.connect(self.conn.dsn(db)) as c:
            attempts = c.execute(
                "SELECT depth, outcome, row_lo, row_hi, row_count "
                "FROM copy_attempt_log WHERE run_id=%s AND table_name='orders'",
                (run_id,),
            ).fetchall()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0], (0, "success", 1, 3, 3))

    def test_bisection_log_structure(self) -> None:
        db = self._fresh_db()
        run_id, _ = self._load(db, 2)
        with psycopg.connect(self.conn.dsn(db)) as c:
            orders_log = c.execute(
                "SELECT depth, row_lo, row_hi, outcome FROM copy_attempt_log "
                "WHERE run_id=%s AND table_name='orders' ORDER BY attempt_id",
                (run_id,),
            ).fetchall()
        # 块 [1..2] 先失败，再左 [1] 后右 [2]，证明左先右后、保持源顺序。
        self.assertEqual(orders_log[0], (0, 1, 2, "failed"))
        self.assertEqual(orders_log[1], (1, 1, 1, "success"))
        self.assertEqual(orders_log[2], (1, 2, 2, "failed"))
        # 每次失败都被隔离：最终每个逻辑行都有结论(无未决、无整批中止)。
        with psycopg.connect(self.conn.dsn(db)) as c:
            n = c.execute(
                "SELECT count(*) FROM import_receipts WHERE run_id=%s AND table_name='orders'",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(n, 8)

    def test_preexisting_rows_are_not_overwritten(self) -> None:
        db = self._fresh_db()
        with psycopg.connect(self.conn.dsn(db), autocommit=True) as c:
            c.execute("INSERT INTO customers(customer_id,name) VALUES ('c1','GUARD')")
        run_id, _ = self._load(db, 5)
        with psycopg.connect(self.conn.dsn(db)) as c:
            name = c.execute(
                "SELECT name FROM customers WHERE customer_id='c1'"
            ).fetchone()[0]
            state = c.execute(
                "SELECT sqlstate FROM import_receipts "
                "WHERE run_id=%s AND table_name='customers' AND logical_row=1",
                (run_id,),
            ).fetchone()[0]
        self.assertEqual(name, "GUARD")
        self.assertEqual(state, "23505")

    def test_receipt_uniqueness_and_totals(self) -> None:
        db = self._fresh_db()
        run_id, _ = self._load(db, 5)
        report, _ = verify_against_reference(
            self.conn, db, run_id, self.input_cfg,
            ref_dbname=f"ref_{uuid.uuid4().hex[:8]}",
        )
        self.assertTrue(report.ok, msg="\n".join(report.failures))

    def test_invalid_block_rows_rejected(self) -> None:
        db = self._fresh_db()
        with self.assertRaises(ValueError):
            with psycopg.connect(self.conn.dsn(db)) as main, psycopg.connect(
                self.conn.dsn(db), autocommit=True
            ) as audit:
                Loader(main, audit, "x", self.input_cfg, 0)

    def test_empty_field_normalized_to_null(self) -> None:
        recs = read_logical_records(self.input_cfg.files["customers"], "UTF-8")
        # c3 行 name 为空 -> None(COPY 时写空字段 -> NULL，触发 NOT NULL)
        self.assertEqual(recs[3].values, ("c3", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
