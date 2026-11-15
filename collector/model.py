"""快照数据模型：规范身份、RDATA 折叠、RRset 视图与 JSON 持久化。

记录身份（删除/增加的匹配键）为规范 owner + type + class + RDATA 线格式，
不含 TTL；TTL 变化通过“删旧加新”表现。TXT 的多个字串属于同一条 RDATA，
整体序列化为一个线格式字节串，绝不拆成多条记录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import dns.name
import dns.rdataclass
import dns.rdatatype

# 题目限定的记录类型；围栏 SOA 也在其中。
ALLOWED_TYPES = frozenset(
    {
        dns.rdatatype.SOA,
        dns.rdatatype.NS,
        dns.rdatatype.A,
        dns.rdatatype.AAAA,
        dns.rdatatype.CNAME,
        dns.rdatatype.MX,
        dns.rdatatype.TXT,
    }
)
ALLOWED_CLASS = dns.rdataclass.IN


def canonical_owner(name: dns.name.Name) -> str:
    """域名比较大小写不敏感：统一为小写绝对名字文本。"""
    return name.to_text().lower()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Record:
    """一条 RDATA 级别的记录（一个 RRset 可含多条 RDATA）。"""

    owner: str          # 规范小写绝对域名
    rtype: int          # dns.rdatatype 值
    rdclass: int        # 本采集器固定 IN
    rdata_wire: bytes   # 未压缩的 RDATA 线格式，作为身份的一部分
    rdata_text: str     # 稳定展示文本
    ttl: int
    source: str         # 来源标注（哪次传送写入）

    @property
    def identity(self) -> tuple[str, int, int, bytes]:
        return (self.owner, self.rtype, self.rdclass, self.rdata_wire)

    @property
    def type_text(self) -> str:
        return dns.rdatatype.to_text(self.rtype)

    def to_json(self) -> dict:
        return {
            "owner": self.owner,
            "type": self.type_text,
            "class": dns.rdataclass.to_text(self.rdclass),
            "rdata": self.rdata_text,
            "rdata_wire": self.rdata_wire.hex(),
            "ttl": self.ttl,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, item: dict) -> "Record":
        return cls(
            owner=item["owner"],
            rtype=dns.rdatatype.from_text(item["type"]),
            rdclass=dns.rdataclass.from_text(item["class"]),
            rdata_wire=bytes.fromhex(item["rdata_wire"]),
            rdata_text=item["rdata"],
            ttl=int(item["ttl"]),
            source=item["source"],
        )

    @classmethod
    def from_rr(
        cls,
        name: dns.name.Name,
        ttl: int,
        rdclass: int,
        rdata: dns.rdata.Rdata,
        source: str,
    ) -> "Record":
        return cls(
            owner=canonical_owner(name),
            rtype=rdata.rdtype,
            rdclass=rdclass,
            # origin=None：RDATA 内嵌域名按绝对名字输出未压缩线格式
            rdata_wire=rdata.to_wire(None),
            rdata_text=rdata.to_text(),
            ttl=int(ttl),
            source=source,
        )


@dataclass
class RRsetView:
    """同一 owner/type/class 下若干 RDATA 的只读视图。"""

    owner: str
    rtype: int
    rdclass: int
    rdata: list[Record] = field(default_factory=list)

    @property
    def type_text(self) -> str:
        return dns.rdatatype.to_text(self.rtype)

    @property
    def ttl_set(self) -> set[int]:
        return {r.ttl for r in self.rdata}

    def zone_lines(self) -> list[str]:
        # 冻结文件风格：owner ttl IN TYPE rdata，RDATA 按文本稳定排序
        return [
            f"{r.owner} {r.ttl} IN {self.type_text} {r.rdata_text}"
            for r in sorted(self.rdata, key=lambda x: x.rdata_text)
        ]


class Snapshot:
    """某一时刻整个 zone 的权威快照。"""

    def __init__(
        self,
        zone: str,
        serial: int,
        records: dict[tuple[str, int, int, bytes], Record] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.zone = canonical_owner(dns.name.from_text(zone))
        self.serial = serial
        self.records: dict[tuple[str, int, int, bytes], Record] = (
            records if records is not None else {}
        )
        now = utc_now()
        self.created_at = created_at or now
        self.updated_at = updated_at or now

    # ---- 基础操作 -----------------------------------------------------

    def add(self, record: Record) -> bool:
        """加入一条 RDATA；同身份重复时折叠，保留最早来源。

        返回 True 表示新写入，False 表示重复被折叠。
        """
        existing = self.records.get(record.identity)
        if existing is not None:
            return False
        self.records[record.identity] = record
        return True

    def remove(self, record: Record) -> None:
        """按身份删除；不存在则抛 KeyError，由调用方决定失败语义。"""
        del self.records[record.identity]

    def contains_identity(self, record: Record) -> bool:
        return record.identity in self.records

    def get_identity(self, record: Record) -> Record | None:
        return self.records.get(record.identity)

    def copy(self) -> "Snapshot":
        return Snapshot(
            zone=self.zone,
            serial=self.serial,
            records=dict(self.records),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    # ---- 派生视图 -----------------------------------------------------

    def rrsets(self) -> list[RRsetView]:
        grouped: dict[tuple[str, int, int], RRsetView] = {}
        for rec in self.records.values():
            key = (rec.owner, rec.rtype, rec.rdclass)
            view = grouped.get(key)
            if view is None:
                view = RRsetView(rec.owner, rec.rtype, rec.rdclass)
                grouped[key] = view
            view.rdata.append(rec)
        return sorted(grouped.values(), key=lambda v: (v.owner, v.rtype))

    def soa_record(self) -> Record | None:
        for rec in self.records.values():
            if rec.rtype == dns.rdatatype.SOA and rec.owner == self.zone:
                return rec
        return None

    def soa_serial(self) -> int | None:
        soa = self.soa_record()
        if soa is None:
            return None
        rdata = dns.rdata.from_wire(
            dns.rdataclass.IN, dns.rdatatype.SOA, soa.rdata_wire, 0,
            len(soa.rdata_wire),
        )
        return int(rdata.serial)

    def rrset_signature(self) -> set[tuple[str, str, int, str]]:
        """供独立比对的 (owner, 类型, TTL, RDATA) 集合。"""
        return {
            (r.owner, r.type_text, r.ttl, r.rdata_text)
            for r in self.records.values()
        }

    def sources(self) -> list[str]:
        return sorted({r.source for r in self.records.values()})

    # ---- 持久化 -------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "zone": self.zone,
            "serial": self.serial,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "records": [r.to_json() for r in self.records.values()],
        }

    @classmethod
    def from_json(cls, payload: dict) -> "Snapshot":
        records = {
            (r := Record.from_json(item)).identity: r
            for item in payload["records"]
        }
        return cls(
            zone=payload["zone"],
            serial=int(payload["serial"]),
            records=records,
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    def to_zone_text(self) -> str:
        lines: list[str] = []
        for view in self.rrsets():
            lines.extend(view.zone_lines())
        return "\n".join(lines) + ("\n" if lines else "")
