"""Partition-completion-event driven sales assembly."""

from .engine import Pipeline, required_partition
from .errors import AssemblyError, BuildError, ConfigError, Rejected

__all__ = [
    "AssemblyError",
    "BuildError",
    "ConfigError",
    "Pipeline",
    "Rejected",
    "required_partition",
]
__version__ = "1.0.0"
