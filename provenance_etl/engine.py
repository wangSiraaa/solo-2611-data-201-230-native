"""Core engine: plan validation, evaluation, provenance, trace and verify.

Design notes
------------
Every intermediate relation is an ordered list of :class:`Row` objects.  A
``Row`` is an immutable business-value mapping plus a *provenance identity*
(``prov``).  The identity describes how the row was derived:

    source     (table, row_id)
    filter     identity of the input row (unchanged)
    union_all  (branch, identity of the contributing row)
    join       (left identity, right identity) -- an ordered pair, so the same
               left record joined to many right records keeps distinct ids and
               the contribution count is preserved.
    group      (groups are re-derived from their members; group-level tracing
               lists each contributing upstream row explicitly)

Provenance is *never* deduplicated.  Two source rows with identical business
values stay two rows with different identities; the same source row re-entering
through two union branches stays two rows (``("L", id)`` / ``("R", id)``); and a
join against two matching right rows produces two rows per left row.

NULL follows SQL semantics: any comparison with NULL is false, NULL join keys
never match, NULL group keys form one group, SUM ignores NULL and is NULL for
an all-NULL group, COUNT(*) counts every row.

Only in-memory execution of small provenanced relations is implemented.  There
is no SQL / expression parser, no LEFT JOIN, and no counterfactual reasoning.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EtlError(Exception):
    """Base class for all errors raised by the executor."""


class PlanError(EtlError):
    """Malformed, unsupported or cyclic plan."""


class SourceError(EtlError):
    """Malformed source table."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# A provenance identity is a hashable tuple.  Kept deliberately simple:
#   ("source", table, row_id)
#   ("union", branch, identity)
#   ("join", left_identity, right_identity)
Prov = tuple


@dataclass(frozen=True, slots=True)
class SourceRow:
    table: str
    row_id: Any
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Row:
    values: dict[str, Any]
    prov: Prov
    # field name -> (table, row_id, source field name); lets verification
    # replay any derived field back to the exact source column it came from.
    origins: dict[str, tuple[str, Any, str]] = field(default_factory=dict)

    def keyed(self, prefix: str) -> dict[str, Any]:
        """Return business values with every field prefixed (join operator)."""
        return {prefix + name: value for name, value in self.values.items()}

    def keyed_origins(self, prefix: str) -> dict[str, tuple[str, Any, str]]:
        return {prefix + name: origin for name, origin in self.origins.items()}


def _json(value: Any) -> Any:
    """Convert an internal value to its JSON form (sentinel -> None)."""
    return None if value is _NULL else value


def _value_token(value: Any) -> tuple[int, Any]:
    """Total order over int/str/NULL that never mixes Python types."""
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, int):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    return (2, "")


# Sentinel used only while grouping (interning NULL keeps tuples hashable).
class _Null:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return "NULL"

    def __bool__(self) -> bool:
        return False


_NULL = _Null()


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------

_OPS = {"source", "filter", "union_all", "inner_join", "group"}
_CMPS = {"eq", "gt"}
_FNS = {"count", "sum"}


def _unify_kind(kinds: Iterable[str]) -> str:
    """Resolve the common value domain of several fields.

    All source fields are already restricted to int/str/NULL, so a union or
    join of differently-typed fields still yields "any": the executor never
    needs to reinterpret values, only to decide whether SUM is legal.
    """

    distinct = set(kinds)
    if distinct <= {"int"}:
        return "int"
    if distinct <= {"str"}:
        return "str"
    return "any"


@dataclass
class Node:
    id: str
    op: str
    raw: dict[str, Any]
    inputs: list[str] = field(default_factory=list)


def _require(raw: dict[str, Any], key: str, node_id: str, expected: type | None = None):
    if key not in raw:
        raise PlanError(f"node {node_id!r}: missing required key {key!r}")
    value = raw[key]
    if expected is not None and not isinstance(value, expected):
        raise PlanError(
            f"node {node_id!r}: key {key!r} must be {expected.__name__}, "
            f"got {type(value).__name__}"
        )
    return value


