"""
MediaEngine LangGraph 图定义。

通过工厂函数 build_media_graph() 构建 StateGraph，
将现有 BaseNode 子类包装为 LangGraph 节点函数。
所有业务逻辑保留在原有节点中，本文件只做薄适配。
"""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List

from langgraph.graph import END, START, StateGraph
from loguru import logger

from .graph_state import MediaGraphState
from .nodes import (
    FirstSearchNode,
    FirstSummaryNode,
    ReflectionNode,
    ReflectionSummaryNode,
    ReportFormattingNode,
    ReportStructureNode,
)
from .state.state import Paragraph, State
from .utils import format_search_results_for_prompt


# ────────────────────────────────────────────────────────────────
# 辅助函数：TypedDict ↔ dataclass 转换
# ────────────────────────────────────────────────────────────────

def _paragraphs_to_dicts(paragraphs: List[Paragraph]) -> list[dict]:
    return [p.to_dict() for p in paragraphs]


def _rebuild_state_from_graph(graph_state: MediaGraphState) -> State:
    """从 LangGraph 状态重建 dataclass State。"""
    from .state.state import Paragraph
    paragraphs = [Paragraph.from_dict(d) for d in graph_state.get("paragraphs", [])]
    return State(
        query=graph_state.get("query", ""),
        report_title=graph_state.get("report_title", ""),
        paragraphs=paragraphs,
        final_report=graph_state.get("final_report", ""),
        is_completed=graph_state.get("is_completed", False),
    )


# ────────────────────────────────────────────────────────────────
# 条件边函数
# ────────────────────────────────────────────────────────────────

def _should_continue_reflection(state: MediaGraphState) -> str:
    count = state.get("current_reflection_count", 0)
    max_ref = state.get("max_reflections", 2)
    if count < max_ref:
        return "reflect_again"
    return "next_paragraph"


def _has_more_paragraphs(state: MediaGraphState) -> str:
    idx = state.get("current_paragraph_index", 0)
    paragraphs = state.get("paragraphs", [])
    if idx < len(paragraphs):
        return "process_next"
    return "all_done"


# ────────────────────────────────────────────────────────────────
# 搜索执行辅助
# ────────────────────────────────────────────────────────────────

def _execute_search_and_convert(agent, search_output: dict, search_query: str, search_tool: str) -> list[dict]:
    """执行 Bocha/Anspire 搜索工具并转换结果为标准 dict 列表。"""
    search_kwargs: Dict[str, Any] = {}
    if search_tool in ["comprehensive_search", "web_search_only"]:
        search_kwargs["max_results"] = 10

    logger.info("  - 执行网络搜索...")
    search_response = agent.execute_search_tool(search_tool, search_query, **search_kwargs)

    search_results: list[dict] = []
    if search_response and search_response.webpages:
        max_results = min(len(search_response.webpages), 10)
        for result in search_response.webpages[:max_results]:
            search_results.append({
                "title": result.name,
                "url": result.url,
                "content": result.snippet,
                "score": None,
                "raw_content": result.snippet,
                "published_date": result.date_last_crawled,
            })

    if search_results:
        _message = f"  - 找到 {len(search_results)} 个搜索结果"
        for j, r in enumerate(search_results, 1):
            date_info = f" (发布于: {r.get('published_date', 'N/A')})" if r.get("published_date") else ""
            _message += f"\n    {j}. {r['title'][:50]}...{date_info}"
        logger.info(_message)
    else:
        logger.info("  - 未找到搜索结果")

    return search_results


# ────────────────────────────────────────────────────────────────
# 工厂函数：构建 StateGraph
# ────────────────────────────────────────────────────────────────

