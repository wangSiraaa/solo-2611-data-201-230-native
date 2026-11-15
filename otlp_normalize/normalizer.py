"""对外交付入口：normalize 与 inspect。

规模定位：单次几十点的离线批处理，纯标准库，不依赖 OTel Collector 或指标数据库。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .engine import run_engine
from .model import FactRow, OriginRecord, UnconfirmedRecord, load_exact
from .parser import Counters, ParseResult, parse_batch
from .report import render_facts, render_inspect, render_origins, render_unconfirmed


class NormalizationResult:
    def __init__(
        self,
        sources: list[str],
        facts: list[FactRow],
        origins: list[OriginRecord],
        unconfirmed: list[UnconfirmedRecord],
    ) -> None:
        self.sources = sources
        self.facts = facts
        self.origins = origins
        self.unconfirmed = unconfirmed

    # -- 结构化输出 --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "fact_table": render_facts(self.facts),
            "origin_mapping": render_origins(self.origins),
            "unconfirmed": render_unconfirmed(self.unconfirmed),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def write_jsonl(self, out_dir: str | Path) -> list[Path]:
        """写三张 JSONL 表：facts / origins / unconfirmed。"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = {
            out / "facts.jsonl": render_facts(self.facts),
            out / "origins.jsonl": render_origins(self.origins),
            out / "unconfirmed.jsonl": render_unconfirmed(self.unconfirmed),
        }
        written: list[Path] = []
        for path, rows in paths.items():
            with path.open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            written.append(path)
        return written

    def inspect_text(self) -> str:
        return render_inspect(self.sources, self.facts, self.origins, self.unconfirmed)


def _coerce_batch(item: str | Path | dict[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(item, dict):
        # 直传 dict 也把 JSON number 规约成 Decimal，与“从文件读取”路径一致；
        # 已有 Decimal 原样保留（含尾零），bool 保持 bool（Python 中 bool 是 int 子类）。
        return _coerce_numbers(item), "<dict>"
    if isinstance(item, (str, Path)):
        path = Path(item)
        # 先看是不是现存路径；否则按 JSON 文本解析（避免对超长文本做 stat）
        try:
            if path.exists():
                return load_exact(path.read_text(encoding="utf-8")), str(path)
        except OSError:
            pass
        return load_exact(str(item)), "<str>"
    raise TypeError(f"unsupported input: {type(item)!r}")


def _coerce_numbers(obj: Any) -> Any:
    if isinstance(obj, Decimal) or isinstance(obj, bool) or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return Decimal(obj)
    if isinstance(obj, float):
        return Decimal(str(obj))  # 经最短十进制文本，不引入二进制尾数
    if isinstance(obj, dict):
        return {k: _coerce_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_numbers(v) for v in obj]
    return obj


def normalize(inputs: str | Path | dict[str, Any] | Iterable[Any]) -> NormalizationResult:
    """读取一个或多个 OTLP JSON 指标批，输出归一化结果。

    参数可以是文件路径、JSON 字符串、已解析的 batch dict，或它们的可迭代集合。
    多个批次共享同一组计数器与流身份空间；不按文件名选点，跨文件的
    同流同时间点同样参与折叠/冲突判定。
    """
    if isinstance(inputs, (str, Path, dict)):
        items: list[Any] = [inputs]
    else:
        items = list(inputs)

    counters = Counters()
    parsed = ParseResult()
    sources: list[str] = []
    for item in items:
        batch, source = _coerce_batch(item)
        sources.append(source)
        parsed.extend(parse_batch(batch, source, counters))

    facts = run_engine(parsed.points, parsed.origins, parsed.unconfirmed, counters)

    # 防御性自检：每个原点必须有最终归宿
    for o in parsed.origins:
        if o.status == "pending":
            raise RuntimeError(f"internal error: origin {o.origin_id} left without a final status")

    return NormalizationResult(sources, facts, parsed.origins, parsed.unconfirmed)


def inspect(inputs: str | Path | dict[str, Any] | Iterable[Any]) -> str:
    """归一化并返回人类可读的诊断文本；不做告警、推断或可视化。"""
    return normalize(inputs).inspect_text()
