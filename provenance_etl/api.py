"""Public ``run`` / ``trace`` / ``verify`` entry points.

Each entry point re-evaluates the plan from the source files; ``verify`` is
therefore a genuine independent re-read, not a cache lookup.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from .engine import (
    EtlError,
    Execution,
    PlanError,
    Prov,
    _flatten_prov,
    _json,
    _value_token,
    execute,
)

__all__ = ["run", "trace", "verify", "EtlError", "PlanError", "SourceError"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_value(execution: Execution, table: str, row_id: Any, field: str) -> Any:
    """Fetch a business field from the original source row (re-derived lookup)."""

    for source in execution.sources.values():
        if source.table != table:
            continue
        for row in source.rows:
            if row.row_id == row_id:
                if field not in row.values:
                    raise EtlError(
                        f"source {table!r} row {row_id!r} has no field {field!r}"
                    )
                return row.values[field]
    raise EtlError(f"unknown source row {table!r}/{row_id!r}")


def _prov_json(prov: Prov) -> list[Any]:
    """JSON-safe rendering of a provenance identity (recursive)."""

    op = prov[0]
    if op == "source":
        return ["source", prov[1], prov[2]]
    if op == "union":
        return ["union", prov[1], _prov_json(prov[2])]
    if op == "join":
        return ["join", _prov_json(prov[1]), _prov_json(prov[2])]
    return ["group", *[_json(v) for v in prov[1:]]]


def _occurrences(prov: Prov) -> list[dict[str, Any]]:
    """Expand a provenance identity into one entry per source occurrence."""

    out: list[dict[str, Any]] = []
    for (table, row_id), path in _flatten_prov(prov):
        out.append(
            {
                "table": table,
                "row_id": row_id,
                "path": [{"op": op, "detail": detail} for op, detail in path],
            }
        )
    out.sort(key=_occurrence_order)
    return out


def _path_token(path: list[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple((entry["op"], entry["detail"]) for entry in path)


def _occurrence_order(occ: dict[str, Any]) -> tuple[Any, ...]:
    return (occ["table"], _value_token(occ["row_id"]), _path_token(occ["path"]))


def _record_key(occ: dict[str, Any]) -> tuple[Any, ...]:
    return (occ["table"], _value_token(occ["row_id"]))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run(plan_path: str | os.PathLike[str], output_dir: str | os.PathLike[str]) -> dict:
    """Evaluate the plan, write ``results.json`` and return the report."""

    execution = execute(plan_path)
    report = _build_report(execution)

    out_dir = os.fspath(output_dir)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return report


def _build_report(execution: Execution) -> dict:
    node = execution.nodes[execution.output_id]
    schema = execution.output_schema

    rows: list[dict[str, Any]] = []
    for row in execution.output_rows:
        item: dict[str, Any] = {
            "values": {field: _json(row.values.get(field)) for field, _ in schema},
            "provenance": _prov_json(row.prov),
        }
        # A group row aggregates many records; its per-row provenance lives in
        # trace/verify rather than in a replaced (and misleading) list.
        if node.op != "group":
            item["source_occurrences"] = _occurrences(row.prov)
        rows.append(item)

    return {
        "output": execution.output_id,
        "op": node.op,
        "schema": [{"field": field, "kind": kind} for field, kind in schema],
        "row_count": len(rows),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


def trace(plan_path: str | os.PathLike[str]) -> dict:
    """Return per-group / per-aggregate contribution provenance."""

    execution = execute(plan_path)
    node = execution.nodes[execution.output_id]
    if node.op != "group":
        raise EtlError(f"trace requires a group output node, got {node.op!r}")
    return {
        "output": execution.output_id,
        "keys": list(node.raw["keys"]),
        "aggregates": [agg["name"] for agg in node.raw["aggregates"]],
        "groups": _group_trace(execution),
    }


def _group_trace(execution: Execution) -> list[dict[str, Any]]:
    node = execution.nodes[execution.output_id]
    rows = execution.output_rows
    contribs = execution.group_contrib[execution.output_id]
    keys = node.raw["keys"]
    aggregates = node.raw["aggregates"]

    groups_out: list[dict[str, Any]] = []
    for row, per_agg in zip(rows, contribs):
        group_desc = {field: _json(row.values.get(field)) for field in keys}

        agg_out: dict[str, Any] = {}
        for agg in aggregates:
            name = agg["name"]
            items = per_agg[name]
            is_count = agg["fn"] == "count"
            field_name = None if is_count else agg["field"]

            contributions: list[dict[str, Any]] = []
            value_counts: dict[Any, int] = {}
            record_map: dict[tuple[str, Any], dict[str, Any]] = {}

            for member, value in items:
                # Per-field origin lets verification replay the exact source
                # column even when a join prefixed/duplicated the field name.
                origin = None if field_name is None else member.origins.get(field_name)
                source_value = None
                if origin is not None:
                    source_value = _source_value(execution, *origin)

                contribution_value = 1 if is_count else value
                occurrences = _occurrences(member.prov)
                contributions.append(
                    {
                        "prov": _prov_json(member.prov),
                        "value": contribution_value,
                        "origin": (
                            None
                            if origin is None
                            else {"table": origin[0], "row_id": origin[1], "field": origin[2]}
                        ),
                        "source_value": source_value,
                        "source_occurrences": occurrences,
                    }
                )

                # Build a per-source-record view with counts per operation path.
                # The same original record appearing through several union
                # branches / join pairings keeps every occurrence here.
                for occ in occurrences:
                    rk = _record_key(occ)
                    rec = record_map.get(rk)
                    if rec is None:
                        rec = record_map[rk] = {
                            "table": occ["table"],
                            "row_id": occ["row_id"],
                            "occurrences": 0,
                            "paths": {},
                        }
                    rec["occurrences"] += 1
                    token = _path_token(occ["path"])
                    path_entry = rec["paths"].get(token)
                    if path_entry is None:
                        path_entry = rec["paths"][token] = {
                            "path": occ["path"],
                            "occurrences": 0,
                        }
                    path_entry["occurrences"] += 1

                key = "__NULL__" if contribution_value is None else contribution_value
                value_counts[key] = value_counts.get(key, 0) + 1

            distinct = [
                {
                    "value": None if key == "__NULL__" else key,
                    "occurrences": value_counts[key],
                }
                for key in sorted(value_counts, key=_value_token)
            ]

            source_records = sorted(
                record_map.values(),
                key=lambda r: (r["table"], _value_token(r["row_id"])),
            )
            for rec in source_records:
                rec["paths"] = [
                    p
                    for p in sorted(
                        rec["paths"].values(),
                        key=lambda p: _path_token(p["path"]),
                    )
                ]

            # Ordered raw contribution list (repeats preserved) for exact replay.
            replay = [
                {
                    "prov": _prov_json(member.prov),
                    "origin": None if field_name is None else list(member.origins[field_name]),
                    "value": 1 if is_count else value,
                }
                for member, value in items
            ]

            agg_out[name] = {
                "fn": agg["fn"],
                "field": field_name,
                "value": _json(row.values.get(name)),
                "contribution_count": len(items),
                "distinct_value_count": len(value_counts),
                "contributing_values": distinct,
                "contributions": contributions,
                "source_records": source_records,
                "replay": replay,
            }
        groups_out.append({"group": group_desc, "aggregates": agg_out})
    return groups_out


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify(plan_path: str | os.PathLike[str]) -> dict:
    """Re-read sources, replay contributions, and compare against SQLite."""

    execution = execute(plan_path)
    node = execution.nodes[execution.output_id]
    if node.op != "group":
        raise EtlError(f"verify requires a group output node, got {node.op!r}")

    groups = _group_trace(execution)
    errors: list[str] = []
    replay_report: list[dict[str, Any]] = []

    for record in groups:
        per_agg: dict[str, Any] = {}
        for agg in node.raw["aggregates"]:
            name = agg["name"]
            info = record["aggregates"][name]
            is_count = agg["fn"] == "count"

            if is_count:
                replayed = len(info["replay"])
            else:
                total = 0
                saw_value = False
                for item in info["replay"]:
                    origin = item["origin"]
                    if origin is None:
                        errors.append(
                            f"group {record['group']} aggregate {name}: "
                            "contribution has no source origin"
                        )
                        continue
                    table, row_id, src_field = origin
                    value = _source_value(execution, table, row_id, src_field)
                    if value is None:
                        errors.append(
                            f"group {record['group']} aggregate {name}: NULL "
                            f"contribution {table}/{row_id} must be excluded from SUM"
                        )
                        continue
                    if isinstance(value, bool) or not isinstance(value, int):
                        errors.append(
                            f"group {record['group']} aggregate {name}: source "
                            f"value {value!r} is not an integer"
                        )
                        continue
                    if value != item["value"]:
                        errors.append(
                            f"group {record['group']} aggregate {name}: recorded "
                            f"contribution {item['value']!r} != source value "
                            f"{value!r} at {table}/{row_id}"
                        )
                    total += value
                    saw_value = True
                replayed = total if saw_value else None

            actual = info["value"]
            ok = actual == replayed
            if not ok:
                errors.append(
                    f"group {record['group']} aggregate {name}: actual {actual!r} "
                    f"!= replayed {replayed!r}"
                )
            per_agg[name] = {"actual": actual, "replayed": replayed, "ok": ok}
        replay_report.append({"group": record["group"], "aggregates": per_agg})

    # Independent SQLite translation, built from the same source rows.
    sqlite_rows, sql = _sqlite_equivalent(execution, node)
    sqlite_rows = sorted(sqlite_rows, key=lambda r: tuple(_sort_token(v) for v in r))
    comparable = _comparable_rows(groups, node)
    if comparable != sqlite_rows:
        errors.append(
            "SQLite comparison mismatch: "
            f"executor={comparable!r} sqlite={sqlite_rows!r}"
        )

    return {
        "output": execution.output_id,
        "ok": not errors,
        "errors": errors,
        "groups": replay_report,
        "sqlite_sql": sql,
        "sqlite": [dict(zip(_sqlite_columns(node), row)) for row in sqlite_rows],
    }


def _sqlite_columns(node) -> list[str]:
    return list(node.raw["keys"]) + [
        agg["name"] for agg in node.raw["aggregates"]
    ]


def _comparable_rows(groups: list[dict[str, Any]], node) -> list[tuple]:
    """Project executor groups into SQLite's result shape/order."""

    rows: list[tuple] = []
    for record in groups:
        row = tuple(record["group"][key] for key in node.raw["keys"])
        row += tuple(
            record["aggregates"][agg["name"]]["value"]
            for agg in node.raw["aggregates"]
        )
        rows.append(row)
    return sorted(rows, key=lambda r: tuple(_sort_token(v) for v in r))


