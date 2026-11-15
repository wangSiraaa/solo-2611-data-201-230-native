"""Typed JSON plan 的加载与白名单校验。

plan 是结构化数据（枚举 op + 名称引用），本模块从不把 plan 文本当 SQL 解析。
所有表名、列名必须在 schema 白名单中出现；值一律在 engine 里用绑定参数传入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 顶层允许键与表达式允许键固定，出现未知键直接拒绝，避免悄悄忽略声明。
_TOP_KEYS = {"outer_table", "outer_order", "batch_size", "expressions"}
_EXPR_KEYS = {
    "as",
    "op",
    "inner_table",
    "correlate",
    "outer_value",
    "inner_value",
}
_OPS = {"exists", "scalar", "in", "not_in"}
_MAX_INNER_TABLES = 2


class PlanError(ValueError):
    """plan 或 schema 不满足受限执行器的契约。"""


def _read_json(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlanError(f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"不是合法 JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError(f"{path}: 顶层必须是 JSON 对象")
    return data


@dataclass(frozen=True)
class Schema:
    """schema 白名单：表 -> {列: 逻辑类型}，类型带 '?' 表示可空。"""

    tables: dict[str, dict[str, str]]

    @classmethod
    def load(cls, path: str | Path) -> "Schema":
        raw = _read_json(path)
        tables: dict[str, dict[str, str]] = {}
        if not raw:
            raise PlanError("schema 为空")
        for table, columns in raw.items():
            if not isinstance(table, str) or not table:
                raise PlanError("schema 表名必须是非空字符串")
            if not isinstance(columns, dict) or not columns:
                raise PlanError(f"schema 表 {table!r} 必须至少声明一列")
            seen: set[str] = set()
            for column, typ in columns.items():
                if not isinstance(column, str) or not column:
                    raise PlanError(f"表 {table!r} 存在非法列名: {column!r}")
                if column in seen:
                    raise PlanError(f"表 {table!r} 列 {column!r} 重复声明")
                if typ not in ("integer", "integer?", "text", "text?"):
                    raise PlanError(
                        f"表 {table!r} 列 {column!r} 类型 {typ!r} 不受支持"
                    )
                seen.add(column)
            tables[table] = dict(columns)
        return cls(tables=tables)

    def require_table(self, table: str) -> None:
        if table not in self.tables:
            raise PlanError(f"表 {table!r} 不在 schema 白名单中")

    def require_column(self, table: str, column: str) -> None:
        self.require_table(table)
        if column not in self.tables[table]:
            raise PlanError(
                f"列 {table}.{column} 不在 schema 白名单中"
            )

    def require_integer(self, table: str, column: str, where: str) -> None:
        self.require_column(table, column)
        if not self.tables[table][column].startswith("integer"):
            raise PlanError(f"{where}: {table}.{column} 必须是 integer 关联键")

    def columns(self, table: str) -> dict[str, str]:
        return self.tables[table]


@dataclass(frozen=True)
class Correlate:
    outer: str
    inner: str


@dataclass(frozen=True)
class Expression:
    name: str
    op: str
    inner_table: str
    correlate: Correlate
    outer_value: str | None = None
    inner_value: str | None = None


@dataclass(frozen=True)
class Plan:
    outer_table: str
    outer_order: str
    batch_size: int
    expressions: tuple[Expression, ...] = field(default_factory=tuple)
    path: str | None = None

    @property
    def inner_tables(self) -> tuple[str, ...]:
        seen: list[str] = []
        for expr in self.expressions:
            if expr.inner_table not in seen:
                seen.append(expr.inner_table)
        return tuple(seen)


def load_schema(path: str | Path) -> "Schema":
    return Schema.load(path)


def load_plan(path: str | Path, schema: "Schema") -> Plan:
    """读取并校验 plan；任何越权引用或结构问题都在执行前失败。"""

    raw = _read_json(path)
    extra = set(raw) - _TOP_KEYS
    if extra:
        raise PlanError(f"plan 含未知顶层键: {sorted(extra)}")

    for key in ("outer_table", "outer_order", "batch_size", "expressions"):
        if key not in raw:
            raise PlanError(f"plan 缺少必填键: {key}")

    outer_table = raw["outer_table"]
    if not isinstance(outer_table, str):
        raise PlanError("outer_table 必须是字符串")
    schema.require_table(outer_table)

    outer_order = raw["outer_order"]
    if not isinstance(outer_order, str):
        raise PlanError("outer_order 必须是字符串")
    schema.require_column(outer_table, outer_order)
    schema.require_integer(outer_table, outer_order, "outer_order")

    batch_size = raw["batch_size"]
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise PlanError("batch_size 必须是正整数")

    raw_exprs = raw["expressions"]
    if not isinstance(raw_exprs, list) or not raw_exprs:
        raise PlanError("expressions 必须是非空数组")

    names: set[str] = set()
    exprs: list[Expression] = []
    outer_key_by_table: dict[str, str] = {}
    for index, item in enumerate(raw_exprs):
        where = f"expressions[{index}]"
        if not isinstance(item, dict):
            raise PlanError(f"{where} 必须是对象")
        extra = set(item) - _EXPR_KEYS
        if extra:
            raise PlanError(f"{where} 含未知键: {sorted(extra)}")

        name = item.get("as")
        op = item.get("op")
        if not isinstance(name, str) or not name:
            raise PlanError(f"{where}: as 必须是非空字符串")
        if name in names:
            raise PlanError(f"{where}: 表达式名 {name!r} 重复")
        if name in schema.columns(outer_table):
            raise PlanError(
                f"{where}: 表达式名 {name!r} 与外表原列冲突，不能覆盖原列"
            )
        names.add(name)
        if op not in _OPS:
            raise PlanError(f"{where}: op {op!r} 不受支持，允许 {sorted(_OPS)}")

        inner_table = item.get("inner_table")
        if not isinstance(inner_table, str):
            raise PlanError(f"{where}: inner_table 必须是字符串")
        schema.require_table(inner_table)
        if inner_table == outer_table:
            raise PlanError(f"{where}: 内表不能与外表相同")

        correlate = item.get("correlate")
        if not isinstance(correlate, dict) or set(correlate) != {"outer", "inner"}:
            raise PlanError(
                f"{where}: correlate 必须且只能包含 outer / inner 两个键"
            )
        outer_key = correlate["outer"]
        inner_key = correlate["inner"]
        if not isinstance(outer_key, str) or not isinstance(inner_key, str):
            raise PlanError(f"{where}: correlate 列名必须是字符串")
        # 题定：一个整数等值关联键。
        schema.require_integer(outer_table, outer_key, f"{where} 外表关联键")
        schema.require_integer(inner_table, inner_key, f"{where} 内表关联键")
        previous = outer_key_by_table.setdefault(inner_table, outer_key)
        if previous != outer_key:
            raise PlanError(
                f"{where}: 同一内表 {inner_table!r} 的外表关联键必须一致"
            )

        outer_value = item.get("outer_value")
        inner_value = item.get("inner_value")
        if op in ("in", "not_in"):
            if not isinstance(outer_value, str) or not isinstance(inner_value, str):
                raise PlanError(f"{where}: {op} 需要 outer_value 与 inner_value")
            schema.require_column(outer_table, outer_value)
            schema.require_column(inner_table, inner_value)
        elif op == "scalar":
            if outer_value is not None:
                raise PlanError(f"{where}: scalar 不接受 outer_value")
            if not isinstance(inner_value, str):
                raise PlanError(f"{where}: scalar 需要 inner_value")
            schema.require_column(inner_table, inner_value)
        else:  # exists
            if outer_value is not None or inner_value is not None:
                raise PlanError(f"{where}: exists 不接受 outer_value / inner_value")

        exprs.append(
            Expression(
                name=name,
                op=op,
                inner_table=inner_table,
                correlate=Correlate(outer=outer_key, inner=inner_key),
                outer_value=outer_value,
                inner_value=inner_value,
            )
        )

    inner_tables = list(dict.fromkeys(e.inner_table for e in exprs))
    if len(inner_tables) > _MAX_INNER_TABLES:
        raise PlanError(
            f"plan 引用了 {len(inner_tables)} 张内表，受限执行器至多允许 "
            f"{_MAX_INNER_TABLES} 张"
        )

    return Plan(
        outer_table=outer_table,
        outer_order=outer_order,
        batch_size=batch_size,
        expressions=tuple(exprs),
        path=str(path),
    )
