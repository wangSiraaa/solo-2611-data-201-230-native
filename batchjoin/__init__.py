"""受限批量关联执行器。

只接受经 schema 白名单校验的 typed JSON plan，不解析任意 SQL：
- 一张外表、至多两张内表、一个整数等值关联键；
- 表达式仅限 EXISTS / 标量子查询 / IN / NOT IN。
"""

from .plan import Plan, Schema, load_schema, load_plan, PlanError
from .engine import execute_plan, CardinalityError, ExecutionStats

__all__ = [
    "Plan",
    "Schema",
    "load_schema",
    "load_plan",
    "PlanError",
    "execute_plan",
    "CardinalityError",
    "ExecutionStats",
]
