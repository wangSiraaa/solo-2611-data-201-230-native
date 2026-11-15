"""RFC 1982 定义的 32 位 DNS 序列号算术。

序列号空间是模 2^32 的环：4294967295 的下一个合法值是 0。
当两个序列号的环形差值恰好为 2^31 时，二者无法比较，必须显式报错，
不能猜测方向。
"""

from __future__ import annotations

MODULUS = 1 << 32
HALF_SPAN = 1 << 31
MAX_SERIAL = MODULUS - 1


class IncomparableSerial(Exception):
    """两个序列号差值恰好为 2^31，按 RFC 1982 无法判断先后。"""

    def __init__(self, left: int, right: int) -> None:
        super().__init__(
            f"序列号 {left} 与 {right} 的环形差值为 2^31，无法比较先后"
        )
        self.left = left
        self.right = right


def validate(serial: int) -> int:
    if not isinstance(serial, int) or isinstance(serial, bool):
        raise ValueError(f"序列号必须是整数: {serial!r}")
    if not 0 <= serial <= MAX_SERIAL:
        raise ValueError(f"序列号超出 32 位无符号范围: {serial}")
    return serial


def compare(left: int, right: int) -> int:
    """按序列号算术比较先后。

    返回 -1 表示 left 早于 right，0 表示相等，1 表示 left 晚于 right；
    环形差值恰好 2^31 时抛 IncomparableSerial。
    """
    left, right = validate(left), validate(right)
    if left == right:
        return 0
    delta = (left - right) % MODULUS
    if delta == HALF_SPAN:
        raise IncomparableSerial(left, right)
    return 1 if delta < HALF_SPAN else -1


def is_forward(old: int, new: int) -> bool:
    """new 是否为 old 之后的合法前进（含 4294967295 -> 0 回绕）。"""
    return compare(new, old) == 1
