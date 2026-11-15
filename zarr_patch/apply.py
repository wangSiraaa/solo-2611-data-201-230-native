"""Apply a compiled :class:`PatchPlan` into a new Zarr v2 directory.

Processing is chunk-centric: each touched chunk is decoded at most once and
encoded at most once, regardless of how many operations hit it. Missing
source chunks are built from the fill value. Edge chunks keep their full
chunk-shaped buffer and only the in-shape region is ever considered data;
padding beyond the array shape is never interpreted.

Unaffected existing chunks are copied byte-for-byte (no decode/re-encode);
missing unaffected chunks stay absent. All writes go to a temporary staging
directory that is renamed into place only after the whole batch succeeded, so
a pre-flight failure never publishes a partial product.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from zarr_patch.errors import PatchValidationError
from zarr_patch.model import PatchPlan
from zarr_patch.source import ArrayMeta, list_existing_chunks, load_meta

_META_NAME = ".zarray"
_OPTIONAL_META = (".zattrs", ".zgroup")


@dataclass
class ChunkApplyRecord:
    key: str
    source_existed: bool
    decodes: int
    encodes: int
    update_count: int
    affected_cells: int


@dataclass
class ApplyReport:
    """What actually happened during apply, derived from real codec I/O."""

    output_dir: str
    touched: list[ChunkApplyRecord] = field(default_factory=list)
    byte_copied_chunks: list[str] = field(default_factory=list)
    # Compressed-byte copies and decode calls are reported separately.
    byte_copy_count: int = 0
    total_decodes: int = 0
    total_encodes: int = 0
    total_updates: int = 0
    missing_untouched_absent: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "decoded_and_encoded_chunks": [r.key for r in self.touched],
            "touched_detail": [
                {
                    "key": r.key,
                    "source_existed": r.source_existed,
                    "decodes": r.decodes,
                    "encodes": r.encodes,
                    "update_count": r.update_count,
                    "affected_cells": r.affected_cells,
                }
                for r in self.touched
            ],
            "byte_copied_chunks": sorted(self.byte_copied_chunks),
            "byte_copy_count": self.byte_copy_count,
            "missing_untouched_kept_absent": sorted(self.missing_untouched_absent),
            "totals": {
                "decodes": self.total_decodes,
                "encodes": self.total_encodes,
                "byte_copies": self.byte_copy_count,
                "updates": self.total_updates,
            },
        }


class CountingCodec:
    """Codec wrapper that records every real decode/encode entry point call.

    Delegates to the numcodecs codec built from the source metadata so the
    compression format stays identical. Counters make the "at most one decode
    and one encode per touched chunk" guarantee externally observable.
    """

    def __init__(self, codec):
        self._codec = codec
        self.decode_calls = 0
        self.encode_calls = 0

    def decode(self, buf, out=None):
        self.decode_calls += 1
        return self._codec.decode(buf, out=out)

    def encode(self, buf):
        self.encode_calls += 1
        return self._codec.encode(buf)

    @property
    def codec_id(self):
        return self._codec.codec_id

    def get_config(self):
        return self._codec.get_config()


def _check_output_path(output_dir: str) -> None:
    if os.path.exists(output_dir):
        raise PatchValidationError(
            f"output path already exists, refusing to overwrite: {output_dir}"
        )
    parent = os.path.dirname(os.path.abspath(output_dir))
    if not os.path.isdir(parent):
        raise PatchValidationError(f"parent directory of output does not exist: {parent}")
    if not os.access(parent, os.W_OK):
        raise PatchValidationError(f"parent directory is not writable: {parent}")


def _copy_metadata(meta: ArrayMeta, staging_dir: str) -> None:
    # Byte-for-byte copy guarantees shape/chunks/dtype/codec/order/fill are
    # preserved exactly, independent of any JSON re-serialization differences.
    shutil.copyfile(
        os.path.join(meta.source_dir, _META_NAME),
        os.path.join(staging_dir, _META_NAME),
    )
    for name in _OPTIONAL_META:
        src = os.path.join(meta.source_dir, name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(staging_dir, name))


def _decode_chunk(
    meta: ArrayMeta, codec: CountingCodec, key: str, source_existed: bool
) -> np.ndarray:
    """Return a full chunk-shaped int32 buffer for one chunk.

    Existing chunks are decoded once through the real codec entry point;
    missing chunks are materialized from the fill value with zero decodes.
    """
    chunk_shape = meta.chunks
    if source_existed:
        with open(os.path.join(meta.source_dir, key), "rb") as fh:
            raw = fh.read()
        buf = codec.decode(raw)
        arr = np.frombuffer(buf, dtype=meta.dtype).reshape(chunk_shape)
        decoded = np.array(arr, dtype=meta.dtype, copy=True)
    else:
        decoded = np.full(
            chunk_shape, meta.fill_value, dtype=meta.dtype, order=meta.order
        )
    return decoded


def apply_plan(plan: PatchPlan, output_dir: str) -> ApplyReport:
    """Materialize ``plan`` into a brand-new Zarr array at ``output_dir``."""
    meta = load_meta(plan.source_dir)
    _assert_plan_matches_meta(plan, meta)
    _check_output_path(output_dir)

    existing = list_existing_chunks(meta)
    touched_keys = set(plan.chunk_plans)

    output_abs = os.path.abspath(output_dir)
    staging_dir = output_abs + ".staging"
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir)

    report = ApplyReport(output_dir=output_abs)
    try:
        _copy_metadata(meta, staging_dir)

        # Unaffected existing chunks: raw compressed bytes, never decoded.
        for key in sorted(existing - touched_keys):
            shutil.copyfile(
                os.path.join(meta.source_dir, key),
                os.path.join(staging_dir, key),
            )
            report.byte_copied_chunks.append(key)
        report.byte_copy_count = len(report.byte_copied_chunks)

        gn, gm = meta.chunk_grid
        all_keys = {meta.chunk_key(i, j) for i in range(gn) for j in range(gm)}
        report.missing_untouched_absent = sorted(all_keys - existing - touched_keys)

        codec = CountingCodec(meta.build_codec())
        # Sorted keys give deterministic reporting; correctness does not rely
        # on the order because each chunk is independent.
        for key in sorted(touched_keys):
            chunk_plan = plan.chunk_plans[key]
            source_existed = key in existing
            decodes_before = codec.decode_calls
            encodes_before = codec.encode_calls

            arr = _decode_chunk(meta, codec, key, source_existed)
            valid = meta.valid_region(*chunk_plan.chunk_index)
            for (lr, lc), value in chunk_plan.final_cells.items():
                if not (
                    valid[0].start <= lr < valid[0].stop
                    and valid[1].start <= lc < valid[1].stop
                ):
                    raise PatchValidationError(
                        f"plan cell ({lr}, {lc}) outside valid region of chunk {key}"
                    )
                arr[lr, lc] = np.int32(value)

            if codec.decode_calls - decodes_before > 1:
                raise AssertionError(f"chunk {key} decoded more than once")
            payload = codec.encode(np.ascontiguousarray(arr, dtype=meta.dtype))
            if codec.encode_calls - encodes_before != 1:
                raise AssertionError(f"chunk {key} encoded {codec.encode_calls - encodes_before}x")
            with open(os.path.join(staging_dir, key), "wb") as fh:
                fh.write(payload)

            report.touched.append(
                ChunkApplyRecord(
                    key=key,
                    source_existed=source_existed,
                    decodes=codec.decode_calls - decodes_before,
                    encodes=codec.encode_calls - encodes_before,
                    update_count=chunk_plan.update_count,
                    affected_cells=chunk_plan.affected_cell_count,
                )
            )

        report.total_decodes = codec.decode_calls
        report.total_encodes = codec.encode_calls
        report.total_updates = sum(r.update_count for r in report.touched)

        # Re-check right before publishing: never clobber an output that
        # appeared after the initial pre-flight check.
        if os.path.exists(output_dir):
            raise PatchValidationError(
                f"output path already exists, refusing to overwrite: {output_dir}"
            )
        os.rename(staging_dir, output_abs)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return report


def _assert_plan_matches_meta(plan: PatchPlan, meta: ArrayMeta) -> None:
    if plan.shape != meta.shape or plan.chunks != meta.chunks:
        raise PatchValidationError("plan layout does not match current source metadata")
    if plan.dtype != meta.dtype.str or plan.fill_value != meta.fill_value:
        raise PatchValidationError("plan dtype/fill does not match current source metadata")
    if plan.order != meta.order:
        raise PatchValidationError("plan order does not match current source metadata")
