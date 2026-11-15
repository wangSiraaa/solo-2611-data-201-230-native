"""解析阶段的异常类型。

所有异常都继承 :class:`CodecError`，携带问题批的序号与文件偏移，
满足“停止并给出批与文件偏移”的诊断要求。
"""

from __future__ import annotations


class CodecError(Exception):
    """变长整数 / record 体等底层编解码错误。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BatchCorrupt(Exception):
    """批级别致命错误（CRC、长度、record count、压缩体等）。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        batch_index: int | None = None,
        file_offset: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.batch_index = batch_index
        self.file_offset = file_offset

    def with_location(self, batch_index: int, file_offset: int) -> "BatchCorrupt":
        """在驱动循环中补上批序号 / 文件偏移后重新抛出。"""
        return BatchCorrupt(
            self.message,
            kind=self.kind,
            batch_index=batch_index,
            file_offset=file_offset,
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "batch_index": self.batch_index,
            "file_offset": self.file_offset,
        }


class CrcMismatch(BatchCorrupt):
    def __init__(self, message: str):
        super().__init__(message, kind="crc_mismatch")


class TruncatedBatch(BatchCorrupt):
    def __init__(self, message: str):
        super().__init__(message, kind="truncated")


class RecordCountError(BatchCorrupt):
    def __init__(self, message: str):
        super().__init__(message, kind="record_count")


class CompressionError(BatchCorrupt):
    def __init__(self, message: str):
        super().__init__(message, kind="compression")
