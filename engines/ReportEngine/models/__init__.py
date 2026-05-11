"""
Report Engine状态管理模块。

导出 ReportState/ReportMetadata，供Agent与上层调度共享。
"""

from .state import ReportState, ReportMetadata

__all__ = ["ReportState", "ReportMetadata"]
