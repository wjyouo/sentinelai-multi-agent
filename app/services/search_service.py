"""
Search service — runs Insight/Media/Query engine agents in background threads.
Publishes progress/results via event_bus SSE.

InsightEngine uses the module-level run_research() directly (DeepSearchAgent
has been eliminated). MediaEngine and QueryEngine still use DeepSearchAgent.
"""

import threading
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger

from app.services.event_bus import publish
from app.services.forum_service import start_forum_engine
from engines.MediaEngine.agent import DeepSearchAgent as MediaSearchAgent
from engines.QueryEngine.agent import DeepSearchAgent as QuerySearchAgent

OUTPUT_DIRS = {
    'insight': 'insight_engine_streamlit_reports',
    'media': 'media_engine_streamlit_reports',
    'query': 'query_engine_streamlit_reports',
}
# ForumEngine LogMonitor 尾随的日志文件目录
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


def search_all(query: str):
    """Launch all 3 engine tasks in parallel background threads."""
    if not query.strip():
        return {"success": False, "message": "搜索查询不能为空"}

    # 启动 ForumEngine LogMonitor，开始监控引擎日志文件
    start_forum_engine()

    for engine_type in ['insight', 'media', 'query']:
        t = threading.Thread(
            target=run_engine_task,
            args=(engine_type, query),
            daemon=True,
        )
        t.start()

    return {"success": True, "message": "已启动所有引擎搜索", "query": query}


