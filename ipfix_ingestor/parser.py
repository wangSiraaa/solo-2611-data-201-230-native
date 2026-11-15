"""IPFIX v10 报文解析核心（RFC 7011 受限子集）。

范围见 fixtures/scope.json：
- 仅 Message v10、Template Set(id=2)、Data Set(id>=256)；不实现 Options Template。
- 模板身份 = (exporter, observation_domain_id, template_id)，按到达顺序生效；
  fieldCount=0 撤回；同号重定义只影响后续 Set，旧记录保留版本与原始偏移。
- 变长字段长度前缀 1 字节(0..254) / 扩展 0xff+2 字节；企业位需读 Enterprise Number。
- 严格核对 Message Length / Set Length / 字段边界；越界变长立即隔离整个 Set。
- 记录尾仅允许 0..3 个零字节 padding 且剩余须短于最短合法记录长度。
- 大整数（含 >2^53）始终走 int，绝不经过 float。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

IPFIX_VERSION = 10
HEADER_LEN = 16
SET_HDR_LEN = 4
TEMPLATE_SET_ID = 2
MIN_DATA_SET_ID = 256
ENTERPRISE_BIT = 0x8000
VARIABLE_LEN = 0xFFFF
VARIABLE_EXT_PREFIX = 0xFF
MAX_PADDING = 3
UINT_LENGTHS = frozenset({1, 2, 4, 8})


class MessageError(Exception):
    """报文级错误：头部不合法或报文截断，整个报文失败，不产生半截数据。"""


class SetParseError(Exception):
    """Set 级错误：该 Set 被隔离为无法解释的 Set，不影响同报文其他 Set。"""


@dataclass(frozen=True)
class FieldSpec:
    """模板中的单个字段指示符。"""

    name: str
    ie_id: int
    ie_type: str          # "uint" | "ipv4" | "utf8"
    length: int           # 固定长度，或 VARIABLE_LEN
    enterprise: bool
    enterprise_number: int | None
    offset_in_template: int  # 从模板体起的指示符偏移（调试用）

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ieId": self.ie_id,
            "type": self.ie_type,
            "length": "variable" if self.length == VARIABLE_LEN else self.length,
            "enterprise": self.enterprise,
            "enterpriseNumber": self.enterprise_number,
            "offsetInTemplate": self.offset_in_template,
        }


@dataclass(frozen=True)
class TemplateRecord:
    """某一版本的完整模板定义。"""

    template_id: int
    version: int
    fields: tuple[FieldSpec, ...]
    min_record_len: int   # 变长字段按 0 长度计入

    def as_dict(self) -> dict:
        return {
            "templateId": self.template_id,
            "version": self.version,
            "fieldCount": len(self.fields),
            "minRecordLength": self.min_record_len,
            "fields": [f.as_dict() for f in self.fields],
        }


@dataclass
class _TemplateState:
    template: TemplateRecord | None  # None 表示已撤回；撤回前的版本仍在历史中
    next_version: int = 1


class TemplateRegistry:
    """按 (exporter, domain, template_id) 隔离的模板生命周期表。"""

    def __init__(self) -> None:
        self._state: dict[tuple[str, int, int], _TemplateState] = {}

    @staticmethod
    def key(exporter: str, domain_id: int, template_id: int) -> tuple[str, int, int]:
        return exporter, domain_id, template_id

    def active(self, exporter: str, domain_id: int, template_id: int) -> TemplateRecord | None:
        state = self._state.get(self.key(exporter, domain_id, template_id))
        if state is None:
            return None
        return state.template  # 撤回后为 None

    def define(
        self, exporter: str, domain_id: int, template_id: int, fields: tuple[FieldSpec, ...]
    ) -> TemplateRecord:
        """登记一次重定义（到达顺序），返回新版本。"""
        k = self.key(exporter, domain_id, template_id)
        state = self._state.setdefault(k, _TemplateState(template=None))
        min_len = sum(0 if f.length == VARIABLE_LEN else f.length for f in fields)
        rec = TemplateRecord(template_id, state.next_version, fields, min_len)
        state.template = rec
        state.next_version += 1
        return rec

    def withdraw(self, exporter: str, domain_id: int, template_id: int) -> int:
        """撤回模板，返回被撤回的版本号；未定义过则报错。"""
        k = self.key(exporter, domain_id, template_id)
        state = self._state.get(k)
        if state is None or state.template is None:
            raise SetParseError(f"withdraw_unknown_template: id={template_id} 从未定义或已撤回")
        withdrawn_version = state.template.version
        state.template = None
        return withdrawn_version


@dataclass
class IEConfig:
    """information-elements.json 的受限注册表视图。"""

    standard: dict[int, str]
    enterprise: dict[tuple[int, int], str]

    @classmethod
    def load(cls, path: str | Path) -> "IEConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        standard = {int(k): v for k, v in raw.get("standard", {}).items()}
        enterprise = {
            tuple(map(int, key.split(":", 1))): value
            for key, value in raw.get("enterprise", {}).items()
        }
        return cls(standard=standard, enterprise=enterprise)

    def lookup(self, enterprise: bool, enterprise_number: int | None, ie_id: int) -> tuple[str, str]:
        """返回 (字段名, 类型)。注册表外的字段直接报错，绝不与同号标准字段混淆。"""
        if enterprise:
            spec = self.enterprise.get((enterprise_number or 0, ie_id))
            if spec is None:
                raise SetParseError(
                    f"unknown_enterprise_ie: enterprise={enterprise_number} ieId={ie_id}"
                )
        else:
            spec = self.standard.get(ie_id)
            if spec is None:
                raise SetParseError(f"unknown_standard_ie: ieId={ie_id}")
        name, _, ie_type = spec.partition(":")
        return name, ie_type


def _u16(buf: bytes, off: int) -> int:
    return (buf[off] << 8) | buf[off + 1]


def _u32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 4], "big")


def decode_uint(value: bytes) -> int:
    """无符号网络序整数，长度仅允许 1/2/4/8；全程 int，不经过 float。"""
    if len(value) not in UINT_LENGTHS:
        raise SetParseError(f"bad_uint_length: {len(value)}")
    return int.from_bytes(value, "big")


def decode_ipv4(value: bytes) -> str:
    if len(value) != 4:
        raise SetParseError(f"bad_ipv4_length: {len(value)}")
    return ".".join(str(b) for b in value)


def decode_utf8(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SetParseError(f"bad_utf8: {exc}") from exc


def _decode_value(ie_type: str, raw: bytes):
    if ie_type == "uint":
        return decode_uint(raw)
    if ie_type == "ipv4":
        return decode_ipv4(raw)
    if ie_type == "utf8":
        return decode_utf8(raw)
    raise SetParseError(f"unsupported_ie_type: {ie_type}")


def _check_tail_padding(payload: bytes, pos: int, min_record_len: int) -> int:
    """记录尾检查：剩余必须短于最短合法记录，且只允许 0..3 个零字节。"""
    remaining = len(payload) - pos
    if remaining == 0:
        return pos
    if remaining >= min_record_len:
        raise SetParseError(
            f"trailing_bytes_parseable: remaining={remaining} minRecordLength={min_record_len}"
        )
    if remaining > MAX_PADDING:
        raise SetParseError(f"bad_padding_length: {remaining} 字节，超过 0..3")
    if any(b != 0 for b in payload[pos:]):
        raise SetParseError("nonzero_trailing_bytes: 无法当作零 padding")
    return len(payload)


def _read_varlen(payload: bytes, pos: int, set_end: int) -> tuple[bytes, int]:
    """读取变长字段（短/扩展长度前缀），越界立即抛错，绝不读到下一 Set。"""
    if pos >= set_end:
        raise SetParseError("variable_length_overrun: 缺少长度前缀")
    first = payload[pos]
    pos += 1
    if first == VARIABLE_EXT_PREFIX:
        if pos + 2 > set_end:
            raise SetParseError("variable_length_overrun: 扩展长度前缀不完整")
        length = _u16(payload, pos)
        pos += 2
    else:
        length = first
    if pos + length > set_end:
        raise SetParseError(
            f"variable_length_overrun: 声明 {length} 字节，Set 内仅剩 {set_end - pos}"
        )
    return payload[pos:pos + length], pos + length


def _parse_template_set(
    payload: bytes,
    exporter: str,
    domain_id: int,
    registry: TemplateRegistry,
    ie_config: IEConfig,
) -> list[dict]:
    """解析 Template Set(id=2)，返回模板历史事件。"""
    events: list[dict] = []
    pos = 0
    end = len(payload)
    while pos < end:
        if end - pos < 4:
            # 模板记录头都放不下：只允许短于最短记录(>=4)的零 padding
            _check_tail_padding(payload, pos, 4)
            break
        template_id = _u16(payload, pos)
        field_count = _u16(payload, pos + 2)
        record_offset = pos
        pos += 4
        if template_id < MIN_DATA_SET_ID:
            raise SetParseError(f"bad_template_id: {template_id} 必须 >=256")

        if field_count == 0:
            # scope.json: fieldCount=0 撤回单个显式 id>=256 的模板
            if end - pos > MAX_PADDING or any(b != 0 for b in payload[pos:end]):
                raise SetParseError("withdraw_record_has_trailing_bytes")
            version = registry.withdraw(exporter, domain_id, template_id)
            events.append(
                {
                    "exporter": exporter,
                    "domain": domain_id,
                    "templateId": template_id,
                    "version": version,
                    "event": "withdrawn",
                    "fieldCount": 0,
                    "fields": [],
                    "recordOffset": record_offset,
                }
            )
            break

        fields: list[FieldSpec] = []
        for index in range(field_count):
            if pos + 4 > end:
                raise SetParseError("template_truncated: 字段指示符不完整")
            word = _u16(payload, pos)
            declared_len = _u16(payload, pos + 2)
            is_enterprise = bool(word & ENTERPRISE_BIT)
            ie_id = word & 0x7FFF
            ent_number: int | None = None
            spec_offset = pos  # 相对 Set payload 的指示符偏移，与数据记录偏移同基准
            pos += 4
            if is_enterprise:
                if pos + 4 > end:
                    raise SetParseError("template_truncated: 缺少 Enterprise Number")
                ent_number = _u32(payload, pos)
                pos += 4
            name, ie_type = ie_config.lookup(is_enterprise, ent_number, ie_id)

            # 长度合法性（注册表级别，避免产出永远无法解释的数据模板）
            if declared_len == VARIABLE_LEN:
                if ie_type not in ("utf8",):
                    raise SetParseError(
                        f"variable_length_not_allowed: 字段 {name} 类型 {ie_type} 不支持变长"
                    )
            elif ie_type == "uint" and declared_len not in UINT_LENGTHS:
                raise SetParseError(
                    f"bad_uint_length: 字段 {name} 声明长度 {declared_len}"
                )
            elif ie_type == "ipv4" and declared_len != 4:
                raise SetParseError(f"bad_ipv4_length: 字段 {name} 声明长度 {declared_len}")
            fields.append(
                FieldSpec(
                    name=name,
                    ie_id=ie_id,
                    ie_type=ie_type,
                    length=declared_len,
                    enterprise=is_enterprise,
                    enterprise_number=ent_number,
                    offset_in_template=spec_offset,
                )
            )

        record = registry.define(exporter, domain_id, template_id, tuple(fields))
        events.append(
            {
                "exporter": exporter,
                "domain": domain_id,
                "templateId": template_id,
                "version": record.version,
                "event": "defined",
                "fieldCount": len(fields),
                "fields": [f.as_dict() for f in fields],
                "minRecordLength": record.min_record_len,
                "recordOffset": record_offset,
            }
        )
    return events


def _parse_data_set(
    payload: bytes,
    set_id: int,
    exporter: str,
    domain_id: int,
    registry: TemplateRegistry,
) -> tuple[list[dict], dict | None]:
    """解析 Data Set。返回 (流量记录, 未知模板保留项)；解析失败由上层隔离整个 Set。"""
    template = registry.active(exporter, domain_id, set_id)
    if template is None:
        # 未定义/已撤回：保留原始 Set，绝不以后来的模板倒灌解释
        return [], {
            "reason": "unknown_template",
            "templateId": set_id,
        }

    records: list[dict] = []
    pos = 0
    end = len(payload)
    rec_index = 0
    while pos < end:
        # 只有剩余装不下最短合法记录时，尾部才可能是零 padding
        if end - pos < template.min_record_len:
            pos = _check_tail_padding(payload, pos, template.min_record_len)
            break
        record_offset = pos
        start = pos
        values: dict[str, dict] = {}
        for fspec in template.fields:
            if fspec.length == VARIABLE_LEN:
                raw, pos = _read_varlen(payload, pos, end)
            else:
                if pos + fspec.length > end:
                    raise SetParseError(
                        f"field_overrun: 字段 {fspec.name} 需要 {fspec.length} 字节，超出 Set 边界"
                    )
                raw = payload[pos:pos + fspec.length]
                pos += fspec.length
            values[fspec.name] = {
                "ieId": fspec.ie_id,
                "enterprise": fspec.enterprise,
                "enterpriseNumber": fspec.enterprise_number,
                "type": fspec.ie_type,
                "value": _decode_value(fspec.ie_type, raw),
            }
        if pos == start:  # 防御：零消耗记录（理论上本范围模板不会出现）
            raise SetParseError("zero_length_record: 解析未消耗任何字节")
        records.append(
            {
                "exporter": exporter,
                "domain": domain_id,
                "templateId": set_id,
                "templateVersion": template.version,
                "recordIndex": rec_index,
                "recordOffset": record_offset,
                "values": values,
            }
        )
        rec_index += 1
    return records, None


def parse_message(
    data: bytes,
    exporter: str,
    source: str,
    message_index: int,
    registry: TemplateRegistry,
    ie_config: IEConfig,
) -> dict:
    """解析单个 IPFIX 报文；返回结构化结果。报文级错误抛 MessageError。"""
    if len(data) < HEADER_LEN:
        raise MessageError(
            f"message_truncated: 实际 {len(data)} 字节，不足 {HEADER_LEN} 字节报文头"
        )
    version = _u16(data, 0)
    if version != IPFIX_VERSION:
        raise MessageError(f"bad_version: {version}，仅支持 IPFIX v10")
    declared_len = _u16(data, 2)
    if declared_len != len(data):
        raise MessageError(
            f"bad_message_length: 头部声明 {declared_len}，实际 {len(data)}"
        )
    export_time = _u32(data, 4)
    sequence = _u32(data, 8)
    domain_id = _u32(data, 12)

    result = {
        "source": source,
        "messageIndex": message_index,
        "exporter": exporter,
        "domain": domain_id,
        "exportTime": export_time,
        "sequenceNumber": sequence,
        "messageLength": declared_len,
        "flowRecords": [],
        "templateEvents": [],
        "unexplainedSets": [],
    }

    pos = HEADER_LEN
    set_index = 0
    while pos < len(data):
        if len(data) - pos < SET_HDR_LEN:
            raise MessageError(
                f"message_truncated: 偏移 {pos} 处 Set 头不完整（报文尾截断）"
            )
        set_id = _u16(data, pos)
        set_len = _u16(data, pos + 2)
        set_offset = pos
        if set_len < SET_HDR_LEN:
            raise MessageError(f"bad_set_length: 偏移 {set_offset} setLength={set_len}")
        if pos + set_len > len(data):
            raise MessageError(
                f"message_truncated: 偏移 {set_offset} 的 Set 声明 {set_len} 字节，超出报文边界"
            )
        payload = data[pos + SET_HDR_LEN: pos + set_len]
        try:
            if set_id == TEMPLATE_SET_ID:
                events = _parse_template_set(
                    payload, exporter, domain_id, registry, ie_config
                )
                for ev in events:
                    ev.update(
                        {
                            "source": source,
                            "messageIndex": message_index,
                            "setOffset": set_offset,
                            "setIndex": set_index,
                        }
                    )
                result["templateEvents"].extend(events)
            elif set_id >= MIN_DATA_SET_ID:
                records, unknown = _parse_data_set(
                    payload, set_id, exporter, domain_id, registry
                )
                for rec in records:
                    rec.update(
                        {
                            "source": source,
                            "messageIndex": message_index,
                            "setOffset": set_offset,
                            "setIndex": set_index,
                        }
                    )
                result["flowRecords"].extend(records)
                if unknown is not None:
                    result["unexplainedSets"].append(
                        {
                            "exporter": exporter,
                            "domain": domain_id,
                            "source": source,
                            "messageIndex": message_index,
                            "setOffset": set_offset,
                            "setIndex": set_index,
                            "setId": set_id,
                            "setLength": set_len,
                            "reason": unknown["reason"],
                            "rawHex": payload.hex(),
                        }
                    )
            else:
                # 3..255 为保留 ID（3 为 Options Template Set，本范围不实现）
                raise SetParseError(f"unsupported_set_id: {set_id}")
        except SetParseError as exc:
            # 隔离该 Set：模板类 Set 整体不生效，数据类 Set 不保留半截记录
            result["unexplainedSets"].append(
                {
                    "exporter": exporter,
                    "domain": domain_id,
                    "source": source,
                    "messageIndex": message_index,
                    "setOffset": set_offset,
                    "setIndex": set_index,
                    "setId": set_id,
                    "setLength": set_len,
                    "reason": str(exc),
                    "rawHex": payload.hex(),
                }
            )
        pos += set_len
        set_index += 1
    return result


def parse_capture(
    capture_path: str | Path,
    ie_path: str | Path,
    base_dir: str | Path | None = None,
) -> dict:
    """按 capture.json 的到达顺序离线解析整段捕获。"""
    capture_path = Path(capture_path)
    base = Path(base_dir) if base_dir is not None else capture_path.parent
    entries = json.loads(capture_path.read_text(encoding="utf-8"))
    ie_config = IEConfig.load(ie_path)
    registry = TemplateRegistry()

    out = {
        "flowRecords": [],
        "templateHistory": [],
        "unexplainedSets": [],
        "messageErrors": [],
        "messages": [],
    }
    for index, entry in enumerate(entries):
        msg_path = base / entry["file"]
        source = entry["file"]
        data = msg_path.read_bytes()
        try:
            parsed = parse_message(
                data, entry["exporter"], source, index, registry, ie_config
            )
        except MessageError as exc:
            # 报文失败：已读的一半不当作完整数据；注册表保持失败前状态
            out["messageErrors"].append(
                {
                    "source": source,
                    "messageIndex": index,
                    "exporter": entry["exporter"],
                    "error": str(exc),
                }
            )
            continue
        out["flowRecords"].extend(parsed["flowRecords"])
        out["templateHistory"].extend(parsed["templateEvents"])
        out["unexplainedSets"].extend(parsed["unexplainedSets"])
        out["messages"].append(
            {
                "source": source,
                "messageIndex": index,
                "exporter": parsed["exporter"],
                "domain": parsed["domain"],
                "exportTime": parsed["exportTime"],
                "sequenceNumber": parsed["sequenceNumber"],
                "messageLength": parsed["messageLength"],
                "flowRecordCount": len(parsed["flowRecords"]),
                "templateEventCount": len(parsed["templateEvents"]),
                "unexplainedSetCount": len(parsed["unexplainedSets"]),
            }
        )
    return out