def _sort_token(value: Any) -> tuple[int, Any]:
    return _value_token(value)


def _sqlite_equivalent(execution: Execution, output_node) -> tuple[list[tuple], str]:
    """Run an independent SQLite query over the same source rows."""

    connection = sqlite3.connect(":memory:")
    try:
        for node_id in execution.order:
            node = execution.nodes[node_id]
            if node.op != "source":
                continue
            source = execution.sources[node_id]
            table = _ident(source.table, "src")
            cols = list(source.fields)
            connection.execute(
                f"CREATE TABLE {table} ({', '.join(_ident(c, 'col') for c in cols)})"
            )
            if source.rows:
                placeholders = ", ".join("?" for _ in cols)
                connection.executemany(
                    f"INSERT INTO {table} VALUES ({placeholders})",
                    [tuple(row.values.get(c) for c in cols) for row in source.rows],
                )

        counter = [0]

        def alias(_ref: str) -> str:
            # A fresh alias per use site: a self-join references the same CTE
            # twice and would otherwise collide.
            counter[0] += 1
            return f"a{counter[0]}"

        sql = _build_sql(execution, output_node, alias)
        cursor = connection.execute(sql)
        return [tuple(row) for row in cursor.fetchall()], sql
    finally:
        connection.close()


def _ident(name: str, prefix: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(name))
    if not safe or safe[0].isdigit():
        safe = "_" + safe
    return f'"{prefix}_{safe}"'


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        raise PlanError("boolean filter values are not supported")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise PlanError(f"unsupported literal {value!r}")


