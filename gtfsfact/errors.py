"""错误类型：任何输入问题都导致整个 build 失败，绝不输出半份成功数据。"""


class BuildError(Exception):
    """输入 feed 或构建配置无效；build 终止且不产生输出文件。"""


class UnsupportedError(BuildError):
    """GTFS 中合法、但本实现明确不支持的构造（如 exact_times=0、重叠频率区间）。"""
