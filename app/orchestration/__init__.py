"""
顶层协调模块。

通过 LangGraph StateGraph 将"输入就绪检查 → 文件加载 → 报告生成"
三个阶段串联为可视化的有向图，替换 flask_interface.py 中的
run_report_generation 顺序逻辑。
"""

from .graph import build_orchestration_graph
from .graph_state import OrchestrationState

__all__ = ["build_orchestration_graph", "OrchestrationState"]