def _build_sql(execution: Execution, output_node, alias) -> str:
    """Translate the restricted DAG into SQLite SQL (one CTE per node).

    Every node's CTE exposes its columns under one uniform name per logical
    schema field (``out_<field>``).  ``SELECT *`` therefore keeps names stable
    through filters/unions, and downstream nodes always reference ``out_*``.
    """

    tables = {
        node_id: _ident(source.table, "src")
        for node_id, source in execution.sources.items()
    }
    ctes: list[str] = []

    def ref(node_id: str) -> str:
        return _ident(node_id, "n")

    def out_col(name: str) -> str:
        return _ident(name, "out")

    emitted: dict[str, str] = {}

    def emit(node_id: str) -> str:
        if node_id in emitted:
            return emitted[node_id]
        node = execution.nodes[node_id]
        name = ref(node_id)

        if node.op == "source":
            source = execution.sources[node_id]
            projection = ", ".join(
                f"{_ident(c, 'col')} AS {out_col(c)}" for c in source.fields
            )
            ctes.append(f"{name} AS (SELECT {projection} FROM {tables[node_id]})")
            emitted[node_id] = name
            return name

        if node.op == "filter":
            body = emit(node.inputs[0])
            field = out_col(node.raw["field"])
            value = node.raw["value"]
            if value is None:
                predicate = "0"  # comparison with NULL is never true
            elif node.raw["cmp"] == "eq":
                predicate = f"{field} = {_literal(value)}"
            else:
                predicate = f"{field} > {_literal(value)}"
            ctes.append(f"{name} AS (SELECT * FROM {body} WHERE {predicate})")
            emitted[node_id] = name
            return name

        if node.op == "union_all":
            left = emit(node.inputs[0])
            right = emit(node.inputs[1])
            # Project by name in the left branch's order: branches may list the
            # same fields in different physical order.
            cols = [c for c, _ in execution.schemas[node_id]]
            projection = ", ".join(out_col(c) for c in cols)
            ctes.append(
                f"{name} AS (SELECT {projection} FROM {left} "
                f"UNION ALL SELECT {projection} FROM {right})"
            )
            emitted[node_id] = name
            return name

        if node.op == "inner_join":
            left = emit(node.inputs[0])
            right = emit(node.inputs[1])
            la, ra = alias(left), alias(right)
            lcols = [c for c, _ in execution.schemas[node.inputs[0]]]
            rcols = [c for c, _ in execution.schemas[node.inputs[1]]]
            select = ", ".join(
                [
                    f"{la}.{out_col(c)} AS {out_col(node.raw['left_prefix'] + c)}"
                    for c in lcols
                ]
                + [
                    f"{ra}.{out_col(c)} AS {out_col(node.raw['right_prefix'] + c)}"
                    for c in rcols
                ]
            )
            ctes.append(
                f"{name} AS (SELECT {select} FROM {left} AS {la} "
                f"INNER JOIN {right} AS {ra} "
                f"ON {la}.{out_col(node.raw['left_key'])} = "
                f"{ra}.{out_col(node.raw['right_key'])})"
            )
            emitted[node_id] = name
            return name

        # group
        body = emit(node.inputs[0])
        keys = node.raw["keys"]
        select = [f"{out_col(k)} AS {out_col(k)}" for k in keys]
        for agg in node.raw["aggregates"]:
            if agg["fn"] == "count":
                select.append(f"COUNT(*) AS {out_col(agg['name'])}")
            else:
                select.append(
                    f"SUM({out_col(agg['field'])}) AS {out_col(agg['name'])}"
                )
        group_by = (
            " GROUP BY " + ", ".join(out_col(k) for k in keys) if keys else ""
        )
        ctes.append(f"{name} AS (SELECT {', '.join(select)} FROM {body}{group_by})")
        emitted[node_id] = name
        return name

    final = emit(output_node.id)
    columns = _sqlite_columns(output_node)
    projection = ", ".join(out_col(c) for c in columns)
    return f"WITH {', '.join(ctes)} SELECT {projection} FROM {final}"
