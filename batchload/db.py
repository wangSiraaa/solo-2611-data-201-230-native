"""PostgreSQL 连接与库/模式初始化。

所有库都建在本机独立演示集群上，与任何既有数据库互不影响。
业务表结构只执行 fixtures/schema.sql，审计表另用 config.AUDIT_DDL 创建。
"""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql

from .config import AUDIT_DDL, ConnInfo, DEFAULT_SCHEMA


def connect(conn: ConnInfo, dbname: str | None = None, *, autocommit: bool = False):
    return psycopg.connect(conn.dsn(dbname), autocommit=autocommit)


def _maintenance_connect(conn: ConnInfo):
    """连到维护库 postgres，用于 CREATE/DROP DATABASE。"""
    return psycopg.connect(conn.dsn("postgres"), autocommit=True)


def database_exists(conn: ConnInfo, dbname: str) -> bool:
    with _maintenance_connect(conn) as m:
        row = m.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        return row is not None


def drop_database(conn: ConnInfo, dbname: str) -> bool:
    with _maintenance_connect(conn) as m:
        # 断开该库上的其它会话，保证可重复演示。
        m.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (dbname,),
        )
        exists = m.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            return False
        m.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dbname)))
        return True


def create_database(conn: ConnInfo, dbname: str) -> None:
    with _maintenance_connect(conn) as m:
        m.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def apply_schema(conn: ConnInfo, dbname: str, schema_path: str | Path = DEFAULT_SCHEMA) -> None:
    """在目标库执行 schema.sql(customers/orders/import_receipts)。"""
    ddl = Path(schema_path).read_text(encoding="UTF-8")
    with psycopg.connect(conn.dsn(dbname)) as c:
        c.execute(ddl)
        c.commit()


def apply_audit_ddl(conn: ConnInfo, dbname: str) -> None:
    """创建额外审计表 rejects / copy_attempt_log。"""
    with psycopg.connect(conn.dsn(dbname)) as c:
        c.execute(AUDIT_DDL)
        c.commit()


def init_database(
    conn: ConnInfo, dbname: str, *, recreate: bool = False,
    schema_path: str | Path = DEFAULT_SCHEMA,
) -> None:
    """建库并部署 schema + 审计表。recreate=True 时先删后建。"""
    if recreate and database_exists(conn, dbname):
        drop_database(conn, dbname)
    if not database_exists(conn, dbname):
        create_database(conn, dbname)
    apply_schema(conn, dbname, schema_path)
    apply_audit_ddl(conn, dbname)
