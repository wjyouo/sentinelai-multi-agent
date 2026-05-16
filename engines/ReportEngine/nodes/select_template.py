"""LangGraph node: select report template."""

import os
from loguru import logger
from ..state import ReportGraphState
from ..nodes.template_selection_node import TemplateSelectionNode


class SelectTemplateNode:
    """
    使用LLM来选择模板
    """
    def __init__(self, ctx):
        self.ctx = ctx
        self._node = TemplateSelectionNode(ctx.llm_client, ctx.config.TEMPLATE_DIR)

    def __call__(self, state: ReportGraphState) -> dict:
        query = state["query"]
        reports = state.get("normalized_reports", {})
        forum_logs = state.get("forum_logs", "")
        custom_template = state.get("custom_template", "")

        if custom_template:
            logger.info("使用用户自定义模板")
            return {"template_result": {"template_name": "custom", "template_content": custom_template, "selection_reason": "用户指定的自定义模板"}}

        template_input = {"query": query, "reports": reports, "forum_logs": forum_logs}
        try:
            result = self._node.run(template_input)
            logger.info(f"选择模板: {result['template_name']}")
            return {"template_result": result}
        except Exception as e:
            logger.error(f"模板选择失败，使用默认模板: {e}")
            fallback = {"template_name": "社会公共热点事件分析报告模板", "template_content": _fallback_content(), "selection_reason": "模板选择失败，使用默认模板"}
            return {"template_result": fallback}


def _fallback_content() -> str:
    return """# 社会公共热点事件分析报告\n## 执行摘要\n本报告针对当前社会热点事件进行综合分析。\n## 事件概况\n### 基本信息\n## 舆情态势分析\n### 整体趋势\n## 结论与展望\n---\n*报告类型：社会公共热点事件分析*\n"""
