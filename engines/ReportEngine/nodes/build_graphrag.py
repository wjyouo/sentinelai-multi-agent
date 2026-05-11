"""LangGraph node: build knowledge graph and run GraphRAG queries."""

from loguru import logger
from ..state import ReportGraphState
from ..graphrag import StateParser, ForumParser, GraphBuilder, GraphStorage
from ..nodes.graphrag_query_node import GraphRAGQueryNode


class BuildGraphRagNode:
    def __init__(self, ctx):
        self.ctx = ctx
        self._query_node = GraphRAGQueryNode(ctx.llm_client)

    def __call__(self, state: ReportGraphState) -> dict:
        logger.info("GraphRAG 已启用，开始构建知识图谱...")
        try:
            knowledge_graph = self._build_graph(state)
            return {"knowledge_graph": knowledge_graph}
        except Exception as e:
            logger.exception(f"GraphRAG 异常: {e}")
            return {"knowledge_graph": None}

    def _build_graph(self, state: ReportGraphState):
        from app.config import settings
        state_parser = StateParser()
        reports = state.get("normalized_reports", {})
        forum_logs = state.get("forum_logs", "")

        loaded_states = {}
        engines = ['query', 'media', 'insight']
        engine_map = {'query': 'query_engine', 'media': 'media_engine', 'insight': 'insight_engine'}
        for eng in engines:
            report_text = reports.get(engine_map[eng], "")
            if report_text and not report_text.startswith("【"):
                state_path = state_parser.find_state_json_from_text(report_text)
                if state_path:
                    ps = state_parser.parse_from_file(eng, state_path)
                    if ps:
                        loaded_states[eng] = ps

        forum_entries = ForumParser().parse(forum_logs) if forum_logs else []
        graph = GraphBuilder().build(state["query"], loaded_states, forum_entries)
        GraphStorage().save(graph, state.get("report_id", ""), None)
        logger.info(f"知识图谱构建完成: {graph.get_stats()}")
        return graph
