"""Offline, chunk-consolidating patch engine for 2-D Zarr v2 arrays.

Public entry points:

* :func:`zarr_patch.planning.compile_plan` -- validate a patch batch and
  compile it into per-chunk local update sequences (no chunk I/O).
* :func:`zarr_patch.apply.apply_plan` -- materialise a compiled plan into a
  new Zarr directory, decoding/encoding every touched chunk at most once.
"""

from zarr_patch.apply import ApplyReport, apply_plan
from zarr_patch.errors import PatchValidationError
from zarr_patch.model import ChunkPlan, PatchPlan
from zarr_patch.planning import compile_plan

__all__ = [
    "compile_plan",
    "apply_plan",
    "ApplyReport",
    "PatchPlan",
    "ChunkPlan",
    "PatchValidationError",
]
