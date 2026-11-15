"""受限 TCP 传输层：AXFR/IXFR 报文收发、围栏校验与 RFC 1995 差量解析。

只对接给定 zone 的单个主服务器，不做递归、不做名字压缩（线格式解码全部
交给 dnspython）。报文按 RFC 1035 的 2 字节长度前缀分帧，逐条消息以
one_rr_per_rrset 解析，避免 dnspython 把多个同名 SOA/同名 RR 合并。
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.rrset

from .model import ALLOWED_TYPES, ALLOWED_CLASS, canonical_owner
from . import serialutil

DEFAULT_TIMEOUT = 10.0


class TransferError(Exception):
    """传送失败基类：任何子类失败都意味着保留旧快照、不发布新快照。"""


class ConnectionTransferError(TransferError):
    """连接中断、超时或最后一条消息被截断。"""


class ResponseTransferError(TransferError):
    """响应码、标志或成员记录不符合权威传送约定。"""


class FenceError(TransferError):
    """AXFR 围栏不完整或 zone/serial 不一致。"""


class IxfrStructureError(TransferError):
    """IXFR 响应不符合 RFC 1995 的 SOA-删-SOA-增 结构。"""


class DeltaGapError(TransferError):
    """差量旧 serial 接不上本地版本/前段新 serial，版本链存在缺口。"""


class DeleteMissingError(TransferError):
    """删除集要求删除的 RDATA 在当时基线中不存在。"""


@dataclass(frozen=True)
class WireRR:
    """从线上消息解出的单条 RR（一个 RRset 只含一个 rdata）。"""

    name: dns.name.Name
    ttl: int
    rdclass: int
    rdtype: int
    rdata: dns.rdata.Rdata

    @property
    def serial(self) -> int:
        return int(self.rdata.serial)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Delta:
    """RFC 1995 的一段差量：old SOA -> 删除集 -> new SOA -> 增加集。

    old_soa/new_soa 保留围栏 SOA 线上对象（含 TTL 与完整 RDATA）。
    """

    old_soa: WireRR
    new_soa: WireRR
    deletes: tuple[WireRR, ...]
    adds: tuple[WireRR, ...]

    @property
    def old_serial(self) -> int:
        return self.old_soa.serial

    @property
    def new_serial(self) -> int:
        return self.new_soa.serial


@dataclass(frozen=True)
class IxfrResponse:
    """IXFR 请求的解析结果。

    kind:
      nochange —— 只有一个 SOA，服务端确认 serial 未变；
      ixfr     —— 一段或多段 RFC 1995 差量；
      axfr     —— 服务端回退为完整 AXFR 形态，必须整库重建。

    opening/closing 保留首尾围栏 SOA 的线上对象，供最终 SOA 核对与发布。
    """

    kind: str
    high_serial: int
    deltas: tuple[Delta, ...] = ()
    axfr_rrs: tuple[WireRR, ...] = ()
    opening: WireRR | None = None
    closing: WireRR | None = None


# --------------------------------------------------------------------------
# 查询构造
# --------------------------------------------------------------------------

def build_axfr_query(zone: str) -> dns.message.Message:
    q = dns.message.make_query(zone, dns.rdatatype.AXFR, dns.rdataclass.IN)
    q.flags &= ~dns.flags.RD  # 显式不请求递归
    return q


def build_ixfr_query(
    zone: str, serial: int, soa_rdata: dns.rdata.Rdata
) -> dns.message.Message:
    serialutil.validate(serial)
    apex = dns.name.from_text(zone)
    q = dns.message.make_query(zone, dns.rdatatype.IXFR, dns.rdataclass.IN)
    q.flags &= ~dns.flags.RD
    authority = dns.rrset.RRset(apex, dns.rdataclass.IN, dns.rdatatype.SOA)
    authority.add(soa_rdata)
    q.authority.append(authority)
    return q


def make_template_soa(zone: str, serial: int) -> dns.rdata.Rdata:
    """构造 IXFR authority 用的 SOA RDATA，仅 serial 被服务端使用。"""
    zone = canonical_owner(dns.name.from_text(zone))
    text = f"ns.{zone} hostmaster.{zone} {serial} 3600 600 86400 60"
    return dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.SOA, text)


# --------------------------------------------------------------------------
# TCP 收发
# --------------------------------------------------------------------------

def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _tcp_exchange(
    host: str, port: int, query: dns.message.Message, timeout: float
) -> list[dns.message.Message]:
    wire = query.to_wire()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(struct.pack("!H", len(wire)) + wire)
            messages: list[dns.message.Message] = []
            while True:
                try:
                    header = _recv_exact(sock, 2)
                except EOFError:
                    break  # 服务端在消息边界正常关闭连接，传送结束
                (length,) = struct.unpack("!H", header)
                try:
                    payload = _recv_exact(sock, length)
                    message = dns.message.from_wire(
                        payload, one_rr_per_rrset=True
                    )
                except EOFError as exc:
                    # 已读到长度前缀却收不齐消息体：半传输
                    raise ConnectionTransferError(
                        "连接在 DNS 消息中间中断，传送不完整"
                    ) from exc
                except dns.exception.DNSException as exc:
                    raise ConnectionTransferError(
                        f"响应消息解码失败（可能被截断）: {exc}"
                    ) from exc
                messages.append(message)
    except (OSError, socket.timeout) as exc:
        raise ConnectionTransferError(f"与 {host}:{port} 的 TCP 传送失败: {exc}") from exc
    return messages


def _is_apex_soa(rr: WireRR, apex: dns.name.Name) -> bool:
    return (
        rr.rdtype == dns.rdatatype.SOA
        and canonical_owner(rr.name) == canonical_owner(apex)
    )


def _validate_member(rr: WireRR, apex: dns.name.Name) -> None:
    if rr.rdclass != ALLOWED_CLASS:
        raise ResponseTransferError(
            f"收到非 IN class 记录: {rr.name} class={rr.rdclass}"
        )
    if rr.rdtype not in ALLOWED_TYPES:
        raise ResponseTransferError(
            f"收到范围外类型 {dns.rdatatype.to_text(rr.rdtype)}（owner={rr.name}）"
        )
    if not rr.name.is_subdomain(apex):
        raise ResponseTransferError(
            f"记录 owner {rr.name} 不属于 zone {apex}"
        )


def _flatten(
    messages: list[dns.message.Message],
    query: dns.message.Message,
    apex: dns.name.Name,
) -> list[WireRR]:
    rrs: list[WireRR] = []
    for message in messages:
        if message.id != query.id:
            raise ResponseTransferError("响应事务 ID 与请求不一致")
        if message.rcode() != dns.rcode.NOERROR:
            raise ResponseTransferError(
                f"服务端返回错误码 {dns.rcode.to_text(message.rcode())}"
            )
        if not (message.flags & dns.flags.AA):
            raise ResponseTransferError("响应缺少 AA 标志，不是权威数据")
        for rrset in message.answer:  # one_rr_per_rrset：每个仅一条 rdata
            for rdata in rrset:
                rr = WireRR(rrset.name, rrset.ttl, rrset.rdclass, rdata.rdtype, rdata)
                _validate_member(rr, apex)
                rrs.append(rr)
    if not rrs:
        raise ResponseTransferError("响应不含任何 answer 记录")
    return rrs


# --------------------------------------------------------------------------
# AXFR
# --------------------------------------------------------------------------

def validate_axfr(rrs: list[WireRR], apex: dns.name.Name) -> int:
    """校验 AXFR 围栏并返回统一 serial；任何不符都抛 FenceError。"""
    if len(rrs) < 2:
        raise FenceError("AXFR 记录数不足，缺少 SOA 围栏")
    first, last = rrs[0], rrs[-1]
    if not (_is_apex_soa(first, apex) and _is_apex_soa(last, apex)):
        raise FenceError("AXFR 必须以 zone 顶点 SOA 开始并以 SOA 结束")
    if first.serial != last.serial:
        raise FenceError(
            f"AXFR 围栏 serial 不一致: 开始={first.serial} 结束={last.serial}"
        )
    for rr in rrs[1:-1]:
        if _is_apex_soa(rr, apex):
            raise FenceError("AXFR 内部出现顶点 SOA，围栏结构被破坏")
    return first.serial


def fetch_axfr(
    host: str, port: int, zone: str, timeout: float = DEFAULT_TIMEOUT
) -> tuple[int, list[WireRR]]:
    apex = dns.name.from_text(zone)
    query = build_axfr_query(zone)
    messages = _tcp_exchange(host, port, query, timeout)
    rrs = _flatten(messages, query, apex)
    serial = validate_axfr(rrs, apex)
    return serial, rrs


# --------------------------------------------------------------------------
# IXFR（RFC 1995）与 AXFR 回退识别
# --------------------------------------------------------------------------

def _parse_ixfr(rrs: list[WireRR], apex: dns.name.Name) -> IxfrResponse:
    if not _is_apex_soa(rrs[0], apex):
        raise IxfrStructureError("IXFR 响应必须以当前顶点 SOA 开始")
    opening = rrs[0]
    high_serial = opening.serial

    # 无变化：整个响应只有一个当前 SOA
    if len(rrs) == 1:
        return IxfrResponse(
            "nochange", high_serial, opening=opening, closing=opening
        )

    # AXFR 回退：首个 SOA 之后紧跟普通记录，而不是旧版本 SOA
    if not _is_apex_soa(rrs[1], apex):
        serial = validate_axfr(rrs, apex)
        return IxfrResponse(
            "axfr", serial, axfr_rrs=tuple(rrs),
            opening=opening, closing=rrs[-1],
        )

    deltas: list[Delta] = []
    closing: WireRR | None = None
    i = 1
    while i < len(rrs):
        if not _is_apex_soa(rrs[i], apex):
            raise IxfrStructureError(
                f"第 {len(deltas) + 1} 段差量缺少旧版本 SOA 标记"
            )
        old_soa = rrs[i]
        i += 1

        deletes: list[WireRR] = []
        while i < len(rrs) and not _is_apex_soa(rrs[i], apex):
            deletes.append(rrs[i])
            i += 1
        if i >= len(rrs):
            raise IxfrStructureError("删除集后缺少新版本 SOA 标记")

        new_soa = rrs[i]
        i += 1
        adds: list[WireRR] = []
        while i < len(rrs) and not _is_apex_soa(rrs[i], apex):
            adds.append(rrs[i])
            i += 1

        deltas.append(
            Delta(old_soa, new_soa, tuple(deletes), tuple(adds))
        )
        # 下一个 SOA 要么是下一段的旧 SOA，要么是收尾的当前 SOA
        if i < len(rrs):
            candidate = rrs[i]
            if i == len(rrs) - 1 and _is_apex_soa(candidate, apex):
                if candidate.serial != high_serial:
                    raise IxfrStructureError(
                        f"收尾 SOA serial {candidate.serial} 与起始 SOA "
                        f"{high_serial} 不一致"
                    )
                closing = candidate
                break
            # 否则回到循环头，作为下一段旧 SOA 继续解析

    if closing is None:
        raise IxfrStructureError("IXFR 缺少收尾 SOA 围栏")
    if not deltas:
        raise IxfrStructureError("IXFR 响应未解析出任何差量段")
    return IxfrResponse(
        "ixfr", high_serial, tuple(deltas),
        opening=opening, closing=closing,
    )


def fetch_ixfr(
    host: str,
    port: int,
    zone: str,
    current_serial: int,
    template_soa: dns.rdata.Rdata,
    timeout: float = DEFAULT_TIMEOUT,
) -> IxfrResponse:
    apex = dns.name.from_text(zone)
    query = build_ixfr_query(zone, current_serial, template_soa)
    messages = _tcp_exchange(host, port, query, timeout)
    rrs = _flatten(messages, query, apex)
    return _parse_ixfr(rrs, apex)
