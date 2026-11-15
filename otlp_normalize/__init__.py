"""otlp_normalize: OTLP 单调 Sum / 显式桶 Histogram 离线时间性归一化器。"""
from .normalizer import NormalizationResult, inspect, normalize

__all__ = ["normalize", "inspect", "NormalizationResult"]
__version__ = "1.0.0"
