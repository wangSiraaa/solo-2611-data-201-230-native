"""Validate a patch batch and compile it into per-chunk local update plans.

Validation is all-or-nothing: :class:`PatchValidationError` is raised on the
first illegal operation and nothing about the output directory is touched.
Compilation is pure arithmetic over indices -- no chunk is read or decoded
here, and repeated hits on one chunk are accumulated into a single
:class:`~zarr_patch.model.ChunkPlan` instead of issuing one array assignment
per operation.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from zarr_patch.errors import PatchValidationError
from zarr_patch.model import ChunkPlan, PatchPlan
from zarr_patch.source import ArrayMeta, load_meta

_INT32 = np.iinfo(np.int32)
_RECT_KEYS = {"op", "rows", "cols", "value", "values"}
_POINT_KEYS = {"op", "coords", "values"}


def _is_plain_int(x: Any) -> bool:
    """True for machine integers but never for booleans.

    ``bool`` is a subclass of ``int`` in Python; the spec explicitly says
    booleans must not be accepted as integer values or indices.
    """
    if isinstance(x, bool):
        return False
    return isinstance(x, (int, np.integer))


def _check_int32(value: int, what: str) -> int:
    v = int(value)
    if not _INT32.min <= v <= _INT32.max:
        raise PatchValidationError(f"{what}={v} is outside the int32 range")
    return v


def _check_index(x: Any, what: str, limit: int) -> int:
    if not _is_plain_int(x):
        raise PatchValidationError(f"{what} must be a non-negative integer, got {x!r}")
    v = int(x)
    if v < 0:
        raise PatchValidationError(f"{what} must be non-negative, got {v}")
    if v > limit:
        raise PatchValidationError(f"{what}={v} is out of bounds (limit {limit})")
    return v


def _check_range(pair: Any, name: str, dim: int, op_idx: int) -> tuple[int, int]:
    where = f"operation {op_idx} ({name})"
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise PatchValidationError(f"{where} must be a [start, stop) pair, got {pair!r}")
    start = _check_index(pair[0], f"{where} start", dim)
    stop = _check_index(pair[1], f"{where} stop", dim)
    if start > stop:
        raise PatchValidationError(f"{where} start ({start}) exceeds stop ({stop})")
    return start, stop


def _check_scalar(value: Any, what: str, op_idx: int) -> int:
    if not _is_plain_int(value):
        raise PatchValidationError(
            f"operation {op_idx}: {what} must be an integer (booleans are not accepted), "
            f"got {value!r}"
        )
    return _check_int32(value, f"operation {op_idx}: {what}")


def _check_rect_values(values: Any, height: int, width: int, op_idx: int) -> list[list[int]]:
    where = f"operation {op_idx}: values"
    if not isinstance(values, list) or len(values) != height:
        raise PatchValidationError(
            f"{where} must be a 2-D list with shape [{height}, {width}]"
        )
    checked: list[list[int]] = []
    for r, row in enumerate(values):
        if not isinstance(row, list) or len(row) != width:
            raise PatchValidationError(
                f"{where}[{r}] must be a list of {width} integers"
            )
        checked_row = [
            _check_scalar(cell, f"values[{r}][{c}]", op_idx) for c, cell in enumerate(row)
        ]
        checked.append(checked_row)
    return checked


def _check_points(coords: Any, values: Any, meta: ArrayMeta, op_idx: int):
    where = f"operation {op_idx} (points)"
    if not isinstance(coords, list):
        raise PatchValidationError(f"{where}: coords must be a list of [row, col] pairs")
    if not isinstance(values, list) or len(values) != len(coords):
        raise PatchValidationError(
            f"{where}: coords and values must be lists of equal length "
            f"({len(coords) if isinstance(coords, list) else 'n/a'} vs "
            f"{len(values) if isinstance(values, list) else 'n/a'})"
        )
    checked_coords: list[tuple[int, int]] = []
    checked_values: list[int] = []
    for k, (pair, raw_value) in enumerate(zip(coords, values)):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise PatchValidationError(f"{where}: coords[{k}] must be a [row, col] pair")
        row = _check_index(pair[0], f"{where} coords[{k}] row", meta.shape[0] - 1)
        col = _check_index(pair[1], f"{where} coords[{k}] col", meta.shape[1] - 1)
        checked_coords.append((row, col))
        checked_values.append(_check_scalar(raw_value, f"values[{k}]", op_idx))
    return checked_coords, checked_values


def _validate_operation(op: Any, op_idx: int, meta: ArrayMeta):
    """Return a normalized description of one operation."""
    if not isinstance(op, dict):
        raise PatchValidationError(f"operation {op_idx} must be a JSON object")
    kind = op.get("op")
    if kind == "rect":
        unknown = set(op) - _RECT_KEYS
        if unknown:
            raise PatchValidationError(f"operation {op_idx}: unknown rect keys {sorted(unknown)}")
        r0, r1 = _check_range(op.get("rows"), "rows", meta.shape[0], op_idx)
        c0, c1 = _check_range(op.get("cols"), "cols", meta.shape[1], op_idx)
        has_scalar = "value" in op
        has_array = "values" in op
        if has_scalar == has_array:
            raise PatchValidationError(
                f"operation {op_idx}: exactly one of 'value' (scalar) or 'values' (2-D) required"
            )
        height, width = r1 - r0, c1 - c0
        if has_scalar:
            # An empty rectangle is a legal no-op even with a scalar value.
            if height == 0 or width == 0:
                return ("rect", r0, r1, c0, c1, None, None)
            return ("rect", r0, r1, c0, c1, _check_scalar(op["value"], "value", op_idx), None)
        # With 'values', the list must always match the rectangle shape; an
        # empty selection therefore requires an appropriately empty list.
        return ("rect", r0, r1, c0, c1, None, _check_rect_values(op["values"], height, width, op_idx))

    if kind == "points":
        unknown = set(op) - _POINT_KEYS
        if unknown:
            raise PatchValidationError(f"operation {op_idx}: unknown points keys {sorted(unknown)}")
        if "coords" not in op or "values" not in op:
            raise PatchValidationError(f"operation {op_idx}: points requires 'coords' and 'values'")
        checked_coords, checked_values = _check_points(op["coords"], op["values"], meta, op_idx)
        return ("points", checked_coords, checked_values)

    raise PatchValidationError(
        f"operation {op_idx}: unknown op {kind!r} (expected 'rect' or 'points')"
    )


def _chunk_plan_for(chunk_plans: dict[str, ChunkPlan], meta: ArrayMeta, ci: int, cj: int) -> ChunkPlan:
    key = meta.chunk_key(ci, cj)
    plan = chunk_plans.get(key)
    if plan is None:
        plan = ChunkPlan(chunk_index=(ci, cj), key=key)
        chunk_plans[key] = plan
    return plan


def _emit_cell(
    chunk_plans: dict[str, ChunkPlan],
    meta: ArrayMeta,
    op_idx: int,
    row: int,
    col: int,
    value: int,
) -> None:
    ci, ri = divmod(row, meta.chunks[0])
    cj, cj_local = divmod(col, meta.chunks[1])
    plan = _chunk_plan_for(chunk_plans, meta, ci, cj)
    plan.updates.append((ri, cj_local, value, op_idx))
    # Dict assignment keeps the last writer, which is exactly the required
    # operation priority: later operations overwrite earlier ones, and within
    # one points operation later coordinates win.
    plan.final_cells[(ri, cj_local)] = value


def compile_plan(source_dir: str, patches: Iterable[Any]) -> PatchPlan:
    """Validate ``patches`` and compile every cell write into chunk plans.

    Raises :class:`PatchValidationError` on the first illegal operation; in
    that case no plan is returned and no output path is involved.
    """
    meta = load_meta(source_dir)
    if not isinstance(patches, list):
        raise PatchValidationError("patches must be an ordered list of operations")

    normalized = [_validate_operation(op, idx, meta) for idx, op in enumerate(patches)]

    chunk_plans: dict[str, ChunkPlan] = {}
    noop_operations: list[int] = []

    for op_idx, spec in enumerate(normalized):
        if spec[0] == "rect":
            _, r0, r1, c0, c1, scalar, array_values = spec
            if r1 == r0 or c1 == c0:
                noop_operations.append(op_idx)
                continue
            for lr, row in enumerate(range(r0, r1)):
                for lc, col in enumerate(range(c0, c1)):
                    value = scalar if scalar is not None else array_values[lr][lc]
                    _emit_cell(chunk_plans, meta, op_idx, row, col, value)
        else:
            _, checked_coords, checked_values = spec
            if not checked_coords:
                noop_operations.append(op_idx)
                continue
            for (row, col), value in zip(checked_coords, checked_values):
                _emit_cell(chunk_plans, meta, op_idx, row, col, value)

    return PatchPlan.from_meta(meta, len(normalized), noop_operations, chunk_plans)
