"""Instrumentation at the real numcodecs codec entry points.

The engine's own :class:`~zarr_patch.apply.CountingCodec` reports per-chunk
counters, but an independent check should observe the *actual* numcodecs
class methods. This context manager monkey-patches ``decode``/``encode`` on
the concrete codec class built from the source metadata and records every
call while it is active.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from zarr_patch.source import load_meta


@dataclass
class CodecCallLog:
    decode_calls: int = 0
    encode_calls: int = 0
    decode_args: list = field(default_factory=list)
    encode_args: list = field(default_factory=list)


@contextlib.contextmanager
def instrument_codec(source_dir: str):
    """Patch the concrete numcodecs codec class used by the source array."""
    meta = load_meta(source_dir)
    codec = meta.build_codec()
    cls = type(codec)
    log = CodecCallLog()

    orig_decode = cls.decode
    orig_encode = cls.encode

    def tracked_decode(self, buf, out=None):
        log.decode_calls += 1
        log.decode_args.append((type(self).__name__, len(bytes(buf))))
        return orig_decode(self, buf, out=out)

    def tracked_encode(self, buf):
        log.encode_calls += 1
        log.encode_args.append((type(self).__name__, len(buf)))
        return orig_encode(self, buf)

    cls.decode = tracked_decode
    cls.encode = tracked_encode
    try:
        yield log
    finally:
        cls.decode = orig_decode
        cls.encode = orig_encode
