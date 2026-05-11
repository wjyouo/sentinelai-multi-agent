"""LangGraph node: design document layout."""

from loguru import logger
from ..state import ReportGraphState
from ..nodes.document_layout_node import DocumentLayoutNode


class DesignLayoutNode:
    _RETRY_ATTEMPTS = 2

    def __init__(self, ctx):
        self.ctx = ctx
        self._node = DocumentLayoutNode(ctx.llm_client)

    def __call__(self, state: ReportGraphState) -> dict:
        query = state["query"]
        reports = state.get("normalized_reports", {})
        forum_logs = state.get("forum_logs", "")
        sections = state.get("template_sections", [])
        template_text = state.get("template_result", {}).get("template_content", "")
        template_overview = state.get("template_overview", {})

        last_error = None
        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            try:
                result = self._node.run(sections, template_text, reports, forum_logs, query, template_overview)
                logger.info(f"文档设计完成: {result.get('title')}")
                return {"layout_design": result}
            except Exception as e:
                last_error = e
                logger.warning(f"文档设计第 {attempt} 次失败: {e}")
        raise last_error
