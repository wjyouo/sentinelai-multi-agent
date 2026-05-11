"""LangGraph node: generate all chapters sequentially with retry logic."""

from copy import deepcopy
from pathlib import Path
from loguru import logger
from ..state import ReportGraphState
from ..core import TemplateSection
from ..nodes.chapter_generation_node import ChapterGenerationNode, ChapterJsonParseError, ChapterContentError, ChapterValidationError


class GenerateChaptersNode:
    _SPARSE_MIN_ATTEMPTS = 3

    def __init__(self, ctx):
        self.ctx = ctx
        self._graphrag_node = self._init_graphrag_node(ctx)
        self._node = ChapterGenerationNode(
            ctx.llm_client, ctx.validator, ctx.chapter_storage,
            fallback_llm_clients=ctx.json_rescue_clients,
            error_log_dir=getattr(ctx.config, "JSON_ERROR_LOG_DIR", "logs/json_repair_failures"),
        )

    def __call__(self, state: ReportGraphState) -> dict:
        sections = state.get("template_sections", [])
        generation_context = state.get("generation_context", {})
        chapter_targets = generation_context.get("chapter_directives", {})
        query = state["query"]
        template_result = state.get("template_result", {})
        word_plan = state.get("word_plan", {})
        layout_design = state.get("layout_design", {})
        template_overview = state.get("template_overview", {})
        knowledge_graph = state.get("knowledge_graph")
        graphrag_enabled = state.get("graphrag_enabled", False)
        graphrag_query_node = self._graphrag_node if graphrag_enabled else None
        stream_handler = self.ctx.stream_handler
        report_id = state.get("report_id", "")

        # Attach graph results to context if available
        section_contexts = []
        for section in sections:
            ctx = generation_context.copy()
            if graphrag_enabled and knowledge_graph and graphrag_query_node:
                try:
                    chapter_meta = chapter_targets.get(section.chapter_id, {}) if isinstance(chapter_targets, dict) else {}
                    emphasis = chapter_meta.get("emphasis") or chapter_meta.get("emphasisPoints") or ""
                    if isinstance(emphasis, list):
                        emphasis = "；".join(str(x) for x in emphasis)
                    section_info = {"title": section.title, "id": section.chapter_id, "role": getattr(section, "description", None) or chapter_meta.get("rationale") or "", "target_words": chapter_meta.get("targetWords", 500), "emphasis": emphasis}
                    gr = graphrag_query_node.run(section_info, {"query": query, "template_name": template_result.get("template_name"), "chapters": word_plan.get("chapters", [])}, knowledge_graph, max_queries=min(3, getattr(self.ctx.config, "GRAPHRAG_MAX_QUERIES", 3)))
                    if gr and gr.get("total_nodes", 0) > 0:
                        from ..graphrag.prompts import format_graph_results_for_prompt
                        ctx["graph_results"] = gr
                        ctx["graph_enhancement_prompt"] = format_graph_results_for_prompt(gr)
                except Exception as gq_err:
                    logger.warning(f"GraphRAG 查询失败 ({section.title}): {gq_err}")
            section_contexts.append(ctx)

        chapters = []
        total = len(sections)
        chapter_max_attempts = max(self._SPARSE_MIN_ATTEMPTS, getattr(self.ctx.config, "CHAPTER_JSON_MAX_ATTEMPTS", 2))

        for idx, section in enumerate(sections):
            chap_ctx = section_contexts[idx]
            logger.info(f"生成章节: {section.title}")
            self._emit(state, "chapter_status", {"chapterId": section.chapter_id, "title": section.title, "status": "running"})

            run_dir = self.ctx.chapter_storage.start_session(report_id, {})
            success = False
            use_fallback = False
            chapter_payload = None
            best_sparse, best_score = None, -1

            for attempt in range(1, chapter_max_attempts + 1):
                try:
                    chapter_payload = self._node.run(section, chap_ctx, run_dir, stream_callback=lambda d, m, s=section: self._emit(state, "chapter_chunk", {"chapterId": s.chapter_id, "title": s.title, "delta": d}))
                    success = True
                    break
                except (AttributeError, TypeError, KeyError, IndexError, ValueError) as e:
                    logger.warning(f"章节 {section.title} 结构错误 (第{attempt}次): {e}")
                    self._emit(state, "chapter_status", {"chapterId": section.chapter_id, "title": section.title, "status": "retrying" if attempt < chapter_max_attempts else "error", "attempt": attempt, "error": str(e), "reason": "structure_error"})
                    if attempt >= chapter_max_attempts:
                        raise ChapterJsonParseError(f"{section.title} 在第{chapter_max_attempts}次尝试后仍失败: {e}") from e
                except ChapterContentError as e:
                    candidate = getattr(e, "chapter_payload", None)
                    score = getattr(e, "body_characters", 0) or 0
                    if isinstance(candidate, dict) and score > best_score:
                        best_sparse, best_score = deepcopy(candidate), score
                    logger.warning(f"章节 {section.title} 内容稀疏 (第{attempt}次): {e}")
                    if attempt >= chapter_max_attempts and attempt >= self._SPARSE_MIN_ATTEMPTS and best_sparse:
                        chapter_payload = _finalize_sparse(best_sparse)
                        use_fallback = True
                        success = True
                        break
                    if attempt >= chapter_max_attempts:
                        raise
                except (ChapterJsonParseError, ChapterValidationError) as e:
                    logger.warning(f"章节 {section.title} JSON/校验错误 (第{attempt}次): {e}")
                    if attempt >= chapter_max_attempts:
                        raise
                except Exception as e:
                    if not _is_content_safety_error(e):
                        raise
                    logger.warning(f"章节 {section.title} 内容安全限制 (第{attempt}次): {e}")
                    if attempt >= chapter_max_attempts:
                        raise

            if not success:
                raise ChapterJsonParseError(f"{section.title} 在 {chapter_max_attempts} 次尝试后仍失败")

            chapters.append(chapter_payload)
            pct = 20 + round(80 * (idx + 1) / total)
            self._emit(state, "progress", {"progress": pct, "message": f"章节 {idx+1}/{total} 已完成"})
            self._emit(state, "chapter_status", {"chapterId": section.chapter_id, "title": section.title, "status": "completed", "attempt": attempt, "warning": "content_sparse_fallback" if use_fallback else None})

        return {"chapters": chapters}

    @staticmethod
    def _init_graphrag_node(ctx):
        try:
            from ..nodes.graphrag_query_node import GraphRAGQueryNode
            return GraphRAGQueryNode(ctx.llm_client)
        except Exception:
            return None

    def _emit(self, state: ReportGraphState, evt: str, payload: dict):
        handler = self.ctx.stream_handler
        if handler:
            try:
                handler(evt, payload)
            except Exception:
                pass


def _finalize_sparse(chapter: dict) -> dict:
    safe = deepcopy(chapter) if isinstance(chapter, dict) else {}
    warning = {"type": "paragraph", "inlines": [{"text": "本章LLM生成的内容字数可能过低，必要时可以尝试重新运行程序。", "marks": [{"type": "italic"}]}], "meta": {"role": "content-sparse-warning"}}
    blocks = safe.get("blocks")
    if isinstance(blocks, list):
        for i, b in enumerate(blocks):
            if isinstance(b, dict) and b.get("type") == "heading":
                blocks.insert(i + 1, warning)
                break
        else:
            blocks.insert(0, warning)
    else:
        safe["blocks"] = [warning]
    safe.setdefault("meta", {})["contentSparseWarning"] = True
    return safe


def _is_content_safety_error(e: Exception) -> bool:
    msg = str(e) if e else ""
    return any(k in msg.lower() for k in ["inappropriate content", "content violation", "content moderation", "model-studio/error-code"])
