"""离线 Kafka magic=2 RecordBatch 读取器。

只面向单个分区的小型日志片段：按 message format v2 解析 RecordBatch，
结合分区 high watermark / last stable offset 元数据，导出对
``read_committed`` 消费者可见的原始消息。不启动 broker，也不做日志修复。
"""

from .errors import (
    BatchCorrupt,
    CodecError,
    CompressionError,
    CrcMismatch,
    RecordCountError,
    TruncatedBatch,
)
from .batch import Batch, Record, parse_fragment
from .records import Header
from .analyze import Entry, analyze, load_metadata

__all__ = [
    "Batch",
    "Header",
    "Record",
    "Entry",
    "BatchCorrupt",
    "CodecError",
    "CompressionError",
    "CrcMismatch",
    "RecordCountError",
    "TruncatedBatch",
    "parse_fragment",
    "analyze",
    "load_metadata",
]
