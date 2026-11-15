"""Restricted relational ETL executor with per-row provenance.

Public API:
    run(plan_path, output_dir)  -> dict report
    trace(plan_path)            -> dict with groups and their contributions
    verify(plan_path)           -> dict with verification report
"""

from .api import run, trace, verify
from .engine import EtlError, PlanError, SourceError

__all__ = ["EtlError", "PlanError", "SourceError", "run", "trace", "verify"]
