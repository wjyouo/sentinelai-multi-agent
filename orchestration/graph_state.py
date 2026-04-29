"""
顶层协调 LangGraph 状态定义。

将 run_report_generation() 中的全流程数据平铺为 TypedDict，
覆盖从"输入检查"到"报告生成完毕"的所有阶段。
"""

from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict


class OrchestrationState(TypedDict, total=False):
    """顶层协调图的 LangGraph 状态"""

    # ── 输入参数（由调用方注入） ──
    query: str
    custom_template: str
    task_id: str
    stream_handler: Any  # Optional[Callable]

    # ── 阶段 1: 输入就绪检查 ──
    check_result: Dict[str, Any]
    input_ready: bool

    # ── 阶段 2: 文件加载 ──
    content: Dict[str, Any]  # reports, forum_logs, states

    # ── 阶段 3: 报告生成 ──
    generation_result: Dict[str, Any]

    # ── 最终输出 ──
    html_content: str
    report_file_path: str
    report_file_relative_path: str
    report_file_name: str
    state_file_path: str
    ir_file_path: str

    # ── 全局状态 ──
    status: str  # pending / running / completed / error
    error: Optional[str]
