"""逻辑 CSV 记录读取。

输入文件可能包含引号字段与字段内换行(orders.csv 的 o6)，因此绝不能按物理行
切块。这里用标准库 csv 解析成逻辑记录，并按要求把空字段(无论是否带引号)
统一规整为 None，COPY 时再写成空字段交给数据库转成 NULL。

逻辑行号 logical_row 为 1-based、不含表头的数据记录序号。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CsvRecord:
    logical_row: int          # 1-based，不含表头
    values: tuple[str | None, ...]
    raw_key: str | None       # 首列(主键)的原始文本，便于溯源


def read_logical_records(path: str | Path, encoding: str) -> list[CsvRecord]:
    """把整个 CSV 读成有序逻辑记录列表。"""
    records: list[CsvRecord] = []
    with open(path, "r", encoding=encoding, newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return records
        ncols = len(header)
        for idx, row in enumerate(reader, start=1):
            # csv 可能因结尾空行产生空记录；跳过完全为空的物理残片。
            if len(row) == 1 and row[0] == "":
                continue
            # 规整列数与空值：缺列补 None，空串(含引号空串)统一成 None。
            values: list[str | None] = []
            for i in range(ncols):
                cell = row[i] if i < len(row) else None
                values.append(None if cell is None or cell == "" else cell)
            records.append(
                CsvRecord(
                    logical_row=idx,
                    values=tuple(values),
                    raw_key=values[0] if values else None,
                )
            )
    return records
