"""
ReportEngine LangGraph 图定义。

通过工厂函数 build_report_graph() 构建 StateGraph，
将现有 generate_report() 流水线拆分为离散的 LangGraph 节点。
所有业务逻辑保留在原有节点和 Agent 方法中，本文件只做薄适配。
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from langgraph.graph import END, START, StateGraph
from loguru import logger

from .graph_state import ReportGraphState
from .core import TemplateSection, parse_template_sections
from .nodes import (
    ChapterJsonParseError,
    ChapterContentError,
    ChapterValidationError,
    GraphRAGQueryNode,
)
from .graphrag.prompts import format_graph_results_for_prompt
from utils.knowledge_logger import init_knowledge_log


# ────────────────────────────────────────────────────────────────
# 条件边函数
# ────────────────────────────────────────────────────────────────

def _has_more_sections(state: ReportGraphState) -> str:
    """判断是否还有未处理的章节。"""
    idx = state.get("current_section_index", 0)
    sections = state.get("sections", [])
    if idx < len(sections):
        return "generate_next"
    return "all_done"


# ────────────────────────────────────────────────────────────────
# 工厂函数：构建 StateGraph
# ────────────────────────────────────────────────────────────────

def build_report_graph(agent) -> Any:
    """
    构建 ReportEngine 的 LangGraph StateGraph。

    Args:
        agent: ReportAgent 实例，提供节点、LLM、配置等依赖。

    Returns:
        编译后的 CompiledStateGraph，可直接 invoke()。
    """

    # ── 辅助：事件分发器工厂 ──────────────────────────────────
    def _make_emit(state: ReportGraphState):
        """根据当前 state 中的 stream_handler 生成一个安全的 emit 函数。"""
        handler = state.get("stream_handler")

        def emit(event_type: str, payload: Dict[str, Any]):
            if not handler:
                return
            try:
                handler(event_type, payload)
            except Exception as cb_err:
                logger.warning(f"流式事件回调失败: {cb_err}")

        return emit

    # ── 节点适配器 ──────────────────────────────────────────────

    def node_normalize_reports(state: ReportGraphState) -> dict:
        """归一化三引擎报告。"""
        emit = _make_emit(state)
        report_id = state.get("report_id", "")

        # 设置 agent 状态
        agent.state.task_id = report_id
        agent.state.query = state["query"]
        agent.state.metadata.query = state["query"]
        agent.state.mark_processing()
        init_knowledge_log(force_reset=True)

        normalized = agent._normalize_reports(state.get("reports", []))

        logger.info(f"开始生成报告 {report_id}: {state['query']}")
        logger.info(
            f"输入数据 - 报告数量: {len(state.get('reports', []))}, "
            f"论坛日志长度: {len(str(state.get('forum_logs', '')))}"
        )
        emit("stage", {"stage": "agent_start", "report_id": report_id, "query": state["query"]})

        return {"normalized_reports": normalized, "status": "processing"}

    def node_select_template(state: ReportGraphState) -> dict:
        """选择或使用自定义模板。"""
        emit = _make_emit(state)

        template_result = agent._select_template(
            state["query"],
            state.get("reports", []),
            state.get("forum_logs", ""),
            state.get("custom_template", ""),
        )
        template_result = agent._ensure_mapping(
            template_result,
            "模板选择结果",
            expected_keys=["template_name", "template_content"],
        )

        agent.state.metadata.template_used = template_result.get("template_name", "")
        emit("stage", {
            "stage": "template_selected",
            "template": template_result.get("template_name"),
            "reason": template_result.get("selection_reason"),
        })
        emit("progress", {"progress": 10, "message": "模板选择完成"})

        return {"template_result": template_result}

    def node_slice_template(state: ReportGraphState) -> dict:
        """将模板切分为章节列表。"""
        emit = _make_emit(state)
        template_result = state["template_result"]
        template_content = template_result.get("template_content", "")

        sections = agent._slice_template(template_content)
        if not sections:
            raise ValueError("模板无法解析出章节，请检查模板内容。")

        emit("stage", {"stage": "template_sliced", "section_count": len(sections)})

        return {"sections": sections}

    def node_design_layout(state: ReportGraphState) -> dict:
        """构建模板概览并设计文档布局。"""
        emit = _make_emit(state)
        template_result = state["template_result"]
        sections = state["sections"]
        normalized_reports = state["normalized_reports"]
        forum_logs = state.get("forum_logs", "")
        query = state["query"]

        template_text = template_result.get("template_content", "")
        template_overview = agent._build_template_overview(template_text, sections)

        layout_design = agent._run_stage_with_retry(
            "文档设计",
            lambda: agent.document_layout_node.run(
                sections, template_text, normalized_reports,
                forum_logs, query, template_overview,
            ),
            expected_keys=["title", "hero", "tocPlan", "tocTitle"],
        )

        emit("stage", {
            "stage": "layout_designed",
            "title": layout_design.get("title"),
            "toc": layout_design.get("tocTitle"),
        })
        emit("progress", {"progress": 15, "message": "文档标题/目录设计完成"})

        return {"template_overview": template_overview, "layout_design": layout_design}

    def node_plan_word_budget(state: ReportGraphState) -> dict:
        """章节篇幅规划。"""
        emit = _make_emit(state)
        sections = state["sections"]
        layout_design = state["layout_design"]
        normalized_reports = state["normalized_reports"]
        forum_logs = state.get("forum_logs", "")
        query = state["query"]
        template_overview = state["template_overview"]

        word_plan = agent._run_stage_with_retry(
            "章节篇幅规划",
            lambda: agent.word_budget_node.run(
                sections, layout_design, normalized_reports,
                forum_logs, query, template_overview,
            ),
            expected_keys=["chapters", "totalWords", "globalGuidelines"],
            postprocess=agent._normalize_word_plan,
        )

        emit("stage", {
            "stage": "word_plan_ready",
            "chapter_targets": len(word_plan.get("chapters", [])),
        })
        emit("progress", {"progress": 20, "message": "章节字数规划已生成"})

        chapter_targets = {
            entry.get("chapterId"): entry
            for entry in word_plan.get("chapters", [])
            if entry.get("chapterId")
        }

        return {"word_plan": word_plan, "chapter_targets": chapter_targets}

    def node_build_context(state: ReportGraphState) -> dict:
        """构建章节生成上下文、manifest 元数据并初始化存储会话。"""
        emit = _make_emit(state)
        query = state["query"]
        normalized_reports = state["normalized_reports"]
        forum_logs = state.get("forum_logs", "")
        template_result = state["template_result"]
        layout_design = state["layout_design"]
        chapter_targets = state["chapter_targets"]
        word_plan = state["word_plan"]
        template_overview = state["template_overview"]
        report_id = state.get("report_id", "")

        generation_context = agent._build_generation_context(
            query, normalized_reports, forum_logs,
            template_result, layout_design, chapter_targets,
            word_plan, template_overview,
        )

        manifest_meta = {
            "query": query,
            "title": layout_design.get("title") or (
                f"{query} - 舆情洞察报告" if query else template_result.get("template_name")
            ),
            "subtitle": layout_design.get("subtitle"),
            "tagline": layout_design.get("tagline"),
            "templateName": template_result.get("template_name"),
            "selectionReason": template_result.get("selection_reason"),
            "themeTokens": generation_context.get("theme_tokens", {}),
            "toc": {
                "depth": 3,
                "autoNumbering": True,
                "title": layout_design.get("tocTitle") or "目录",
            },
            "hero": layout_design.get("hero"),
            "layoutNotes": layout_design.get("layoutNotes"),
            "wordPlan": {
                "totalWords": word_plan.get("totalWords"),
                "globalGuidelines": word_plan.get("globalGuidelines"),
            },
            "templateOverview": template_overview,
        }
        if layout_design.get("themeTokens"):
            manifest_meta["themeTokens"] = layout_design["themeTokens"]
        if layout_design.get("tocPlan"):
            manifest_meta["toc"]["customEntries"] = layout_design["tocPlan"]

        run_dir = agent.chapter_storage.start_session(report_id, manifest_meta)
        agent._persist_planning_artifacts(run_dir, layout_design, word_plan, template_overview)
        emit("stage", {"stage": "storage_ready", "run_dir": str(run_dir)})

        return {
            "generation_context": generation_context,
            "manifest_meta": manifest_meta,
            "run_dir": run_dir,
        }

    def node_init_graphrag(state: ReportGraphState) -> dict:
        """根据配置初始化 GraphRAG 知识图谱。"""
        emit = _make_emit(state)
        graphrag_enabled = getattr(agent.config, "GRAPHRAG_ENABLED", False)
        knowledge_graph = None
        graphrag_query_node = None

        if graphrag_enabled:
            logger.info("GraphRAG 已启用，开始构建知识图谱...")
            emit("stage", {"stage": "graphrag_building", "message": "正在构建知识图谱"})
            try:
                knowledge_graph = agent._build_knowledge_graph(
                    state["query"],
                    state["normalized_reports"],
                    state.get("forum_logs", ""),
                    state["run_dir"],
                )
                if knowledge_graph:
                    graphrag_query_node = GraphRAGQueryNode(agent.llm_client)
                    graph_stats = knowledge_graph.get_stats()
                    emit("stage", {
                        "stage": "graphrag_built",
                        "node_count": graph_stats.get("total_nodes", 0),
                        "edge_count": graph_stats.get("total_edges", 0),
                    })
                    logger.info(f"知识图谱构建完成: {graph_stats}")
                else:
                    logger.warning("知识图谱构建失败，将使用原始流程")
                    graphrag_enabled = False
            except Exception as graph_error:
                logger.exception(f"GraphRAG 构建异常: {graph_error}")
                graphrag_enabled = False
                emit("stage", {"stage": "graphrag_error", "error": str(graph_error)})

        return {
            "graphrag_enabled": graphrag_enabled,
            "knowledge_graph": knowledge_graph,
            "graphrag_query_node": graphrag_query_node,
            "current_section_index": 0,
            "chapters": [],
        }

    def node_generate_chapter(state: ReportGraphState) -> dict:
        """
        生成当前章节（含重试逻辑）。

        章节生成的重试、GraphRAG 查询、内容稀疏兜底等逻辑
        全部在本节点内闭环处理，避免将重试暴露为图的边。
        """
        emit = _make_emit(state)
        idx = state["current_section_index"]
        sections = state["sections"]
        section = sections[idx]
        chapters = list(state.get("chapters", []))
        generation_context = state["generation_context"]
        chapter_targets = state.get("chapter_targets", {})
        run_dir = state["run_dir"]
        graphrag_enabled = state.get("graphrag_enabled", False)
        knowledge_graph = state.get("knowledge_graph")
        graphrag_qn = state.get("graphrag_query_node")
        word_plan = state.get("word_plan", {})
        template_result = state.get("template_result", {})
        query = state["query"]

        total_chapters = len(sections)
        completed_so_far = len(chapters)
        chapter_max_attempts = max(
            agent._CONTENT_SPARSE_MIN_ATTEMPTS, agent.config.CHAPTER_JSON_MAX_ATTEMPTS
        )

        logger.info(f"生成章节: {section.title}")
        emit("chapter_status", {
            "chapterId": section.chapter_id,
            "title": section.title,
            "status": "running",
        })

        # 章节流式回调
        def chunk_callback(delta: str, meta: Dict[str, Any], section_ref=section):
            emit("chapter_chunk", {
                "chapterId": meta.get("chapterId") or section_ref.chapter_id,
                "title": meta.get("title") or section_ref.title,
                "delta": delta,
            })

        # ── GraphRAG 查询 ──
        chapter_context = generation_context.copy()
        if graphrag_enabled and knowledge_graph and graphrag_qn:
            try:
                max_queries = getattr(agent.config, "GRAPHRAG_MAX_QUERIES", 3)
                chapter_meta = (
                    chapter_targets.get(section.chapter_id, {})
                    if isinstance(chapter_targets, dict) else {}
                )
                emphasis_value = chapter_meta.get("emphasis") or chapter_meta.get("emphasisPoints") or ""
                if isinstance(emphasis_value, list):
                    emphasis_value = "；".join(str(item) for item in emphasis_value if item)
                role_text = getattr(section, "description", None) or chapter_meta.get("rationale") or ""
                if not isinstance(role_text, str):
                    role_text = agent._stringify(role_text)

                section_info = {
                    "title": section.title,
                    "id": section.chapter_id,
                    "role": role_text,
                    "target_words": chapter_meta.get("targetWords", 500),
                    "emphasis": emphasis_value,
                }
                graph_results = graphrag_qn.run(
                    section_info,
                    {
                        "query": query,
                        "template_name": template_result.get("template_name"),
                        "chapters": word_plan.get("chapters", []),
                    },
                    knowledge_graph,
                    max_queries=max_queries,
                )
                if graph_results and graph_results.get("total_nodes", 0) > 0:
                    chapter_context["graph_results"] = graph_results
                    chapter_context["graph_enhancement_prompt"] = format_graph_results_for_prompt(graph_results)
                    logger.info(
                        f"章节 {section.title} GraphRAG 查询完成: "
                        f"{graph_results.get('total_nodes', 0)} 节点"
                    )
            except Exception as gq_err:
                logger.warning(f"GraphRAG 查询失败 ({section.title}): {gq_err}")

        # ── 重试循环 ──
        chapter_payload: Optional[Dict[str, Any]] = None
        attempt = 1
        best_sparse_candidate: Optional[Dict[str, Any]] = None
        best_sparse_score = -1
        fallback_used = False

        while attempt <= chapter_max_attempts:
            try:
                chapter_payload = agent.chapter_generation_node.run(
                    section, chapter_context, run_dir, stream_callback=chunk_callback,
                )
                break
            except (AttributeError, TypeError, KeyError, IndexError, ValueError, json.JSONDecodeError) as structure_error:
                error_type = type(structure_error).__name__
                logger.warning(
                    "章节 {title} 生成过程中发生 {error_type}（第 {attempt}/{total} 次尝试），将尝试重新生成: {error}",
                    title=section.title, error_type=error_type,
                    attempt=attempt, total=chapter_max_attempts, error=structure_error,
                )
                emit("chapter_status", {
                    "chapterId": section.chapter_id,
                    "title": section.title,
                    "status": "retrying" if attempt < chapter_max_attempts else "error",
                    "attempt": attempt, "error": str(structure_error),
                    "reason": "structure_error", "error_type": error_type,
                })
                if attempt >= chapter_max_attempts:
                    raise ChapterJsonParseError(
                        f"{section.title} 章节因 {error_type} 在 {chapter_max_attempts} 次尝试后仍无法生成: {structure_error}"
                    ) from structure_error
                attempt += 1
                continue
            except (ChapterJsonParseError, ChapterContentError, ChapterValidationError) as structured_error:
                if isinstance(structured_error, ChapterContentError):
                    error_kind = "content_sparse"
                    readable_label = "内容密度异常"
                elif isinstance(structured_error, ChapterValidationError):
                    error_kind = "validation"
                    readable_label = "结构校验失败"
                else:
                    error_kind = "json_parse"
                    readable_label = "JSON解析失败"
                if isinstance(structured_error, ChapterContentError):
                    candidate = getattr(structured_error, "chapter_payload", None)
                    candidate_score = getattr(structured_error, "body_characters", 0) or 0
                    if isinstance(candidate, dict) and candidate_score >= 0:
                        if candidate_score > best_sparse_score:
                            best_sparse_candidate = deepcopy(candidate)
                            best_sparse_score = candidate_score
                will_fallback = (
                    isinstance(structured_error, ChapterContentError)
                    and attempt >= chapter_max_attempts
                    and attempt >= agent._CONTENT_SPARSE_MIN_ATTEMPTS
                    and best_sparse_candidate is not None
                )
                logger.warning(
                    "章节 {title} {label}（第 {attempt}/{total} 次尝试）: {error}",
                    title=section.title, label=readable_label,
                    attempt=attempt, total=chapter_max_attempts, error=structured_error,
                )
                status_value = "retrying" if attempt < chapter_max_attempts or will_fallback else "error"
                status_payload: Dict[str, Any] = {
                    "chapterId": section.chapter_id,
                    "title": section.title,
                    "status": status_value,
                    "attempt": attempt, "error": str(structured_error),
                    "reason": error_kind,
                }
                if isinstance(structured_error, ChapterValidationError):
                    validation_errors = getattr(structured_error, "errors", None)
                    if validation_errors:
                        status_payload["errors"] = validation_errors
                if will_fallback:
                    status_payload["warning"] = "content_sparse_fallback_pending"
                emit("chapter_status", status_payload)
                if will_fallback:
                    logger.warning(
                        "章节 {title} 达到最大尝试次数，保留字数最多（约 {score} 字）的版本作为兜底输出",
                        title=section.title, score=best_sparse_score,
                    )
                    chapter_payload = agent._finalize_sparse_chapter(best_sparse_candidate)
                    fallback_used = True
                    break
                if attempt >= chapter_max_attempts:
                    raise
                attempt += 1
                continue
            except Exception as chapter_error:
                if not agent._should_retry_inappropriate_content_error(chapter_error):
                    raise
                logger.warning(
                    "章节 {title} 触发内容安全限制（第 {attempt}/{total} 次尝试），准备重新生成: {error}",
                    title=section.title, attempt=attempt,
                    total=chapter_max_attempts, error=chapter_error,
                )
                emit("chapter_status", {
                    "chapterId": section.chapter_id,
                    "title": section.title,
                    "status": "retrying" if attempt < chapter_max_attempts else "error",
                    "attempt": attempt, "error": str(chapter_error), "reason": "content_filter",
                })
                if attempt >= chapter_max_attempts:
                    raise
                attempt += 1
                continue

        if chapter_payload is None:
            raise ChapterJsonParseError(
                f"{section.title} 章节JSON在 {chapter_max_attempts} 次尝试后仍无法解析"
            )

        chapters.append(chapter_payload)
        completed_chapters = completed_so_far + 1
        chapter_progress = 20 + round(80 * completed_chapters / total_chapters)
        emit("progress", {
            "progress": chapter_progress,
            "message": f"章节 {completed_chapters}/{total_chapters} 已完成",
        })
        completion_status: Dict[str, Any] = {
            "chapterId": section.chapter_id,
            "title": section.title,
            "status": "completed",
            "attempt": attempt,
        }
        if fallback_used:
            completion_status["warning"] = "content_sparse_fallback"
            completion_status["warningMessage"] = agent._CONTENT_SPARSE_WARNING_TEXT
        emit("chapter_status", completion_status)

        return {
            "chapters": chapters,
            "current_section_index": idx + 1,
        }

    def node_compose_document(state: ReportGraphState) -> dict:
        """将已完成章节装订为 Document IR。"""
        emit = _make_emit(state)
        report_id = state.get("report_id", "")
        manifest_meta = state["manifest_meta"]
        chapters = state["chapters"]

        document_ir = agent.document_composer.build_document(
            report_id, manifest_meta, chapters,
        )
        emit("stage", {"stage": "chapters_compiled", "chapter_count": len(chapters)})

        return {"document_ir": document_ir}

    def node_render_html(state: ReportGraphState) -> dict:
        """将 Document IR 渲染为 HTML。"""
        emit = _make_emit(state)
        document_ir = state["document_ir"]

        html_report = agent.renderer.render(document_ir)
        emit("stage", {"stage": "html_rendered", "html_length": len(html_report)})

        agent.state.html_content = html_report
        agent.state.mark_completed()

        return {"html_content": html_report}

    def node_save_report(state: ReportGraphState) -> dict:
        """保存报告文件并记录耗时。"""
        emit = _make_emit(state)
        html_content = state["html_content"]
        document_ir = state["document_ir"]
        report_id = state.get("report_id", "")
        save_report = state.get("save_report", True)

        saved_files: Dict[str, Any] = {}
        if save_report:
            saved_files = agent._save_report(html_content, document_ir, report_id)
            emit("stage", {"stage": "report_saved", "files": saved_files})

        return {"saved_files": saved_files, "status": "completed"}

    # ── 构建 StateGraph ────────────────────────────────────────

    graph = StateGraph(ReportGraphState)

    # 注册节点
    graph.add_node("normalize_reports", node_normalize_reports)
    graph.add_node("select_template", node_select_template)
    graph.add_node("slice_template", node_slice_template)
    graph.add_node("design_layout", node_design_layout)
    graph.add_node("plan_word_budget", node_plan_word_budget)
    graph.add_node("build_context", node_build_context)
    graph.add_node("init_graphrag", node_init_graphrag)
    graph.add_node("generate_chapter", node_generate_chapter)
    graph.add_node("compose_document", node_compose_document)
    graph.add_node("render_html", node_render_html)
    graph.add_node("persist_report", node_save_report)

    # 线性主干
    graph.add_edge(START, "normalize_reports")
    graph.add_edge("normalize_reports", "select_template")
    graph.add_edge("select_template", "slice_template")
    graph.add_edge("slice_template", "design_layout")
    graph.add_edge("design_layout", "plan_word_budget")
    graph.add_edge("plan_word_budget", "build_context")
    graph.add_edge("build_context", "init_graphrag")

    # 章节循环：init_graphrag → 首次判断 → generate_chapter → 判断 → ...
    graph.add_node("check_more_sections", lambda state: {})
    graph.add_edge("init_graphrag", "check_more_sections")
    graph.add_conditional_edges(
        "check_more_sections",
        _has_more_sections,
        {
            "generate_next": "generate_chapter",
            "all_done": "compose_document",
        },
    )
    graph.add_edge("generate_chapter", "check_more_sections")

    # 渲染 & 保存
    graph.add_edge("compose_document", "render_html")
    graph.add_edge("render_html", "persist_report")
    graph.add_edge("persist_report", END)

    return graph.compile()