def _require_str(raw: dict[str, Any], key: str, node_id: str) -> str:
    value = _require(raw, key, node_id, str)
    if not value:
        raise PlanError(f"node {node_id!r}: key {key!r} must be non-empty")
    return value


def load_plan(plan_path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with open(plan_path, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
    except FileNotFoundError as exc:
        raise PlanError(f"plan not found: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise PlanError("plan must be a JSON object")
    return plan


def parse_plan(plan: dict[str, Any]) -> tuple[dict[str, Node], str, list[str]]:
    """Parse and structurally validate the plan.

    Returns ``(nodes_by_id, output_id, topological_order)``.
    """

    if "nodes" not in plan:
        raise PlanError("plan is missing 'nodes'")
    if "output" not in plan:
        raise PlanError("plan is missing 'output'")
    raw_nodes = plan["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise PlanError("plan 'nodes' must be a non-empty list")

    nodes: dict[str, Node] = {}
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise PlanError(f"node at index {index} must be an object")
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise PlanError(f"node at index {index}: 'id' must be a non-empty string")
        if node_id in nodes:
            raise PlanError(f"duplicate node id {node_id!r}")
        op = raw.get("op")
        if not isinstance(op, str) or op not in _OPS:
            raise PlanError(f"node {node_id!r}: unsupported op {op!r}")

        if op == "source":
            _require_str(raw, "table", node_id)
            _require_str(raw, "path", node_id)
            inputs: list[str] = []
        elif op == "filter":
            inputs = [_require_str(raw, "input", node_id)]
            _require_str(raw, "field", node_id)
            cmp = _require_str(raw, "cmp", node_id)
            if cmp not in _CMPS:
                raise PlanError(f"node {node_id!r}: unsupported cmp {cmp!r}")
            _require(raw, "value", node_id)
            value = raw["value"]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, str))
            ):
                raise PlanError(
                    f"node {node_id!r}: filter value must be int, str or null"
                )
        elif op == "union_all":
            inputs = [
                _require_str(raw, "left", node_id),
                _require_str(raw, "right", node_id),
            ]
        elif op == "inner_join":
            inputs = [
                _require_str(raw, "left", node_id),
                _require_str(raw, "right", node_id),
            ]
            _require_str(raw, "left_key", node_id)
            _require_str(raw, "right_key", node_id)
            _require_str(raw, "left_prefix", node_id)
            _require_str(raw, "right_prefix", node_id)
        else:  # group
            inputs = [_require_str(raw, "input", node_id)]
            keys = _require(raw, "keys", node_id, list)
            for key in keys:
                if not isinstance(key, str) or not key:
                    raise PlanError(f"node {node_id!r}: group keys must be strings")
            if len(set(keys)) != len(keys):
                raise PlanError(f"node {node_id!r}: duplicate group key")
            aggregates = _require(raw, "aggregates", node_id, list)
            if not aggregates:
                raise PlanError(f"node {node_id!r}: 'aggregates' must be non-empty")
            seen: set[str] = set(keys)
            for agg in aggregates:
                if not isinstance(agg, dict):
                    raise PlanError(f"node {node_id!r}: aggregate must be an object")
                name = _require_str(agg, "name", node_id)
                if name in seen:
                    raise PlanError(
                        f"node {node_id!r}: duplicate aggregate/group-key name {name!r}"
                    )
                seen.add(name)
                fn = _require_str(agg, "fn", node_id)
                if fn not in _FNS:
                    raise PlanError(f"node {node_id!r}: unsupported aggregate fn {fn!r}")
                if fn == "count":
                    if "field" in agg and agg["field"] not in (None, "*"):
                        raise PlanError(
                            f"node {node_id!r}: COUNT only supports COUNT(*)"
                        )
                else:  # sum
                    _require_str(agg, "field", node_id)

        nodes[node_id] = Node(id=node_id, op=op, raw=raw, inputs=inputs)

    output = plan["output"]
    if not isinstance(output, str) or output not in nodes:
        raise PlanError(f"'output' must reference an existing node, got {output!r}")

    for node in nodes.values():
        for dep in node.inputs:
            if dep not in nodes:
                raise PlanError(
                    f"node {node.id!r}: input {dep!r} does not exist"
                )

    order = _topological_order(nodes)
    # Group nodes are terminal: aggregation destroys row-level identities, so a
    # group result cannot feed another operator while keeping exact provenance.
    for node in nodes.values():
        for dep in node.inputs:
            if nodes[dep].op == "group":
                raise PlanError(
                    f"node {node.id!r}: group node {dep!r} cannot be used as "
                    f"input (group output is terminal)"
                )
    return nodes, output, order


def _topological_order(nodes: dict[str, Node]) -> list[str]:
    """Kahn-style topological sort; detects cycles and self references."""

    indegree = {node_id: 0 for node_id in nodes}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node in nodes.values():
        for dep in node.inputs:
            indegree[node.id] += 1
            dependents[dep].append(node.id)

    ready = [node_id for node_id in nodes if indegree[node_id] == 0]
    order: list[str] = []
    # Deterministic ordering independent of input order in ties.
    ready.sort()
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for child in sorted(dependents[node_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()

    if len(order) != len(nodes):
        stuck = sorted(node_id for node_id in nodes if node_id not in set(order))
        raise PlanError(f"plan has a cycle among nodes: {stuck}")
    return order


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------


def _schemas(
    nodes: dict[str, Node], order: list[str]
) -> tuple[dict[str, tuple[tuple[str, str], ...]], dict[str, SourceTable]]:
    """Infer each node's schema and load/inspect every source table."""

    schemas: dict[str, tuple[tuple[str, str], ...]] = {}
    sources: dict[str, SourceTable] = {}

    for node_id in order:
        node = nodes[node_id]
        if node.op == "source":
            table = _read_source(node.raw["table"], node.raw["path"])
            sources[node_id] = table
            schemas[node_id] = tuple(
                (name, table.field_kind(name)) for name in table.fields
            )
            continue

        if node.op == "filter":
            schema = schemas[node.inputs[0]]
            field = node.raw["field"]
            if field not in dict(schema):
                raise PlanError(
                    f"node {node_id!r}: unknown filter field {field!r}"
                )
            value = node.raw["value"]
            if value is not None:
                kind = dict(schema)[field]
                if kind != "any":
                    value_kind = "int" if isinstance(value, int) else "str"
                    if value_kind != kind:
                        raise PlanError(
                            f"node {node_id!r}: filter value {value!r} incompatible "
                            f"with field {field!r} ({kind})"
                        )
            schemas[node_id] = schema
            continue

        if node.op == "union_all":
            left = schemas[node.inputs[0]]
            right = schemas[node.inputs[1]]
            left_names = [name for name, _ in left]
            right_names = [name for name, _ in right]
            if set(left_names) != set(right_names) or len(left_names) != len(right_names):
                raise PlanError(
                    f"node {node_id!r}: union_all requires identical business "
                    f"fields; left={left_names} right={right_names}"
                )
            right_kinds = dict(right)
            kinds = [_unify_kind((kind, right_kinds[name])) for name, kind in left]
            schemas[node_id] = tuple(zip(left_names, kinds))
            continue

        if node.op == "inner_join":
            left = schemas[node.inputs[0]]
            right = schemas[node.inputs[1]]
            left_key = node.raw["left_key"]
            right_key = node.raw["right_key"]
            if left_key not in dict(left):
                raise PlanError(
                    f"node {node_id!r}: unknown left_key {left_key!r}"
                )
            if right_key not in dict(right):
                raise PlanError(
                    f"node {node_id!r}: unknown right_key {right_key!r}"
                )
            lprefix = node.raw["left_prefix"]
            rprefix = node.raw["right_prefix"]
            columns: list[tuple[str, str]] = []
            seen: set[str] = set()
            for name, kind in left:
                out = lprefix + name
                if out in seen:
                    raise PlanError(
                        f"node {node_id!r}: duplicate joined field {out!r}"
                    )
                seen.add(out)
                columns.append((out, kind))
            for name, kind in right:
                out = rprefix + name
                if out in seen:
                    raise PlanError(
                        f"node {node_id!r}: duplicate joined field {out!r}"
                    )
                seen.add(out)
                columns.append((out, kind))
            schemas[node_id] = tuple(columns)
            continue

        # group
        schema = schemas[node.inputs[0]]
        kinds = dict(schema)
        columns: list[tuple[str, str]] = []
        for key in node.raw["keys"]:
            if key not in kinds:
                raise PlanError(f"node {node_id!r}: unknown group key {key!r}")
            columns.append((key, kinds[key]))
        for agg in node.raw["aggregates"]:
            if agg["fn"] == "count":
                columns.append((agg["name"], "int"))
                continue
            field_name = agg["field"]
            if field_name not in kinds:
                raise PlanError(
                    f"node {node_id!r}: unknown aggregate field {field_name!r}"
                )
            if kinds[field_name] != "any" and kinds[field_name] != "int":
                raise PlanError(
                    f"node {node_id!r}: SUM requires an integer field, "
                    f"{field_name!r} is {kinds[field_name]}"
                )
            columns.append((agg["name"], "int"))
        schemas[node_id] = tuple(columns)

    # Provenance identifies a source record by (table, row_id); that pair must
    # be globally unique even if several source nodes share a table name.
    seen_records: dict[tuple[str, Any], str] = {}
    for node_id, table in sources.items():
        for row in table.rows:
            record = (table.table, row.row_id)
            if record in seen_records:
                raise SourceError(
                    f"duplicate source record {table.table!r}/{row.row_id!r} "
                    f"across source nodes {seen_records[record]!r} and {node_id!r}"
                )
            seen_records[record] = node_id

    return schemas, sources


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------


@dataclass
class SourceTable:
    table: str
    path: str
    fields: tuple[str, ...]
    rows: list[SourceRow]

    def field_kind(self, name: str) -> str:
        """Infer a business domain from the observed values.

        "int" / "str" when every non-NULL value belongs to that domain, "any"
        for mixed columns or columns that are entirely NULL (unknown).
        """

        seen_int = seen_str = False
        for row in self.rows:
            value = row.values.get(name)
            if value is None:
                continue
            if isinstance(value, int):
                seen_int = True
            else:
                seen_str = True
        if seen_int and not seen_str:
            return "int"
        if seen_str and not seen_int:
            return "str"
        return "any"


def _read_source(table: str, path: str) -> SourceTable:
    try:
        handle = open(path, "r", encoding="utf-8")
    except FileNotFoundError as exc:
        raise SourceError(f"source table {table!r}: file not found: {path}") from exc
    except OSError as exc:
        raise SourceError(f"source table {table!r}: cannot read {path}: {exc}") from exc

    rows: list[SourceRow] = []
    fields: tuple[str, ...] | None = None
    seen_ids: dict[Any, int] = {}

    with handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SourceError(
                    f"source table {table!r} line {lineno}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(obj, dict):
                raise SourceError(
                    f"source table {table!r} line {lineno}: expected a JSON object"
                )
            if "row_id" not in obj:
                raise SourceError(
                    f"source table {table!r} line {lineno}: missing 'row_id'"
                )
            row_id = obj["row_id"]
            if row_id is None or isinstance(row_id, bool) or not isinstance(
                row_id, (int, str)
            ):
                raise SourceError(
                    f"source table {table!r} line {lineno}: 'row_id' must be int or str"
                )
            if row_id in seen_ids:
                raise SourceError(
                    f"source table {table!r}: duplicate row_id {row_id!r} "
                    f"(lines {seen_ids[row_id]} and {lineno})"
                )
            seen_ids[row_id] = lineno

            row_fields = tuple(obj.keys())
            if fields is None:
                fields = row_fields
            else:
                # Physical column order is irrelevant; only the field set matters.
                if set(row_fields) != set(fields) or len(row_fields) != len(fields):
                    raise SourceError(
                        f"source table {table!r} line {lineno}: field set differs "
                        f"from line 1: {sorted(row_fields)}"
                    )

            values: dict[str, Any] = {}
            for name in row_fields:
                value = obj[name]
                if name == "row_id":
                    values[name] = value
                    continue
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, str))
                ):
                    raise SourceError(
                        f"source table {table!r} line {lineno}: field {name!r} must be "
                        f"int, str or null"
                    )
                values[name] = value
            rows.append(SourceRow(table=table, row_id=row_id, values=values))

    if fields is None:
        raise SourceError(f"source table {table!r}: file is empty ({path})")

    return SourceTable(table=table, path=path, fields=fields, rows=rows)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class Execution:
    plan_path: str
    plan: dict[str, Any]
    nodes: dict[str, Node]
    output_id: str
    order: list[str]
    schemas: dict[str, tuple[tuple[str, str], ...]]
    sources: dict[str, SourceTable]
    results: dict[str, list[Row]]
    # node id -> per output row -> aggregate name -> [(contributing row, value)]
    group_contrib: dict[str, list[dict[str, list[tuple[Row, Any]]]]]

    @property
    def output_rows(self) -> list[Row]:
        return self.results[self.output_id]

    @property
    def output_schema(self) -> tuple[tuple[str, str], ...]:
        return self.schemas[self.output_id]


def execute(plan_path: str | os.PathLike[str]) -> Execution:
    plan = load_plan(plan_path)
    nodes, output_id, order = parse_plan(plan)
    schemas, sources = _schemas(nodes, order)

    results: dict[str, list[Row]] = {}
    group_contrib: dict[str, dict[str, list[tuple[Row, Any]]]] = {}

    for node_id in order:
        node = nodes[node_id]
        if node.op == "source":
            table = sources[node_id]
            ordered = sorted(table.rows, key=lambda r: _value_token(r.row_id))
            results[node_id] = [
                Row(
                    values=dict(source.values),
                    prov=("source", table.table, source.row_id),
                    origins={
                        name: (table.table, source.row_id, name)
                        for name in table.fields
                    },
                )
                for source in ordered
            ]
        elif node.op == "filter":
            results[node_id] = _eval_filter(node, results[node.inputs[0]])
        elif node.op == "union_all":
            results[node_id] = _eval_union(
                results[node.inputs[0]], results[node.inputs[1]]
            )
        elif node.op == "inner_join":
            results[node_id] = _eval_join(node, results[node.inputs[0]], results[node.inputs[1]])
        else:
            rows, contrib = _eval_group(node, results[node.inputs[0]])
            results[node_id] = rows
            group_contrib[node_id] = contrib

    return Execution(
        plan_path=str(plan_path),
        plan=plan,
        nodes=nodes,
        output_id=output_id,
        order=order,
        schemas=schemas,
        sources=sources,
        results=results,
        group_contrib=group_contrib,
    )


def _is_null(value: Any) -> bool:
    return value is None


def _cmp_holds(value: Any, cmp: str, constant: Any) -> bool:
    if _is_null(value) or _is_null(constant):
        return False
    if cmp == "eq":
        return value == constant
    # gt -- incomparable types simply do not match
    try:
        return value > constant
    except TypeError:
        return False


def _eval_filter(node: Node, rows: list[Row]) -> list[Row]:
    field = node.raw["field"]
    cmp = node.raw["cmp"]
    constant = node.raw["value"]
    kept: list[Row] = []
    for row in rows:
        if _cmp_holds(row.values.get(field), cmp, constant):
            kept.append(row)
    return kept


def _eval_union(left: list[Row], right: list[Row]) -> list[Row]:
    """UNION ALL: keep both branches and every occurrence in each branch."""
    out: list[Row] = []
    for row in left:
        out.append(
            Row(
                values=dict(row.values),
                prov=("union", "L", row.prov),
                origins=dict(row.origins),
            )
        )
    for row in right:
        out.append(
            Row(
                values=dict(row.values),
                prov=("union", "R", row.prov),
                origins=dict(row.origins),
            )
        )
    return out


def _eval_join(node: Node, left: list[Row], right: list[Row]) -> list[Row]:
    left_key = node.raw["left_key"]
    right_key = node.raw["right_key"]
    lprefix = node.raw["left_prefix"]
    rprefix = node.raw["right_prefix"]

    index: dict[Any, list[Row]] = {}
    for rrow in right:
        key = rrow.values.get(right_key)
        if _is_null(key):
            continue
        index.setdefault(key, []).append(rrow)

    out: list[Row] = []
    for lrow in left:
        key = lrow.values.get(left_key)
        if _is_null(key):
            continue
        matches = index.get(key)
        if not matches:
            continue
        lvalues = lrow.keyed(lprefix)
        lorigins = lrow.keyed_origins(lprefix)
        for rrow in matches:
            values = dict(lvalues)
            values.update(rrow.keyed(rprefix))
            origins = dict(lorigins)
            origins.update(rrow.keyed_origins(rprefix))
            out.append(
                Row(values=values, prov=("join", lrow.prov, rrow.prov), origins=origins)
            )
    return out


def _norm_key(value: Any) -> Any:
    return _NULL if value is None else value


def _eval_group(
    node: Node, rows: list[Row]
) -> tuple[list[Row], list[dict[str, list[tuple[Row, Any]]]]]:
    keys: list[str] = node.raw["keys"]
    aggregates = node.raw["aggregates"]

    groups: dict[tuple[Any, ...], list[int]] = {}
    for index, row in enumerate(rows):
        key = tuple(_norm_key(row.values.get(field)) for field in keys)
        groups.setdefault(key, []).append(index)
    # A keyless group is a single global aggregate, present even with no rows.
    if not keys:
        groups.setdefault((), [])

    # Deterministic group order, independent of physical input row order.
    frozen: list[tuple[Any, ...]] = sorted(
        groups, key=lambda key: tuple(_value_token(v) for v in key)
    )

    out_rows: list[Row] = []
    # One contribution map per output row: aggregate name -> [(row, value)].
    contrib_all: list[dict[str, list[tuple[Row, Any]]]] = []

    for key in frozen:
        members = [rows[i] for i in groups[key]]
        values: dict[str, Any] = {
            field: _json(value) for field, value in zip(keys, key)
        }
        row_contrib: dict[str, list[tuple[Row, Any]]] = {
            agg["name"]: [] for agg in aggregates
        }
        for agg in aggregates:
            name = agg["name"]
            fn = agg["fn"]
            if fn == "count":
                # COUNT(*) includes every row, NULL business values included.
                values[name] = len(members)
                row_contrib[name].extend((member, 1) for member in members)
                continue
            field_name = agg["field"]
            present = [
                (member, member.values.get(field_name))
                for member in members
                if not _is_null(member.values.get(field_name))
            ]
            if not present:
                values[name] = None
                continue
            total = 0
            for member, value in present:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise PlanError(
                        f"node {node.id!r}: SUM field {field_name!r} contains "
                        f"non-integer value {value!r}"
                    )
                total += value
                row_contrib[name].append((member, value))
            values[name] = total

        prov = ("group",) + _freeze_key(key)
        out_rows.append(Row(values=values, prov=prov))
        contrib_all.append(row_contrib)

    return out_rows, contrib_all


def _freeze_key(key: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(None if value is _NULL else value for value in key)


# ---------------------------------------------------------------------------
# Reference expansion (source rows / operation paths)
# ---------------------------------------------------------------------------


def _flatten_prov(prov: Prov) -> list[tuple[tuple[str, Any], list[tuple[str, str]]]]:
    """Expand a provenance identity into source occurrences and operation paths.

    Returns ``[(('table', row_id), [(op, detail), ...]), ...]``.  The same source
    row appears once per occurrence -- nothing is collapsed.
    """

    op = prov[0]
    if op == "source":
        return [((prov[1], prov[2]), [])]
    if op == "union":
        branch, inner = prov[1], prov[2]
        return [
            (source, path + [("union_all", f"{branch}_branch")])
            for source, path in _flatten_prov(inner)
        ]
    if op == "join":
        left = _flatten_prov(prov[1])
        right = _flatten_prov(prov[2])
        return [
            (source, path + [("inner_join", "left")]) for source, path in left
        ] + [
            (source, path + [("inner_join", "right")]) for source, path in right
        ]
    raise EtlError(f"cannot expand provenance of {prov!r}")
