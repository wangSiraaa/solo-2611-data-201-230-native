"""受限批量关联执行引擎。

执行模型（与 N+1 逐行子查询相对）：
1. 一次顺序扫描外表，按 row_id 排序、按 batch_size 切批；
2. 每批对每个「来源」（内表 + 内/外关联列组合）只发一条参数化 IN 查询，
   绑定本批出现过的不同非 NULL 关联键；
3. 取回的内行按关联键分组，再分别附回该批每一条外行——外行不折叠，
   同一关联键的不同 probe 各自计算 IN / NOT IN；
4. 同一来源上的多个表达式共享这一次读取。

统计由 SQLite trace 回调独立计数，证明查询数随「批 × 来源」增长，
而非随外行数增长。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .plan import Expression, Plan, Schema
from . import predicates

_DATA_PREFIXES = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "REPLACE")


class CardinalityError(RuntimeError):
    """标量子查询对某条外行命中超过一行。

    携带具体外行主键与全部命中内行主键，满足「以具体外行和内行身份报错、
    不能任选第一条」。
    """

    def __init__(
        self,
        expression: str,
        inner_table: str,
        outer_row_id: Any,
        inner_row_ids: list[Any],
        candidate_count: int,
    ) -> None:
        super().__init__(
            f"标量子查询 {expression!r}（内表 {inner_table}）对外表行 "
            f"row_id={outer_row_id} 返回 {candidate_count} 行 "
            f"(内行主键 {inner_row_ids})，预期至多 1 行"
        )
        self.expression = expression
        self.inner_table = inner_table
        self.outer_row_id = outer_row_id
        self.inner_row_ids = inner_row_ids
        self.candidate_count = candidate_count


def quote_ident(name: str) -> str:
    """SQL 标识符引用。name 已经过 schema 白名单验证，这里再做转义兜底。"""

    return '"' + name.replace('"', '""') + '"'


class InstrumentedConnection:
    """在 sqlite3 连接上叠加语句计数；所有取值仍走绑定参数。"""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self.all_statements = 0
        self.data_statements = 0
        self.select_statements = 0
        self._conn.set_trace_callback(self._on_trace)

    def _on_trace(self, statement: str) -> None:
        self.all_statements += 1
        head = statement.lstrip().lstrip("(").lstrip().split(None, 1)[0].upper()
        if head in _DATA_PREFIXES:
            self.data_statements += 1
        if head == "SELECT" or head == "WITH":
            self.select_statements += 1

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def table_pk(self, table: str) -> str:
        # PRAGMA 是元数据访问，trace 里不计入数据查询统计。
        info = self._conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
        pk_columns = [row[1] for row in info if row[5] > 0]
        if len(pk_columns) != 1:
            raise ValueError(
                f"内表 {table!r} 需要单列 INTEGER PRIMARY KEY 用于内行身份标识"
            )
        return pk_columns[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "InstrumentedConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class SourceStat:
    inner_table: str
    inner_key: str
    outer_key: str
    shared_by: list[str]
    queries: int = 0
    batches_with_keys: int = 0
    batches_without_keys: int = 0
    distinct_probe_keys: int = 0
    inner_rows_returned: int = 0
    batch_log: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExecutionStats:
    outer_rows: int
    batch_size: int
    batches: int
    outer_scans: int
    counters: dict[str, int]
    sources: list[SourceStat]
    batch_records: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outer_rows": self.outer_rows,
            "batch_size": self.batch_size,
            "batch_count": self.batches,
            "outer_scans": self.outer_scans,
            "counters": self.counters,
            "growth_law": (
                "inner_batch_queries = 对每个来源，统计其出现 >=1 个非NULL关联键的批数；"
                "随批数与来源数增长，与外行总数无关（同批重复外键只探测一次）"
            ),
            "sources": [
                {
                    "inner_table": s.inner_table,
                    "inner_key": s.inner_key,
                    "outer_key": s.outer_key,
                    "shared_by": s.shared_by,
                    "queries": s.queries,
                    "batches_with_non_null_keys": s.batches_with_keys,
                    "batches_without_probe": s.batches_without_keys,
                    "distinct_probe_keys_overall": s.distinct_probe_keys,
                    "inner_rows_returned": s.inner_rows_returned,
                    "per_batch": s.batch_log,
                }
                for s in self.sources
            ],
            "batch_details": self.batch_records,
        }


@dataclass
class _Source:
    index: int
    inner_table: str
    inner_key: str
    outer_key: str
    pk: str
    value_columns: list[str]  # 该来源所有表达式需要的内表值列（去重保序）
    expressions: list[Expression]

    @property
    def select_columns(self) -> list[str]:
        # 主键用于标量多行报错的内行身份，关联键用于分组，其余为值列。
        return [self.pk, self.inner_key, *self.value_columns]


def _build_sources(plan: Plan, schema: Schema, conn: InstrumentedConnection) -> list[_Source]:
    grouped: dict[tuple[str, str, str], list[Expression]] = {}
    for expr in plan.expressions:
        key = (
            expr.inner_table,
            expr.correlate.inner,
            expr.correlate.outer,
        )
        grouped.setdefault(key, []).append(expr)

    sources: list[_Source] = []
    for index, ((table, inner_key, outer_key), exprs) in enumerate(grouped.items()):
        pk = conn.table_pk(table)
        schema.require_column(table, pk)
        values: list[str] = []
        for expr in exprs:
            if expr.inner_value is not None and expr.inner_value not in values:
                values.append(expr.inner_value)
        sources.append(
            _Source(
                index=index,
                inner_table=table,
                inner_key=inner_key,
                outer_key=outer_key,
                pk=pk,
                value_columns=values,
                expressions=exprs,
            )
        )
    return sources


def _distinct_non_null(rows: list[sqlite3.Row | tuple], key_index: int) -> list[Any]:
    """提取本批不同的非 NULL 关联键，保序去重；不使用会丢失信息的 set 语义结果。"""

    seen: list[Any] = []
    seen_check: set[Any] = set()  # 仅用于整数键的去重判断；NULL 在入口即被排除。
    for row in rows:
        value = row[key_index]
        if value is None:
            continue  # SQL 等号下 NULL 不匹配任何键（含 NULL 内键），不发探测。
        if value not in seen_check:
            seen_check.add(value)
            seen.append(value)
    return seen


def execute_plan(
    db_path: str | Path,
    plan: Plan,
    schema: Schema,
    output_path: str | Path,
) -> ExecutionStats:
    """执行 plan 并在全部成功后原子写出结果 JSON；任何错误都不发布部分结果。"""

    outer_columns = list(schema.columns(plan.outer_table).keys())
    row_id_index = outer_columns.index(plan.outer_order)

    with InstrumentedConnection(db_path) as conn:
        sources = _build_sources(plan, schema, conn)
        source_stats = [
            SourceStat(
                inner_table=s.inner_table,
                inner_key=s.inner_key,
                outer_key=s.outer_key,
                shared_by=[e.name for e in s.expressions],
            )
            for s in sources
        ]

        # 一次外表顺序扫描（本身也是参数化/白名单 SQL，统计为 1 次 outer scan）。
        outer_sql = (
            f"SELECT {', '.join(quote_ident(c) for c in outer_columns)} "
            f"FROM {quote_ident(plan.outer_table)} "
            f"ORDER BY {quote_ident(plan.outer_order)} ASC"
        )
        outer_rows = conn.execute(outer_sql).fetchall()
        outer_scans = 1

        # 列名 -> 行内下标，表达式求值时按名取列。
        col_index = {name: i for i, name in enumerate(outer_columns)}
        source_by_expr = {
            expr.name: source for source in sources for expr in source.expressions
        }
        results: list[dict[str, Any]] = []
        batch_records: list[dict[str, Any]] = []
        batch_count = 0

        for start in range(0, len(outer_rows), plan.batch_size):
            batch = outer_rows[start : start + plan.batch_size]
            batch_count += 1
            batch_record: dict[str, Any] = {
                "batch_index": batch_count - 1,
                "outer_row_ids": [row[row_id_index] for row in batch],
                "sources": [],
            }

            # 每来源每批至多一条参数化查询；结果分组后被该来源的全部表达式共享。
            grouped_rows: dict[int, dict[Any, list[tuple[Any, ...]]]] = {}
            for source, stat in zip(sources, source_stats):
                key_index = col_index[source.outer_key]
                keys = _distinct_non_null(batch, key_index)
                if not keys:
                    stat.batches_without_keys += 1
                    batch_record["sources"].append(
                        {
                            "inner_table": source.inner_table,
                            "query_issued": False,
                            "reason": "本批该来源只有 NULL 外键，SQL 等号下无匹配，跳过探测",
                            "distinct_keys": [],
                            "rows_returned": 0,
                        }
                    )
                    grouped_rows[source.index] = {}
                    continue

                placeholders = ", ".join("?" for _ in keys)
                selected = [quote_ident(c) for c in source.select_columns]
                sql = (
                    f"SELECT {', '.join(selected)} "
                    f"FROM {quote_ident(source.inner_table)} "
                    f"WHERE {quote_ident(source.inner_key)} IN ({placeholders}) "
                    f"ORDER BY {quote_ident(source.pk)} ASC"
                )
                fetched = conn.execute(sql, keys).fetchall()  # 值全部走绑定参数。

                groups: dict[Any, list[tuple[Any, ...]]] = {}
                inner_key_pos = 1  # select_columns: [pk, inner_key, *values]
                for fetched_row in fetched:
                    groups.setdefault(fetched_row[inner_key_pos], []).append(fetched_row)

                grouped_rows[source.index] = groups
                stat.queries += 1
                stat.batches_with_keys += 1
                stat.distinct_probe_keys += len(keys)
                stat.inner_rows_returned += len(fetched)
                stat.batch_log.append(
                    {
                        "batch_index": batch_count - 1,
                        "distinct_keys": list(keys),
                        "rows_returned": len(fetched),
                    }
                )
                batch_record["sources"].append(
                    {
                        "inner_table": source.inner_table,
                        "query_issued": True,
                        "distinct_keys": list(keys),
                        "rows_returned": len(fetched),
                    }
                )

            # 逐外行求值：行不折叠；表达式严格按 plan 声明顺序输出，三值写 true/false/null。
            for outer_row in batch:
                record = {
                    name: outer_row[col_index[name]] for name in outer_columns
                }
                for expr in plan.expressions:
                    source = source_by_expr[expr.name]
                    groups = grouped_rows[source.index]
                    outer_key_value = outer_row[col_index[source.outer_key]]
                    # 外键 NULL：SQL 等号永不命中（NULL = NULL 不是 TRUE），候选为空。
                    matched = (
                        groups.get(outer_key_value, [])
                        if outer_key_value is not None
                        else []
                    )
                    record[expr.name] = _evaluate(
                        expr,
                        source,
                        outer_row,
                        col_index,
                        matched,
                        outer_row_id=outer_row[row_id_index],
                    )
                results.append(record)

            batch_records.append(batch_record)

        inner_queries = sum(s.queries for s in source_stats)
        stats = ExecutionStats(
            outer_rows=len(outer_rows),
            batch_size=plan.batch_size,
            batches=batch_count,
            outer_scans=outer_scans,
            counters={
                "all_sql_statements": conn.all_statements,
                "data_statements": conn.data_statements,
                "select_statements": conn.select_statements,
                "outer_scans": outer_scans,
                "inner_batch_queries": inner_queries,
                "naive_n_plus_1_inner_queries": sum(
                    1
                    for row in outer_rows
                    for source in sources
                    if row[col_index[source.outer_key]] is not None
                ),
            },
            sources=source_stats,
            batch_records=batch_records,
        )

    # 全部批成功后才发布：临时文件 + 原子替换，错误路径上 output_path 不会被创建。
    _atomic_write_json(output_path, results)
    return stats


def _evaluate(
    expr: Expression,
    source: _Source,
    outer_row: tuple[Any, ...],
    col_index: dict[str, int],
    matched_rows: list[tuple[Any, ...]],
    outer_row_id: Any,
) -> Any:
    value_pos = {name: i for i, name in enumerate(source.select_columns)}

    if expr.op == "exists":
        # 行存在即 TRUE；内列是否为 NULL 与存在性无关。
        return predicates.exists_result(matched_rows)

    if expr.op == "scalar":
        pos = value_pos[expr.inner_value]
        candidates = [row[pos] for row in matched_rows]  # 保留 NULL 与重复，list 不折叠。
        try:
            return predicates.scalar_result(candidates)
        except predicates._ScalarCardinality as exc:
            pk_pos = value_pos[source.pk]
            raise CardinalityError(
                expression=expr.name,
                inner_table=source.inner_table,
                outer_row_id=outer_row_id,
                inner_row_ids=[row[pk_pos] for row in matched_rows],
                candidate_count=len(exc.candidates),
            ) from exc

    # IN / NOT IN：候选 list 保留 NULL 与重复，三值逻辑在 predicates 内显式分支。
    pos = value_pos[expr.inner_value]
    candidates = [row[pos] for row in matched_rows]
    outer_value = outer_row[col_index[expr.outer_value]]
    if expr.op == "in":
        return predicates.in_result(outer_value, candidates)
    return predicates.not_in_result(outer_value, candidates)


def _atomic_write_json(path: str | Path, payload: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        # mkstemp 默认 0600；报表结果设为常规 0644 便于下游用户读取。
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
