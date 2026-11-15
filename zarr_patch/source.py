"""Reading and sanity-checking the source Zarr v2 array metadata.

Only the ``.zarray`` JSON document and directory listing are touched here;
the array data itself is never read as a whole. Chunk bytes are read later,
one chunk at a time, by :mod:`zarr_patch.apply`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import numcodecs
import numpy as np

from zarr_patch.errors import PatchValidationError

_META_NAME = ".zarray"


@dataclass(frozen=True)
class ArrayMeta:
    """Immutable description of the on-disk source array."""

    source_dir: str
    shape: tuple[int, int]
    chunks: tuple[int, int]
    dtype: np.dtype
    fill_value: int
    order: str
    compressor_config: dict[str, Any]
    filters_config: list[dict[str, Any]] | None
    raw_metadata: dict[str, Any]

    @property
    def chunk_grid(self) -> tuple[int, int]:
        """Number of chunks along each axis (ceil division of shape)."""
        return (
            (self.shape[0] + self.chunks[0] - 1) // self.chunks[0],
            (self.shape[1] + self.chunks[1] - 1) // self.chunks[1],
        )

    def chunk_key(self, ci: int, cj: int) -> str:
        """Storage key for chunk ``(ci, cj)`` using the '.' separator."""
        return f"{ci}.{cj}"

    def chunk_path(self, ci: int, cj: int) -> str:
        return os.path.join(self.source_dir, self.chunk_key(ci, cj))

    def valid_region(self, ci: int, cj: int) -> tuple[slice, slice]:
        """Slices of the *full chunk buffer* that hold real array cells.

        Edge chunks are stored as full chunk-shaped buffers; cells beyond the
        array shape are padding and must never be treated as data.
        """
        r0 = ci * self.chunks[0]
        c0 = cj * self.chunks[1]
        return (
            slice(0, min(self.chunks[0], self.shape[0] - r0)),
            slice(0, min(self.chunks[1], self.shape[1] - c0)),
        )

    def build_codec(self) -> numcodecs.abc.Codec:
        """Reconstruct the compressor from its JSON config via numcodecs."""
        return numcodecs.get_codec(self.compressor_config)


def _read_metadata(source_dir: str) -> dict[str, Any]:
    meta_path = os.path.join(source_dir, _META_NAME)
    if not os.path.isdir(source_dir):
        raise PatchValidationError(f"source directory does not exist: {source_dir}")
    if not os.path.isfile(meta_path):
        raise PatchValidationError(
            f"not a Zarr v2 array directory (missing {_META_NAME}): {source_dir}"
        )
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchValidationError(f"cannot read {_META_NAME}: {exc}") from exc
    if not isinstance(meta, dict):
        raise PatchValidationError(f"{_META_NAME} must contain a JSON object")
    return meta


def load_meta(source_dir: str) -> ArrayMeta:
    """Load ``.zarray`` and enforce the array layout this engine supports."""
    meta = _read_metadata(source_dir)

    if meta.get("zarr_format") != 2:
        raise PatchValidationError(
            f"only Zarr format 2 is supported, got zarr_format={meta.get('zarr_format')!r}"
        )

    shape = meta.get("shape")
    chunks = meta.get("chunks")
    if (
        not isinstance(shape, list)
        or not isinstance(chunks, list)
        or len(shape) != 2
        or len(chunks) != 2
    ):
        raise PatchValidationError("exactly two-dimensional shape/chunks are required")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in shape + chunks):
        raise PatchValidationError("shape and chunks must be positive integers")
    shape_t = (int(shape[0]), int(shape[1]))
    chunks_t = (int(chunks[0]), int(chunks[1]))

    try:
        dtype = np.dtype(str(meta["dtype"]))
    except (TypeError, KeyError) as exc:
        raise PatchValidationError(f"invalid dtype in metadata: {meta.get('dtype')!r}") from exc
    if dtype.kind != "i" or dtype.itemsize != 4 or dtype.byteorder == ">":
        raise PatchValidationError(
            f"only little-endian int32 arrays are supported, got dtype={dtype.str!r}"
        )
    # Normalize native-endian ('=') descriptors to explicit little-endian.
    dtype = np.dtype("<i4")

    order = meta.get("order", "C")
    if order not in ("C", "F"):
        raise PatchValidationError(f"invalid order: {order!r}")
    if order != "C":
        raise PatchValidationError("only C-order arrays are supported")

    fill_value = meta.get("fill_value")
    if isinstance(fill_value, bool) or not isinstance(fill_value, int):
        raise PatchValidationError(f"fill_value must be an integer, got {fill_value!r}")
    info = np.iinfo(np.int32)
    if not info.min <= fill_value <= info.max:
        raise PatchValidationError(f"fill_value outside int32 range: {fill_value}")

    compressor_config = meta.get("compressor")
    if compressor_config is not None:
        if not isinstance(compressor_config, dict) or "id" not in compressor_config:
            raise PatchValidationError("compressor config must be an object with an 'id'")
        try:
            numcodecs.get_codec(compressor_config)
        except Exception as exc:  # numcodecs raises various error types
            raise PatchValidationError(f"unsupported compressor config: {exc}") from exc

    filters_config = meta.get("filters")
    if filters_config is not None:
        if not isinstance(filters_config, list):
            raise PatchValidationError("filters must be null or a list")
        for cfg in filters_config:
            if not isinstance(cfg, dict) or "id" not in cfg:
                raise PatchValidationError("each filter config must be an object with an 'id'")
            try:
                numcodecs.get_codec(cfg)
            except Exception as exc:
                raise PatchValidationError(f"unsupported filter config: {exc}") from exc

    return ArrayMeta(
        source_dir=os.path.abspath(source_dir),
        shape=shape_t,
        chunks=chunks_t,
        dtype=dtype,
        fill_value=int(fill_value),
        order=order,
        compressor_config=compressor_config,
        filters_config=filters_config,
        raw_metadata=meta,
    )


def list_existing_chunks(meta: ArrayMeta) -> set[str]:
    """Return storage keys of regular chunk files inside the source directory.

    Metadata files and anything outside the declared chunk grid are ignored.
    """
    gn, gm = meta.chunk_grid
    valid = {meta.chunk_key(i, j) for i in range(gn) for j in range(gm)}
    found: set[str] = set()
    with os.scandir(meta.source_dir) as it:
        for entry in it:
            if entry.is_file() and entry.name in valid:
                found.add(entry.name)
    return found
