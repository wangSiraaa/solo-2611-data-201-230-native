"""Typed attribute canonicalization and stream identity.

Scope 规则（见 fixtures/scope.json）：

* 仅支持 stringValue / intValue / boolValue 三种 AnyValue；
* 同一属性集合内重复 key 非法；
* 属性顺序不影响身份，因此属性按 key 排序后进入身份；
* 字符串 "1" 与整数 1 必须是不同身份，类型标签 (tag) 参与比较；
* 缺失属性等价于空集合。

身份是纯 tuple 结构，可哈希、可确定性排序，绝不依赖 dict 迭代顺序。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

# (type tag, typed value)。tag 保证 "1" 与 1 不相等。
TaggedValue = tuple[str, Any]
# (key, (tag, value))
CanonicalAttr = tuple[str, TaggedValue]

TAG_STRING = "s"
TAG_BOOL = "b"
TAG_INT = "i"

METRIC_KIND_SUM = "monotonic_sum"
METRIC_KIND_HISTOGRAM = "histogram"


class DuplicateAttributeKey(ValueError):
    """同一属性集合中出现重复 key。"""


class UnsupportedAttributeValue(ValueError):
    """出现 scope 允许范围之外的 AnyValue 类型（double/array/kvlist/bytes）。"""


def parse_typed_value(value: Any) -> TaggedValue:
    """把 OTLP AnyValue 规约成 (tag, python value)，不经过 float。"""
    if not isinstance(value, dict) or len(value) != 1:
        raise UnsupportedAttributeValue(f"not a single-field AnyValue: {value!r}")
    (tag, raw), = value.items()
    if tag == "stringValue":
        if not isinstance(raw, str):
            raise UnsupportedAttributeValue("stringValue must be a JSON string")
        return TAG_STRING, raw
    if tag == "boolValue":
        if not isinstance(raw, bool):
            raise UnsupportedAttributeValue("boolValue must be a JSON boolean")
        return TAG_BOOL, raw
    if tag == "intValue":
        # OTLP JSON: intValue 是十进制字符串
        if not isinstance(raw, str):
            raise UnsupportedAttributeValue("intValue must be a decimal string")
        return TAG_INT, parse_strict_int(raw, "intValue")
    raise UnsupportedAttributeValue(f"unsupported AnyValue tag: {tag}")


def parse_strict_int(text: str, field: str) -> int:
    """十进制字符串 -> int；拒绝 bool/float/非十进制文本，全程整数运算。"""
    if not isinstance(text, str) or not text:
        raise ValueError(f"{field} must be a non-empty decimal string, got {text!r}")
    sign = ""
    body = text
    if body[0] in "+-":
        sign, body = body[0], body[1:]
    if not body or not body.isascii() or not body.isdigit():
        raise ValueError(f"{field} is not a canonical decimal integer: {text!r}")
    return int(sign + body, 10)


def canonicalize_attrs(attrs: Any) -> tuple[tuple[CanonicalAttr, ...], str | None]:
    """返回 (排序后的规范属性, 错误信息)。

    出错时仍尽力返回基于首个同名 key 的部分身份，便于隔离记录展示，
    但调用方必须让该点走 unconfirmed 通道。
    """
    if attrs is None:
        return (), None
    if not isinstance(attrs, list):
        return (), "attributes must be an array of KeyValue"

    by_key: dict[str, TaggedValue] = {}
    error: str | None = None
    for kv in attrs:
        if not isinstance(kv, dict) or "key" not in kv or "value" not in kv:
            error = error or "malformed KeyValue entry"
            continue
        key = kv["key"]
        if not isinstance(key, str):
            error = error or "attribute key must be a string"
            continue
        if key in by_key:
            error = error or f"duplicate attribute key: {key}"
            continue
        try:
            by_key[key] = parse_typed_value(kv["value"])
        except UnsupportedAttributeValue as exc:
            error = error or str(exc)
            # 仍然给一个可展示的占位身份，标签 z 排在最后且不会与合法值碰撞
            by_key[key] = ("z", "<invalid>")
    return tuple(sorted(by_key.items(), key=lambda item: item[0])), error


def render_attrs(attrs: tuple[CanonicalAttr, ...]) -> list[dict[str, Any]]:
    """规范属性 -> 输出用的 OTLP 风格 KeyValue 列表（已排序）。"""
    json_tag = {
        TAG_STRING: "stringValue",
        TAG_BOOL: "boolValue",
        TAG_INT: "intValue",
        "z": "stringValue",
    }
    out = []
    for key, (tag, value) in attrs:
        out.append({"key": key, "value": {json_tag[tag]: value}})
    return out


# 完整流身份：
# (资源属性, scope 名, scope 版本, metric 名, 单位, metric 类型, 点属性)
StreamIdentity = tuple[
    tuple[CanonicalAttr, ...],  # resource
    str,  # scope name
    str,  # scope version
    str,  # metric name
    str,  # unit
    str,  # metric kind
    tuple[CanonicalAttr, ...],  # point attributes
]


def render_identity(identity: StreamIdentity) -> dict[str, Any]:
    res_attrs, scope_name, scope_version, name, unit, kind, pt_attrs = identity
    return {
        "resource_attributes": render_attrs(res_attrs),
        "scope": {"name": scope_name, "version": scope_version},
        "metric": {"name": name, "unit": unit, "type": kind},
        "point_attributes": render_attrs(pt_attrs),
    }


def identity_label(identity: StreamIdentity) -> str:
    """单行可读身份，用于 inspect 报告。"""
    res_attrs, scope_name, scope_version, name, unit, kind, pt_attrs = identity
    bits = [f"{k}={format_tagged(v)}" for k, v in res_attrs]
    pts = [f"{k}={format_tagged(v)}" for k, v in pt_attrs]
    return (
        f"{kind}:{name}"
        f"[{','.join(bits)}]"
        f"/scope({scope_name}@{scope_version})"
        f"/unit({unit})"
        f"/pt[{','.join(pts)}]"
    )


def format_tagged(tagged: TaggedValue) -> str:
    tag, value = tagged
    prefix = {TAG_STRING: "s", TAG_BOOL: "b", TAG_INT: "i", "z": "?"}[tag]
    return f"{prefix}:{value}"


def decimal_key(values: list[Decimal | int]) -> tuple[Decimal, ...]:
    return tuple(Decimal(v) for v in values)
