"""routecopy 的异常类型与退出码约定。

退出码:
  0  成功
  2  用法/输入文件错误
  3  load 执行期数据库错误(已整批回滚)
  4  计划过期(目录或输入已变,拒绝旧计划)
  5  独立验证不一致
  6  目录形态不受支持(HASH / 表达式 / 非 C collation / 超深 / 类型不支持)
"""


class RouteCopyError(Exception):
    exit_code = 1

    def __init__(self, message, payload=None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


class UsageError(RouteCopyError):
    exit_code = 2


class LoadFailed(RouteCopyError):
    """COPY 期间数据库报错(如 CHECK 约束),事务已整批回滚。"""

    exit_code = 3


class StalePlan(RouteCopyError):
    """load 前只读复核发现目录指纹或输入摘要与计划不一致。"""

    exit_code = 4


class VerifyFailed(RouteCopyError):
    exit_code = 5


class CatalogUnsupported(RouteCopyError):
    exit_code = 6