def run_engine_task(engine_type: str, query: str):
    """Run an engine agent in the current thread, publishing progress via SSE."""
    # 添加 loguru 文件 sink，供 ForumEngine LogMonitor 尾随
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = str(_LOG_DIR / f"{engine_type}.log")
    _sink_id = logger.add(
        _log_file,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} - {message}",
        level="INFO",
        encoding="utf-8",
        rotation="10 MB",
    )

    try:
        publish("engine_progress", {
            "engine": engine_type, "status": "starting",
            "message": "正在初始化引擎...", "progress_pct": 0,
        })

        if engine_type == 'insight':
            result = _run_insight_research(query)
            final_report = result.get("final_report", "")
            citations = _extract_citations_from_result(result)
        else:
            agent, _config = _create_agent(engine_type)
            agent.progress_callback = lambda data: publish(
                "engine_progress", {"engine": engine_type, **data},
            )
            publish("engine_progress", {
                "engine": engine_type, "status": "starting",
                "message": "Agent就绪，开始研究...", "progress_pct": 5,
            })
            final_report = agent.research(query)
            citations = _extract_citations(agent)

        publish("engine_progress", {
            "engine": engine_type, "status": "finalizing",
            "message": "研究完成", "progress_pct": 100,
        })
        publish("engine_result", {
            "engine": engine_type, "final_report": final_report,
            "citations": citations,
        })

    except Exception as exc:
        import traceback
        logger.exception(f"{engine_type} engine error: {exc}")
        publish("engine_error", {
            "engine": engine_type,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
    finally:
        try:
            logger.remove(_sink_id)
        except Exception:
            pass


def _run_insight_research(query: str) -> Dict[str, Any]:
    """Run InsightEngine research via run_research(), publishing progress via SSE."""
    from app.config import settings, Settings
    from engines.InsightEngine.agent import run_research
    from engines.InsightEngine.llms import LLMClient

    model = settings.INSIGHT_ENGINE_MODEL_NAME or "kimi-k2-0711-preview"
    config = Settings(
        INSIGHT_ENGINE_API_KEY=settings.INSIGHT_ENGINE_API_KEY,
        INSIGHT_ENGINE_BASE_URL=settings.INSIGHT_ENGINE_BASE_URL,
        INSIGHT_ENGINE_MODEL_NAME=model,
        DB_HOST=settings.DB_HOST, DB_USER=settings.DB_USER,
        DB_PASSWORD=settings.DB_PASSWORD, DB_NAME=settings.DB_NAME,
        DB_PORT=settings.DB_PORT, DB_CHARSET=settings.DB_CHARSET,
        DB_DIALECT=settings.DB_DIALECT,
        MAX_REFLECTIONS=2, MAX_CONTENT_LENGTH=500000,
        OUTPUT_DIR=OUTPUT_DIRS['insight'],
    )
    llm_client = LLMClient(
        api_key=config.INSIGHT_ENGINE_API_KEY,
        model_name=config.INSIGHT_ENGINE_MODEL_NAME,
        base_url=config.INSIGHT_ENGINE_BASE_URL,
    )

    def progress_callback(data):
        publish("engine_progress", {"engine": "insight", **data})

    return run_research(query, config, llm_client, progress_callback)


def _create_agent(engine_type: str):
    """Instantiate the correct agent class and config for media or query engine."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent.parent
    for _p in [str(_root / "engines"), str(_root / "app" / "utils")]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from app.config import settings, Settings

    if engine_type == 'media':
        from MediaEngine import DeepSearchAgent, TavilySearchAgent, AnspireSearchAgent
        model = settings.MEDIA_ENGINE_MODEL_NAME or "gemini-2.5-pro"
        search_type = settings.SEARCH_TOOL_TYPE or "TavilyAPI"

        config = Settings(
            MEDIA_ENGINE_API_KEY=settings.MEDIA_ENGINE_API_KEY,
            MEDIA_ENGINE_BASE_URL=settings.MEDIA_ENGINE_BASE_URL,
            MEDIA_ENGINE_MODEL_NAME=model,
            SEARCH_TOOL_TYPE=search_type,
            TAVILY_API_KEY=settings.TAVILY_API_KEY,
            BOCHA_WEB_SEARCH_API_KEY=settings.BOCHA_WEB_SEARCH_API_KEY,
            ANSPIRE_API_KEY=settings.ANSPIRE_API_KEY,
            MAX_REFLECTIONS=2, SEARCH_CONTENT_MAX_LENGTH=20000,
            OUTPUT_DIR=OUTPUT_DIRS['media'],
        )

        if search_type == "TavilyAPI":
            agent = TavilySearchAgent(config)
        elif search_type == "BochaAPI":
            agent = DeepSearchAgent(config)
        elif search_type == "AnspireAPI":
            agent = AnspireSearchAgent(config)
        else:
            agent = DeepSearchAgent(config)
        return agent, config

    if engine_type == 'query':
        from QueryEngine import DeepSearchAgent
        model = settings.QUERY_ENGINE_MODEL_NAME or "deepseek-chat"
        config = Settings(
            QUERY_ENGINE_API_KEY=settings.QUERY_ENGINE_API_KEY,
            QUERY_ENGINE_BASE_URL=settings.QUERY_ENGINE_BASE_URL,
            QUERY_ENGINE_MODEL_NAME=model,
            TAVILY_API_KEY=settings.TAVILY_API_KEY,
            MAX_REFLECTIONS=2, SEARCH_CONTENT_MAX_LENGTH=20000,
            OUTPUT_DIR=OUTPUT_DIRS['query'],
        )
        return DeepSearchAgent(config), config

    raise ValueError(f"Unknown engine type: {engine_type}")


def _extract_citations(agent) -> List[Dict[str, Any]]:
    """Extract search history from agent state (media/query engines)."""
    citations: List[Dict[str, Any]] = []
    for p_idx, paragraph in enumerate(agent.state.paragraphs):
        for search in paragraph.research.search_history:
            citations.append({
                "paragraph_index": p_idx,
                "paragraph_title": paragraph.title,
                "query": getattr(search, 'query', ''),
                "url": getattr(search, 'url', None),
                "title": getattr(search, 'title', None),
                "content": (getattr(search, 'content', '') or '')[:500],
                "score": getattr(search, 'score', None),
                "search_count": paragraph.research.get_search_count(),
                "reflection_count": paragraph.research.reflection_iteration,
            })
    return citations


def _extract_citations_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract search history from run_research() result dict (insight engine)."""
    from engines.InsightEngine.models import Paragraph

    citations: List[Dict[str, Any]] = []
    paragraphs = result.get("paragraphs", [])
    for p_idx, p_dict in enumerate(paragraphs):
        paragraph = Paragraph.from_dict(p_dict) if isinstance(p_dict, dict) else p_dict
        for search in paragraph.research.search_history:
            citations.append({
                "paragraph_index": p_idx,
                "paragraph_title": paragraph.title,
                "query": getattr(search, 'query', ''),
                "url": getattr(search, 'url', None),
                "title": getattr(search, 'title', None),
                "content": (getattr(search, 'content', '') or '')[:500],
                "score": getattr(search, 'score', None),
                "search_count": paragraph.research.get_search_count(),
                "reflection_count": paragraph.research.reflection_iteration,
            })
    return citations
