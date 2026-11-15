"""集中管理连接参数、文件路径与审计表 DDL。

数据库完全独立：演示用的库由 init 在本机自建的 PostgreSQL 16 集群里创建，
不依赖任何既有业务库。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 本机自建、无 sudo 的演示集群监听参数(见 demo.sh / initdb)。
DEFAULT_HOST = os.environ.get("PGHOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PGPORT", "55432"))
DEFAULT_USER = os.environ.get("PGUSER", "node")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = ROOT / "fixtures" / "schema.sql"
DEFAULT_INPUT = ROOT / "fixtures" / "input.json"

# 业务导入仅涉及这两张表，顺序固定：先 customers 后 orders。
TABLES = ("customers", "orders")
# 每张表 COPY 时的列顺序(与 schema.sql 一致)。
TABLE_COLUMNS = {
    "customers": ["customer_id", "name"],
    "orders": ["order_id", "customer_id", "amount", "note"],
}
# 主键列(用于 rejects.source_key / 回执)。
PK_COLUMN = {"customers": "customer_id", "orders": "order_id"}


@dataclass
class ConnInfo:
    # 用 None 哨兵，在 __post_init__ 落到环境默认值，避免类定义期固化。
    host: str | None = None
    port: int | None = None
    user: str | None = None
    dbname: str = "batchload"

    def __post_init__(self) -> None:
        if self.host is None:
            self.host = DEFAULT_HOST
        if self.port is None:
            self.port = DEFAULT_PORT
        if self.user is None:
            self.user = DEFAULT_USER

    def dsn(self, dbname: str | None = None) -> str:
        return (
            f"host={self.host} port={self.port} user={self.user} "
            f"dbname={dbname or self.dbname}"
        )


@dataclass
class InputConfig:
    """fixtures/input.json 解析后的输入描述。"""

    encoding: str
    files: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike[str] = DEFAULT_INPUT) -> "InputConfig":
        raw = json.loads(Path(path).read_text(encoding="UTF-8"))
        base = Path(path).resolve().parent
        files = {}
        for table in TABLES:
            rel = raw[table]
            p = Path(rel)
            files[table] = p if p.is_absolute() else (base.parent / p).resolve()
        return cls(encoding=raw.get("encoding", "UTF-8"), files=files)


# 额外审计表 DDL。业务两表与 import_receipts 完全沿用 schema.sql，不做改动。
# rejects: 每条单行拒绝的完整溯源信息(表/文件/逻辑行号/主键/SQLSTATE/约束名/全文)。
AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS rejects (
    run_id          text NOT NULL,
    table_name      text NOT NULL,
    file_name       text NOT NULL,
    logical_row     bigint NOT NULL,
    source_key      text,
    sqlstate        text NOT NULL,
    constraint_name text,
    message         text NOT NULL,
    PRIMARY KEY (run_id, table_name, logical_row)
);

CREATE TABLE IF NOT EXISTS copy_attempt_log (
    attempt_id    bigserial PRIMARY KEY,
    run_id        text NOT NULL,
    table_name    text NOT NULL,
    depth         integer NOT NULL,
    row_lo        bigint NOT NULL,
    row_hi        bigint NOT NULL,
    row_count     bigint NOT NULL,
    outcome       text NOT NULL,
    sqlstate      text,
    constraint_name text,
    message       text,
    attempted_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS copy_attempt_log_run_idx
    ON copy_attempt_log (run_id, table_name, attempt_id);
"""
