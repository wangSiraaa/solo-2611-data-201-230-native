"""packaudit 包：定长训练块打包与可回查审计。"""
from .core import (
    PackError,
    allowed_attention,
    audit_blocks,
    build_shifted_labels,
    load_config,
    load_documents,
    pack_documents,
    read_parquet,
    write_parquet,
)

__all__ = [
    "PackError",
    "allowed_attention",
    "audit_blocks",
    "build_shifted_labels",
    "load_config",
    "load_documents",
    "pack_documents",
    "read_parquet",
    "write_parquet",
]