def build_media_graph(agent) -> Any:
    """
    构建 MediaEngine 的 LangGraph StateGraph。

    Args:
        agent: DeepSearchAgent 实例（MediaEngine）。

    Returns:
        编译后的 CompiledStateGraph。
    """

    def _pc(data: dict):
        """便捷调用 progress_callback。"""
        cb = getattr(agent, 'progress_callback', None)
        if cb:
            cb(data)

    def node_generate_structure(state: MediaGraphState) -> dict:
        query = state["query"]
        _pc({"status": "structure", "message": "正在生成报告结构...", "progress_pct": 10})
        logger.info(f"\n{'=' * 60}")
        logger.info(f"[LangGraph] 生成报告结构: {query}")

        structure_node = ReportStructureNode(agent.llm_client, query)
        new_state = State()
        new_state = structure_node.mutate_state(state=new_state)

        paragraphs = _paragraphs_to_dicts(new_state.paragraphs)
        _message = f"报告结构已生成，共 {len(paragraphs)} 个段落:"
        for i, p in enumerate(paragraphs, 1):
            _message += f"\n  {i}. {p['title']}"
        logger.info(_message)

        return {
            "report_title": new_state.report_title,
            "paragraphs": paragraphs,
            "current_paragraph_index": 0,
            "current_reflection_count": 0,
        }

    def node_initial_search(state: MediaGraphState) -> dict:
        idx = state["current_paragraph_index"]
        paragraphs = state["paragraphs"]
        para = paragraphs[idx]
        total = len(paragraphs)

        pct = int(20 + (idx + 0.3) / total * 60)
        _pc({
            "status": "processing",
            "message": f"处理段落 {idx + 1}/{total}: {para['title']}",
            "progress_pct": pct,
            "paragraph_current": idx + 1,
            "paragraph_total": total,
        })

        logger.info(f"\n[步骤 2.{idx + 1}] 处理段落: {para['title']}")
        logger.info("-" * 50)

        search_input = {"title": para["title"], "content": para["content"]}
        logger.info("  - 生成搜索查询...")
        search_output = agent.first_search_node.run(search_input)
        search_query = search_output["search_query"]
        search_tool = search_output.get("search_tool", "comprehensive_search")
        reasoning = search_output["reasoning"]

        logger.info(f"  - 搜索查询: {search_query}")
        logger.info(f"  - 选择的工具: {search_tool}")
        logger.info(f"  - 推理: {reasoning}")

        search_results = _execute_search_and_convert(agent, search_output, search_query, search_tool)

        # 更新段落搜索历史
        updated_paragraphs = deepcopy(paragraphs)
        research = updated_paragraphs[idx].setdefault("research", {})
        history = research.setdefault("search_history", [])
        for r in search_results:
            history.append({
                "query": search_query,
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "score": r.get("score"),
                "paragraph_title": para["title"],
                "search_tool": search_tool,
                "has_result": True,
                "timestamp": datetime.now().isoformat(),
            })
        if not search_results:
            history.append({
                "query": search_query,
                "url": "",
                "title": "未找到结果",
                "content": "本次搜索未返回结果或调用失败",
                "score": None,
                "paragraph_title": para["title"],
                "search_tool": search_tool,
                "has_result": False,
                "timestamp": datetime.now().isoformat(),
            })

        return {
            "paragraphs": updated_paragraphs,
            "current_search_output": search_output,
            "current_search_results": search_results,
        }

    def node_initial_summary(state: MediaGraphState) -> dict:
        idx = state["current_paragraph_index"]
        paragraphs = state["paragraphs"]
        para = paragraphs[idx]
        search_results = state.get("current_search_results", [])
        search_output = state.get("current_search_output", {})

        logger.info("  - 生成初始总结...")
        summary_input = {
            "title": para["title"],
            "content": para["content"],
            "search_query": search_output.get("search_query", ""),
            "search_results": format_search_results_for_prompt(
                search_results, agent.config.SEARCH_CONTENT_MAX_LENGTH
            ),
        }

        summary_text = agent.first_summary_node.run(summary_input)

        updated_paragraphs = deepcopy(paragraphs)
        updated_paragraphs[idx]["research"]["latest_summary"] = summary_text

        logger.info("  - 初始总结完成")
        return {
            "paragraphs": updated_paragraphs,
            "current_reflection_count": 0,
        }

    def node_reflection_search(state: MediaGraphState) -> dict:
        idx = state["current_paragraph_index"]
        paragraphs = state["paragraphs"]
        para = paragraphs[idx]
        reflection_count = state.get("current_reflection_count", 0)
        max_ref = state.get("max_reflections", 2)

        logger.info(f"  - 反思 {reflection_count + 1}/{max_ref}...")

        reflection_input = {
            "title": para["title"],
            "content": para["content"],
            "paragraph_latest_state": para["research"].get("latest_summary", ""),
        }

        reflection_output = agent.reflection_node.run(reflection_input)
        search_query = reflection_output["search_query"]
        search_tool = reflection_output.get("search_tool", "comprehensive_search")
        reasoning = reflection_output["reasoning"]

        logger.info(f"    反思查询: {search_query}")
        logger.info(f"    选择的工具: {search_tool}")
        logger.info(f"    反思推理: {reasoning}")

        search_results = _execute_search_and_convert(agent, reflection_output, search_query, search_tool)

        updated_paragraphs = deepcopy(paragraphs)
        research = updated_paragraphs[idx].setdefault("research", {})
        history = research.setdefault("search_history", [])
        for r in search_results:
            history.append({
                "query": search_query,
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "score": r.get("score"),
                "paragraph_title": para["title"],
                "search_tool": search_tool,
                "has_result": True,
                "timestamp": datetime.now().isoformat(),
            })
        if not search_results:
            history.append({
                "query": search_query,
                "url": "",
                "title": "未找到结果",
                "content": "本次搜索未返回结果或调用失败",
                "score": None,
                "paragraph_title": para["title"],
                "search_tool": search_tool,
                "has_result": False,
                "timestamp": datetime.now().isoformat(),
            })

        return {
            "paragraphs": updated_paragraphs,
            "current_search_output": reflection_output,
            "current_search_results": search_results,
        }

    def node_reflection_summary(state: MediaGraphState) -> dict:
        idx = state["current_paragraph_index"]
        paragraphs = state["paragraphs"]
        para = paragraphs[idx]
        search_results = state.get("current_search_results", [])
        search_output = state.get("current_search_output", {})
        reflection_count = state.get("current_reflection_count", 0)

        reflection_summary_input = {
            "title": para["title"],
            "content": para["content"],
            "search_query": search_output.get("search_query", ""),
            "search_results": format_search_results_for_prompt(
                search_results, agent.config.SEARCH_CONTENT_MAX_LENGTH
            ),
            "paragraph_latest_state": para["research"].get("latest_summary", ""),
        }

        updated_summary = agent.reflection_summary_node.run(reflection_summary_input)

        updated_paragraphs = deepcopy(paragraphs)
        updated_paragraphs[idx]["research"]["latest_summary"] = updated_summary
        updated_paragraphs[idx]["research"]["reflection_iteration"] = reflection_count + 1

        new_count = reflection_count + 1
        max_ref = state.get("max_reflections", 2)
        logger.info(f"    反思 {new_count} 完成")

        result: dict = {
            "paragraphs": updated_paragraphs,
            "current_reflection_count": new_count,
        }

        if new_count >= max_ref:
            updated_paragraphs[idx]["research"]["is_completed"] = True
            total = len(updated_paragraphs)
            progress = (idx + 1) / total * 100
            logger.info(f"段落处理完成 ({progress:.1f}%)")
            pct = int(20 + (idx + 1) / total * 60)
            _pc({
                "status": "processing",
                "message": f"段落 {idx + 1}/{total} 完成",
                "progress_pct": pct,
                "paragraph_current": idx + 1,
                "paragraph_total": total,
            })
            result["paragraphs"] = updated_paragraphs
            result["current_paragraph_index"] = idx + 1

        return result

    def node_format_report(state: MediaGraphState) -> dict:
        _pc({"status": "finalizing", "message": "正在生成最终报告...", "progress_pct": 90})
        logger.info(f"\n[步骤 3] 生成最终报告...")
        paragraphs = state["paragraphs"]

        report_data = []
        for para in paragraphs:
            report_data.append({
                "title": para["title"],
                "paragraph_latest_state": para.get("research", {}).get("latest_summary", ""),
            })

        try:
            final_report = agent.report_formatting_node.run(report_data)
        except Exception as e:
            logger.info(f"LLM格式化失败，使用备用方法: {str(e)}")
            final_report = agent.report_formatting_node.format_report_manually(
                report_data, state.get("report_title", "深度研究报告")
            )

        logger.info("最终报告生成完成")
        return {
            "final_report": final_report,
            "is_completed": True,
        }

    def node_save_report(state: MediaGraphState) -> dict:
        _pc({"status": "saving", "message": "正在保存报告...", "progress_pct": 95})

        if not state.get("save_report", True):
            return {}

        final_report = state.get("final_report", "")
        query = state.get("query", "")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        query_safe = "".join(
            c for c in query if c.isalnum() or c in (" ", "-", "_")
        ).rstrip()
        query_safe = query_safe.replace(" ", "_")[:30]

        filename = f"deep_search_report_{query_safe}_{timestamp}.md"
        filepath = os.path.join(agent.config.OUTPUT_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(final_report)
        logger.info(f"报告已保存到: {filepath}")

        if agent.config.SAVE_INTERMEDIATE_STATES:
            dc_state = _rebuild_state_from_graph(state)
            dc_state.final_report = final_report
            dc_state.is_completed = True
            state_filename = f"state_{query_safe}_{timestamp}.json"
            state_filepath = os.path.join(agent.config.OUTPUT_DIR, state_filename)
            dc_state.save_to_file(state_filepath)
            logger.info(f"状态已保存到: {state_filepath}")

        return {}

    # ── 构建 StateGraph ──

    graph = StateGraph(MediaGraphState)

    graph.add_node("generate_structure", node_generate_structure)
    graph.add_node("initial_search", node_initial_search)
    graph.add_node("initial_summary", node_initial_summary)
    graph.add_node("reflection_search", node_reflection_search)
    graph.add_node("reflection_summary", node_reflection_summary)
    graph.add_node("check_more_paragraphs", lambda state: {})
    graph.add_node("format_report", node_format_report)
    graph.add_node("persist_report", node_save_report)

    graph.add_edge(START, "generate_structure")
    graph.add_edge("generate_structure", "initial_search")
    graph.add_edge("initial_search", "initial_summary")
    graph.add_edge("initial_summary", "reflection_search")
    graph.add_edge("reflection_search", "reflection_summary")

    graph.add_conditional_edges(
        "reflection_summary",
        _should_continue_reflection,
        {
            "reflect_again": "reflection_search",
            "next_paragraph": "check_more_paragraphs",
        },
    )
    graph.add_conditional_edges(
        "check_more_paragraphs",
        _has_more_paragraphs,
        {
            "process_next": "initial_search",
            "all_done": "format_report",
        },
    )

    graph.add_edge("format_report", "persist_report")
    graph.add_edge("persist_report", END)

    return graph.compile()
