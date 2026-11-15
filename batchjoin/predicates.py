"""三值逻辑（TRUE / FALSE / UNKNOWN）与标量基数规则。

候选集合以「有序 list、保留 None（SQL NULL）、保留重复」的形式从引擎传入。
这里刻意不使用 Python set：set 会折叠重复值，并且 None 与普通值混在一起时
无法再区分「空候选」「仅含 NULL 的候选」「含 NULL 与普通值的候选」三种状态，
而这三者在 IN / NOT IN 下语义不同。
"""

from __future__ import annotations

from typing import Any

# JSON 侧用 true / false / null 表达三值；内部用字符串使分支显式可读。
TRUE = True
FALSE = False
UNKNOWN = None


def in_result(outer_value: Any, candidates: list[Any]) -> bool | None:
    """SQL: outer_value IN (candidates...)。

    - 找到 SQL 等号匹配 -> TRUE；
    - 无匹配但候选非空且涉及 NULL 比较 -> UNKNOWN（JSON null）；
    - 候选为空（含外键为 NULL、相关子查询零行）-> 一律 FALSE，
      即使 outer_value 本身是 NULL（NULL IN (空集) = FALSE）。
    """

    # 空候选：相关子查询没有任何行。SQL 规定 x IN (空集) 恒为 FALSE。
    if not candidates:
        return FALSE

    # 外值为 NULL：任何等值比较都是 UNKNOWN；候选非空，结果不可能是 TRUE。
    if outer_value is None:
        return UNKNOWN

    has_null = False
    for candidate in candidates:  # 线性扫描，不构造 set，保留 NULL 与重复。
        if candidate is None:
            has_null = True
            continue
        # 关联键已限定为整数等值；这里的值比较对应 SQL 的 `=`，
        # 由 SQLite 取数后的 Python 值做同类型比较即可，None 已在上方处理。
        if candidate == outer_value:
            return TRUE

    # 没有等值命中：候选里出现过 NULL 意味着至少一次比较为 UNKNOWN -> UNKNOWN；
    # 全是非 NULL 候选且都不相等 -> FALSE。
    return UNKNOWN if has_null else FALSE


def not_in_result(outer_value: Any, candidates: list[Any]) -> bool | None:
    """SQL: outer_value NOT IN (...)，按三值逻辑对 IN 取反。

    NOT TRUE -> FALSE；NOT FALSE -> TRUE；NOT UNKNOWN -> UNKNOWN。
    """

    matched = in_result(outer_value, candidates)
    if matched is TRUE:
        return FALSE
    if matched is FALSE:
        return TRUE
    return UNKNOWN


def exists_result(candidates_rows: list[tuple]) -> bool:
    """EXISTS 只看是否存在匹配行。

    匹配行里任何列为 NULL 都不影响存在性（行存在即为 TRUE）。
    引擎只在「等值关联键命中」时才会把行放进 candidates_rows，
    外键为 NULL 时该列表为空，自然得到 FALSE，NULL 内键同理不会被命中。
    """

    return bool(candidates_rows)


def scalar_result(candidates: list[Any]) -> Any:
    """标量子查询：零行 -> NULL；一行 -> 该值（含 NULL）；多行 -> 报错。

    不能像 SQLite 那样静默取首行，调用方负责在 CardinalityError 中补充
    具体外行 / 内行身份。
    """

    if len(candidates) == 0:
        return None
    if len(candidates) == 1:
        return candidates[0]
    raise _ScalarCardinality(candidates)


class _ScalarCardinality(RuntimeError):
    """内部信号：标量子查询候选超过一行；引擎捕获后补全身份信息。"""

    def __init__(self, candidates: list[Any]) -> None:
        super().__init__("scalar subquery returned more than one row")
        self.candidates = candidates
