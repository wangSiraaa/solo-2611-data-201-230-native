"""Independent verification helpers used by the test/demo harness.

The oracle deliberately uses a *separate* NumPy array and replays the
operations one by one in original order, including repeated point
coordinates (later wins) and empty selections. It reads the source as a
whole through zarr on purpose: it is a reference checker, not the
implementation under test, which never does a full-array read/write.
"""

from __future__ import annotations

import json
import os

import numpy as np
import zarr
from numcodecs.registry import get_codec

from zarr_patch.errors import PatchValidationError
from zarr_patch.planning import _is_plain_int  # reuse the strict int rule


def oracle_apply(source_dir: str, patches: list) -> np.ndarray:
    """Replay every patch operation onto an independent dense NumPy array."""
    source = zarr.open_array(source_dir, mode="r")
    expected = np.array(source[:], dtype=np.dtype("<i4"))
    shape = source.shape
    info = np.iinfo(np.int32)

    for idx, op in enumerate(patches):
        kind = op["op"]
        if kind == "rect":
            r0, r1 = op["rows"]
            c0, c1 = op["cols"]
            if r1 == r0 or c1 == c0:
                continue
            if "value" in op:
                value = op["value"]
                if not _is_plain_int(value) or not info.min <= value <= info.max:
                    raise PatchValidationError(f"oracle: bad scalar at op {idx}")
                expected[r0:r1, c0:c1] = np.int32(value)
            else:
                values = np.asarray(op["values"], dtype=np.dtype("<i4"))
                if values.shape != (r1 - r0, c1 - c0):
                    raise PatchValidationError(f"oracle: bad values shape at op {idx}")
                expected[r0:r1, c0:c1] = values
        elif kind == "points":
            coords = op["coords"]
            values = op["values"]
            if len(coords) != len(values):
                raise PatchValidationError(f"oracle: length mismatch at op {idx}")
            # Sequential assignment preserves "later coordinate wins".
            for (row, col), value in zip(coords, values):
                if not (0 <= row < shape[0] and 0 <= col < shape[1]):
                    raise PatchValidationError(f"oracle: point out of bounds at op {idx}")
                if not _is_plain_int(value) or not info.min <= value <= info.max:
                    raise PatchValidationError(f"oracle: bad point value at op {idx}")
                expected[row, col] = np.int32(value)
        else:
            raise PatchValidationError(f"oracle: unknown op {kind!r}")
    return expected


def verify_with_zarr(output_dir: str, expected: np.ndarray, source_dir: str) -> dict:
    """Reopen the output with the zarr library and check everything visible."""
    out = zarr.open_array(output_dir, mode="r")
    src = zarr.open_array(source_dir, mode="r")

    assert out.shape == src.shape == expected.shape
    assert out.chunks == src.chunks
    assert out.dtype == src.dtype == np.dtype("<i4")
    assert out.fill_value == src.fill_value
    assert out.order == src.order
    assert out.compressor.get_config() == src.compressor.get_config()
    assert out.filters == src.filters

    actual = np.array(out[:], dtype=np.dtype("<i4"))
    if not np.array_equal(actual, expected):
        diff = np.argwhere(actual != expected)
        raise AssertionError(
            f"output differs from oracle at {len(diff)} cells, first: {diff[:5].tolist()}"
        )

    # Missing-chunk fill behavior at the store level: keys absent in the
    # output must still read back as fill through the library.
    grid = tuple(-(-s // c) for s, c in zip(out.shape, out.chunks))
    store_keys = {k for k in out.store if not k.startswith(".")}
    fill_checks = []
    for i in range(grid[0]):
        for j in range(grid[1]):
            key = f"{i}.{j}"
            if key not in store_keys:
                r0, c0 = i * out.chunks[0], j * out.chunks[1]
                region = out[
                    r0:min(r0 + out.chunks[0], out.shape[0]),
                    c0:min(c0 + out.chunks[1], out.shape[1]),
                ]
                assert np.all(region == out.fill_value), f"missing chunk {key} not filled"
                fill_checks.append(key)

    # Edge chunks: the in-shape region matches the oracle and no padding is
    # exposed beyond shape (array indexing already guarantees shape, and the
    # stored chunk must decode to a full chunk-shaped buffer).
    edge_key = f"{grid[0] - 1}.{grid[1] - 1}"
    if edge_key in store_keys:
        codec = get_codec(out.compressor.get_config())
        with open(os.path.join(output_dir, edge_key), "rb") as fh:
            buf = codec.decode(fh.read())
        chunk = np.frombuffer(buf, dtype=out.dtype).reshape(out.chunks)
        assert chunk.shape == tuple(out.chunks), "edge chunk must keep full chunk shape"
        r0, c0 = (grid[0] - 1) * out.chunks[0], (grid[1] - 1) * out.chunks[1]
        valid = expected[r0 : out.shape[0], c0 : out.shape[1]]
        if not np.array_equal(chunk[: valid.shape[0], : valid.shape[1]], valid):
            raise AssertionError("edge chunk valid region disagrees with oracle")

    return {
        "shape": list(out.shape),
        "chunks": list(out.chunks),
        "dtype": str(out.dtype),
        "fill_value": out.fill_value,
        "order": out.order,
        "compressor": out.compressor.get_config(),
        "missing_chunks_read_as_fill": fill_checks,
        "store_keys": sorted(store_keys),
        "array_equals_oracle": True,
    }


def verify_byte_copies(source_dir: str, output_dir: str, copied_keys: list[str]) -> None:
    """Unaffected existing chunks must be byte-identical, not re-encoded."""
    for key in copied_keys:
        with open(os.path.join(source_dir, key), "rb") as a, open(
            os.path.join(output_dir, key), "rb"
        ) as b:
            assert a.read() == b.read(), f"chunk {key} was not byte-copied"


def load_patches(path: str) -> list:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
