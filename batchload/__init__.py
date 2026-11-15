"""load-batch: 坏行二分隔离的批量 COPY 入库工具。

业务表只有 fixtures/schema.sql 中的 customers / orders，
import_receipts 与本包额外建立的审计表(copy_attempt_log, rejects)
用于在同事务保存回执并记录每条拒绝与每次原生 COPY 尝试。
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
