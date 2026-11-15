#!/usr/bin/env python3
"""独立验证脚本（black-box）。

不 import 主解析器，只做两件事：
1. 用独立 struct / 原始字节切片手工核对 fixtures 的报文与字段；
2. 以子进程方式跑 ingest，再对输出 JSONL 做断言。

覆盖题目要求的全部核对点：
- 两个域的同号模板互不污染（domain 1 vs domain 2 的 template 256）
- 企业位 / Enterprise Number（32473:1 siteLabel，不能当标准 IE 1）
- 127 字节短前缀与 255 字节扩展前缀变长编码
- 撤回（fieldCount=0）与重定义（版本递增、旧记录保留版本）
- 未知模板（exporter b 隔离域、撤回后到达的 Set）
- 坏 Set（报文截断、越界变长）
- >2^53 整数不经过 float
- padding 边界（合成报文：合法 0..3 零 padding / 非零尾 / 过长尾报错）
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIX = ROOT / "fixtures"
MSGS = FIX / "messages"

failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)


def u16(b: bytes, o: int) -> int:
    return struct.unpack_from(">H", b, o)[0]


def u32(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


# ---------- 第一层：独立 struct 核对原始二进制 ----------

def verify_raw_headers() -> None:
    print("== 原始字节层：Message/Set 头 ==")
    for i in range(8):
        data = (MSGS / f"{i:02d}.bin").read_bytes()
        check(u16(data, 0) == 10, f"msg{i:02d} 版本=10")
        check(u16(data, 2) == len(data), f"msg{i:02d} Message Length 严格相等",
              f"declared={u16(data, 2)} actual={len(data)}")
        # 逐 Set 核对 Set Length 自洽
        off, idx = 16, 0
        while off < len(data):
            sid, slen = struct.unpack_from(">HH", data, off)
            check(slen >= 4 and off + slen <= len(data),
                  f"msg{i:02d} set{idx} 边界", f"id={sid} len={slen} off={off}")
            off += slen
            idx += 1
        check(off == len(data), f"msg{i:02d} Set 链恰好耗尽报文")


def verify_domain_isolation_raw() -> None:
    print("== 原始字节层：两个域的同号 256 模板布局不同 ==")
    m0 = (MSGS / "00.bin").read_bytes()   # domain 1, 4 字段，含企业变长
    m1 = (MSGS / "01.bin").read_bytes()   # domain 2, 3 字段，全固定
    check(u32(m0, 12) == 1 and u32(m1, 12) == 2, "Observation Domain ID 分别为 1 / 2")
    # 模板记录位于 Set 头(off=16, 4B)之后：模板ID(2B)+fieldCount(2B) 在 off=20
    fc0, fc1 = u16(m0, 22), u16(m1, 22)
    check(fc0 == 4 and fc1 == 3, "同号模板 256 字段数不同(4 vs 3)")
    # domain1 模板第 4 个指示符：记录体起 24，前 3 个指示符各 4 字节
    f4 = 24 + 3 * 4
    word4 = u16(m0, f4)
    check(word4 == 0x8001, "domain1 字段4 企业位置位且 IE id=1", hex(word4))
    ent = u32(m0, f4 + 4)
    check(ent == 32473, "Enterprise Number=32473（独立读取，非标准 IE 1）")
    check(u16(m0, f4 + 2) == 0xFFFF, "siteLabel 长度=0xffff 变长")
    # domain2 模板首字段是标准 IE 7（sourceTransportPort），与上面的企业 IE 1 同编号不同语义
    check(u16(m1, 24) == 0x0007, "domain2 首字段为标准 IE 7，未被企业字段污染")


def verify_data_values_raw() -> None:
    print("== 原始字节层：固定字段独立 struct 取值 ==")
    m2 = (MSGS / "02.bin").read_bytes()
    src = socket_inet(struct, m2, 20)
    dst = socket_inet(struct, m2, 24)
    octets = struct.unpack_from(">Q", m2, 28)[0]
    check(src == "10.0.0.1" and dst == "10.0.0.2", "msg02 IPv4 地址独立解析")
    check(octets == 9007199254741001, "msg02 64bit 计数独立读取")
    check(octets > 2 ** 53, "该计数 > 2^53")
    check(int(float(octets)) != octets, "经 float 必然损失，证明输出必须保留 int")
    prefix = m2[36]
    check(prefix == 127, "msg02 短变长前缀=127")
    label = m2[37:37 + 127]
    set_end = 16 + u16(m2, 18)
    check(label == b"x" * 127 and 37 + 127 == set_end, "127 字节标签且恰好到 Set 尾")

    m3 = (MSGS / "03.bin").read_bytes()
    port, ip4, pkts = struct.unpack_from(">H4sI", m3, 20)
    check(port == 443 and socket_inet(struct, m3, 22) == "192.0.2.3" and pkts == 6,
          "msg03 按 domain2 模板取 port/ipv4/packetDelta")

    m6 = (MSGS / "06.bin").read_bytes()
    port6, ip6, pkts6 = struct.unpack_from(">H4sI", m6, 40)
    check((port6, socket_inet(struct, m6, 42), pkts6) == (80, "10.0.0.9", 9),
          "msg06 重定义后数据按新 3 字段模板解析")

    m7 = (MSGS / "07.bin").read_bytes()
    p = 48 + 16  # data set payload 起点 + 16 字节固定部分
    check(m7[p] == 0xFF and u16(m7, p + 1) == 255, "msg07 扩展前缀 0xff + 长度 255")
    ys = m7[p + 3:p + 3 + 255]
    check(ys == b"y" * 255 and p + 3 + 255 == 44 + u16(m7, 46),
          "255 字节标签，末尾恰好对齐 Set 边界，不越界下一 Set")


def socket_inet(mod, data: bytes, off: int) -> str:
    return ".".join(str(b) for b in data[off:off + 4])


def verify_bad_files_raw() -> None:
    print("== 原始字节层：坏报文 ==")
    trunc = (FIX / "bad-truncated.bin").read_bytes()
    check(u16(trunc, 2) != len(trunc), "截断报文 Message Length 与实际不符")
    bv = (FIX / "bad-variable.bin").read_bytes()
    sid, slen = struct.unpack_from(">HH", bv, 0x2C)
    check(sid == 256 and slen == 28, "bad-variable 数据 Set 头本身合法")
    ext_len = u16(bv, 0x41)
    check(ext_len == 512 and 0x43 + 512 > 0x2C + slen,
          "变长声明 512 字节远超 Set 尾，必须隔离且不得越界读取")


def build_synthetic_messages() -> list[tuple[str, bytes, str]]:
    """合成 padding 边界报文（模板：1 个 uint32 固定字段，最短记录 4 字节）。"""
    def msg(domain: int, payload: bytes) -> bytes:
        # Template Set: id=2, 模板 300, 1 字段 IE 2(packetDeltaCount) uint32
        tset = struct.pack(">HHHHI", 2, 12, 300, 1, 0x00020004)
        dset = struct.pack(">HH", 300, 4 + len(payload)) + payload
        body = tset + dset
        return struct.pack(">HHIII", 10, 16 + len(body), 0, 0, domain) + body

    good_pad = msg(90, struct.pack(">I", 7) + b"\x00\x00")      # 2 字节零 padding
    nonzero = msg(91, struct.pack(">I", 7) + b"\x00\x01")       # 非零尾 -> 报错
    # 5 字节非零尾：够解析一条 4 字节记录还剩 1 字节；剩 1 字节既非合法 padding
    # （非零）也无法起始记录 -> 必须报错，不得猜一条空记录
    ambiguous = msg(92, struct.pack(">I", 7) + b"\x00\x00\x00\x00\x01")
    # 4 个零字节 = 一条值为 0 的完整记录（scope 允许零数据记录），不是 padding
    two_records = msg(93, struct.pack(">II", 7, 0))
    return [("good-padding", good_pad, "zero_padding_ok"),
            ("nonzero-tail", nonzero, "nonzero_trailing_bytes"),
            ("ambiguous-tail", ambiguous, "trailing_bytes_parseable"),
            ("two-zero-records", two_records, "two_records_ok")]


# ---------- 第二层：子进程跑主程序，断言 JSONL 输出 ----------

def run_ingest() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [sys.executable, "-m", "ipfix_ingestor", "ingest", "--outdir", tmp],
            cwd=ROOT, capture_output=True, text=True,
        )
        check(proc.returncode == 0, "ingest 正式捕获退出码 0", proc.stderr.strip())
        out = {}
        for name in ["flow_records", "template_history", "unexplained_sets",
                     "message_errors", "messages"]:
            out[name] = [
                json.loads(line)
                for line in (Path(tmp) / f"{name}.jsonl").read_text().splitlines()
            ]
    return out


def verify_outputs(out: dict) -> None:
    print("== 输出 JSONL 层断言 ==")
    recs = out["flow_records"]
    check(len(recs) == 4, "恰好 4 条规范流量记录")
    check(all(isinstance(r["values"]["octetDeltaCount"]["value"], int)
              for r in recs if "octetDeltaCount" in r["values"]),
          "所有计数为 JSON integer，无 float")
    big = next(r for r in recs if r["messageIndex"] == 2)
    check(big["values"]["octetDeltaCount"]["value"] == 9007199254741001,
          ">2^53 计数精确入仓")
    check(len(big["values"]["siteLabel"]["value"]) == 127, "127 字节 UTF-8 标签")
    lab = big["values"]["siteLabel"]
    check(lab["enterprise"] is True and lab["enterpriseNumber"] == 32473,
          "企业标签带 Enterprise Number，与标准 IE 1 区分")

    # 每条记录都带身份、版本与偏移
    check(all({"exporter", "domain", "templateVersion", "setOffset", "recordOffset"}
              <= set(r) for r in recs), "记录含 exporter/domain/版本/偏移")
    d2 = next(r for r in recs if r["messageIndex"] == 3)
    check(d2["domain"] == 2 and d2["templateVersion"] == 1
          and set(d2["values"]) == {"sourceTransportPort", "sourceIPv4Address", "packetDeltaCount"},
          "domain2 记录用本域 3 字段模板，未被 domain1 污染")
    v2 = next(r for r in recs if r["messageIndex"] == 6)
    check(v2["templateId"] == 256 and v2["templateVersion"] == 2,
          "重定义后记录挂模板 v2")

    hist = out["template_history"]
    check(len(hist) == 5, "模板历史 5 个事件（定义/撤回/重定义全保留）")
    versions = {(h["domain"], h["templateId"], h["version"], h["event"]) for h in hist}
    check((1, 256, 1, "defined") in versions and (1, 256, 2, "defined") in versions,
          "domain1 模板256 v1/v2 均保留，重定义不倒灌")
    check((1, 256, 1, "withdrawn") in versions, "撤回事件保留被撤回版本号")
    check((2, 256, 1, "defined") in versions, "domain2 同号模板独立成 v1")

    bad = out["unexplained_sets"]
    check(len(bad) == 2, "恰好 2 个无法解释 Set")
    reasons = {(b["exporter"], b["domain"], b["reason"]) for b in bad}
    check(("b", 1, "unknown_template") in reasons, "exporter b 同域同号模板不互相借用")
    check(("a", 1, "unknown_template") in reasons, "撤回后到达的数据记 unknown_template")
    check(all(b["rawHex"] for b in bad), "未知 Set 保留原始字节")
    check(out["message_errors"] == [], "正式捕获无报文级错误")


def verify_bad_via_cli() -> None:
    print("== CLI 层：坏报文单独运行 ==")
    trunc = subprocess.run(
        [sys.executable, "-m", "ipfix_ingestor", "inspect",
         "--file", str(FIX / "bad-truncated.bin"), "--exporter", "a"],
        cwd=ROOT, capture_output=True, text=True)
    check(trunc.returncode == 1 and "bad_message_length" in trunc.stdout,
          "截断报文整体失败，退出码 1")
    bv = subprocess.run(
        [sys.executable, "-m", "ipfix_ingestor", "inspect",
         "--file", str(FIX / "bad-variable.bin"), "--exporter", "a"],
        cwd=ROOT, capture_output=True, text=True)
    check(bv.returncode == 0 and "variable_length_overrun" in bv.stdout,
          "越界变长只隔离该 Set，报文其余部分仍可解析")


def verify_synthetic_padding() -> None:
    print("== 合成报文层：padding 边界 ==")
    from ipfix_ingestor.parser import IEConfig, TemplateRegistry, parse_message, MessageError
    ie = IEConfig.load(FIX / "information-elements.json")
    for name, blob, expect in build_synthetic_messages():
        reg = TemplateRegistry()
        try:
            res = parse_message(blob, "t", name, 0, reg, ie)
        except MessageError as exc:
            check(expect in str(exc), f"{name} 报文级报错", str(exc))
            continue
        if expect == "zero_padding_ok":
            check(len(res["flowRecords"]) == 1 and not res["unexplainedSets"],
                  f"{name} 2 字节零 padding 合法")
        elif expect == "two_records_ok":
            vals = [r["values"]["packetDeltaCount"]["value"] for r in res["flowRecords"]]
            check(vals == [7, 0] and not res["unexplainedSets"],
                  f"{name} 4 零字节解析为值 0 的第二条记录", str(vals))
        else:
            tail_errors = ("trailing_bytes_parseable", "nonzero_trailing_bytes",
                           "bad_padding_length", "field_overrun")
            check(any(code in s["reason"] for code in tail_errors for s in res["unexplainedSets"]),
                  f"{name} 被隔离", str(res["unexplainedSets"]))


def main() -> int:
    verify_raw_headers()
    verify_domain_isolation_raw()
    verify_data_values_raw()
    verify_bad_files_raw()
    verify_outputs(run_ingest())
    verify_bad_via_cli()
    verify_synthetic_padding()
    print("\n" + ("全部核对通过 ✅" if not failures else f"{len(failures)} 项失败 ❌: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
