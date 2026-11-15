"""Compiled plan data structures and their JSON serialization.

A :class:`PatchPlan` is the output of validation/compilation and the sole
input of apply. It is pure data (no open files, no codec objects) and can be
dumped to JSON for inspection or archival.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from zarr_patch.source import ArrayMeta

# A single local update inside one chunk:
# (local_row, local_col, value, source_operation_index).
LocalUpdate = tuple[int, int, int, int]


@dataclass
class ChunkPlan:
    """Everything that happens to one chunk, expressed in chunk-local coords.

    ``updates`` preserves the original per-operation, per-cell ordering. The
    final write only needs ``final_cells`` (last writer wins); ``updates`` is
    retained so the plan documents the full local update sequence and its
    update counts.
    """

    chunk_index: tuple[int, int]
    key: str
    updates: list[LocalUpdate] = field(default_factory=list)
    final_cells: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def update_count(self) -> int:
        """Number of local cell writes (repeated coordinates counted)."""
        return len(self.updates)

    @property
    def affected_cell_count(self) -> int:
        """Number of distinct chunk-local cells finally overwritten."""
        return len(self.final_cells)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_index": list(self.chunk_index),
            "key": self.key,
            "update_count": self.update_count,
            "affected_cell_count": self.affected_cell_count,
            "updates": [list(u) for u in self.updates],
            "final_cells": [[r, c, v] for (r, c), v in sorted(self.final_cells.items())],
        }


@dataclass
class PatchPlan:
    """Compiled representation of a whole ordered patch batch."""

    source_dir: str
    shape: tuple[int, int]
    chunks: tuple[int, int]
    dtype: str
    fill_value: int
    order: str
    operation_count: int
    noop_operations: list[int]
    chunk_plans: dict[str, ChunkPlan]
    total_update_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir": self.source_dir,
            "shape": list(self.shape),
            "chunks": list(self.chunks),
            "dtype": self.dtype,
            "fill_value": self.fill_value,
            "order": self.order,
            "operation_count": self.operation_count,
            "noop_operations": self.noop_operations,
            "total_update_count": self.total_update_count,
            "touched_chunks": sorted(self.chunk_plans),
            "chunk_plans": [self.chunk_plans[k].to_dict() for k in sorted(self.chunk_plans)],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_meta(
        cls,
        meta: ArrayMeta,
        operation_count: int,
        noop_operations: list[int],
        chunk_plans: dict[str, ChunkPlan],
    ) -> "PatchPlan":
        return cls(
            source_dir=meta.source_dir,
            shape=meta.shape,
            chunks=meta.chunks,
            dtype=meta.dtype.str,
            fill_value=meta.fill_value,
            order=meta.order,
            operation_count=operation_count,
            noop_operations=list(noop_operations),
            chunk_plans=chunk_plans,
            total_update_count=sum(cp.update_count for cp in chunk_plans.values()),
        )
