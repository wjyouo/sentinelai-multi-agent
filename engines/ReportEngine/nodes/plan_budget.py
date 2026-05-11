"""LangGraph node: plan word budget."""

from loguru import logger
from ..state import ReportGraphState
from ..nodes.word_budget_node import WordBudgetNode


class PlanBudgetNode:
    _RETRY_ATTEMPTS = 2

    def __init__(self, ctx):
        self.ctx = ctx
        self._node = WordBudgetNode(ctx.llm_client)

    def __call__(self, state: ReportGraphState) -> dict:
        sections = state.get("template_sections", [])
        layout = state.get("layout_design", {})
        reports = state.get("normalized_reports", {})
        forum_logs = state.get("forum_logs", "")
        query = state["query"]
        template_overview = state.get("template_overview", {})

        last_error = None
        for attempt in range(1, self._RETRY_ATTEMPTS + 1):
            try:
                result = self._node.run(sections, layout, reports, forum_logs, query, template_overview)
                result = _normalize(result)
                logger.info(f"篇幅规划完成: {len(result.get('chapters', []))} 章节")
                return {"word_plan": result}
            except Exception as e:
                last_error = e
                logger.warning(f"篇幅规划第 {attempt} 次失败: {e}")
        raise last_error


def _normalize(wp: dict) -> dict:
    chapters = wp.get("chapters", [])
    if isinstance(chapters, dict):
        chapters = list(chapters.values())
    if isinstance(chapters, list):
        wp["chapters"] = [c for c in chapters if isinstance(c, dict)]
    g = wp.get("globalGuidelines")
    if not isinstance(g, list):
        wp["globalGuidelines"] = [g] if g else []
    if not isinstance(wp.get("totalWords"), (int, float)):
        wp["totalWords"] = 10000
    return wp
