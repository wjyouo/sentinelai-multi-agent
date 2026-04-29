"""
ReportEngine LangGraph 状态定义。

将 generate_report() 流水线中的所有中间数据平铺为 TypedDict，
用于 LangGraph StateGraph 的节点间数据流转。
"""

from typing import Any, Dict, List, Optional, Callable
from typing_extensions import TypedDict


class ReportGraphState(TypedDict, total=False):
    """ReportEngine 的 LangGraph 状态"""

    # ── 输入 ──
    query: str
    reports: list  # 原始 reports 列表（来自三引擎）
    forum_logs: str
    custom_template: str
    save_report: bool
    report_id: str
    stream_handler: Any  # Optional[Callable] — TypedDict 不支持 Callable 默认

    # ── 阶段 1: 归一化 ──
    normalized_reports: Dict[str, str]

    # ── 阶段 2: 模板选择 ──
    template_result: Dict[str, Any]

    # ── 阶段 3: 模板切片 ──
    sections: list  # List[TemplateSection]

    # ── 阶段 4: 模板概览 & 文档布局 ──
    template_overview: Dict[str, Any]
    layout_design: Dict[str, Any]

    # ── 阶段 5: 篇幅规划 ──
    word_plan: Dict[str, Any]

    # ── 阶段 6: 生成上下文 & 存储 ──
    generation_context: Dict[str, Any]
    chapter_targets: Dict[str, Any]
    manifest_meta: Dict[str, Any]
    run_dir: Any  # Path 对象

    # ── 阶段 7: GraphRAG ──
    graphrag_enabled: bool
    knowledge_graph: Any  # Optional[Graph]
    graphrag_query_node: Any  # Optional[GraphRAGQueryNode]

    # ── 阶段 8: 章节循环 ──
    current_section_index: int
    chapters: list  # List[Dict] — 已完成的章节 payload

    # ── 阶段 9: 装订 & 渲染 ──
    document_ir: Dict[str, Any]
    html_content: str

    # ── 阶段 10: 保存 ──
    saved_files: Dict[str, Any]

    # ── 全局元信息 ──
    generation_time: float
    status: str  # pending / processing / completed / failed
    error: Optional[str]
