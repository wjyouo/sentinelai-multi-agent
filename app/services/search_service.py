"""
Search service — runs Insight/Media/Query engine agents in background threads.
Publishes progress/results via event_bus SSE.

All engines use module-level run_research() directly.
"""

import threading
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger
from app import config as config_module
from app.services.event_bus import publish
from app.services.event_types import EventType
from app.services.trendscope_service import TrendScopeOptions, TrendScopeOrchestrator

OUTPUT_DIRS = {
    'trendscope': 'data/report/trendscope',
    'insight': 'data/report/insight',
    'media': 'data/report/media',
    'query': 'data/report/query',
}
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


class _EmptySearchResponse:
    def __init__(self, query: str):
        self.query = query
        self.conversation_id = None
        self.answer = ""
        self.follow_ups = []
        self.webpages = []
        self.images = []
        self.modal_cards = []


class _NoopSearchAgency:
    def __init__(self, reason: str):
        self.reason = reason

    def _empty(self, query: str, *args, **kwargs) -> _EmptySearchResponse:
        logger.warning(f"搜索服务未配置，跳过外部搜索: {self.reason}")
        return _EmptySearchResponse(query)

    def comprehensive_search(self, query: str, *args, **kwargs) -> _EmptySearchResponse:
        return self._empty(query, *args, **kwargs)

    def web_search_only(self, query: str, *args, **kwargs) -> _EmptySearchResponse:
        return self._empty(query, *args, **kwargs)

    def search_for_structured_data(self, query: str, *args, **kwargs) -> _EmptySearchResponse:
        return self._empty(query, *args, **kwargs)

    def search_last_24_hours(self, query: str, *args, **kwargs) -> _EmptySearchResponse:
        return self._empty(query, *args, **kwargs)

    def search_last_week(self, query: str, *args, **kwargs) -> _EmptySearchResponse:
        return self._empty(query, *args, **kwargs)


def _configured(value: Any) -> bool:
    return bool(str(value or "").strip())


def search_all(query: str, options: Dict[str, Any] | None = None):
    """Launch TrendScope plus automatically selected engine tasks."""
    if not query.strip():
        return {"success": False, "message": "搜索查询不能为空"}

    trend_options = TrendScopeOptions.from_payload(options)
    intent = TrendScopeOrchestrator().analyze_intent(query, trend_options)
    selected_agents = intent.selected_agents

    for engine_type in selected_agents:
        t = threading.Thread(
            target=run_engine_task,
            args=(engine_type, query, trend_options.model_dump()),
            daemon=True,
        )
        t.start()

    return {
        "success": True,
        "message": "auto-selected agents started",
        "query": query,
        "intent": intent.model_dump(),
        "options": trend_options.model_dump(),
        "selected_agents": selected_agents,
    }

    return {"success": True, "message": "已启动所有引擎搜索", "query": query}


def get_latest_results() -> Dict[str, Any]:
    """Load latest persisted engine reports so the UI can recover after refresh."""
    results: Dict[str, Any] = {}
    for engine_type, output_dir in OUTPUT_DIRS.items():
        engine_dir = Path(output_dir)
        if not engine_dir.is_dir():
            continue

        md_files = sorted(engine_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not md_files:
            continue

        latest_md = md_files[0]
        final_report = latest_md.read_text(encoding="utf-8", errors="ignore")
        state_file = _find_matching_state_file(engine_dir, latest_md)
        citations: List[Dict[str, Any]] = []
        if state_file:
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                citations = _extract_citations_from_result(state)
            except Exception:
                logger.exception(f"读取 {engine_type} 最新状态文件失败: {state_file}")

        results[engine_type] = {
            "engine": engine_type,
            "status": "done",
            "final_report": final_report,
            "citations": citations,
            "report_file": str(latest_md),
            "state_file": str(state_file) if state_file else "",
            "updated_at": latest_md.stat().st_mtime,
        }

    return {"success": True, "results": results}


def _find_matching_state_file(engine_dir: Path, report_file: Path) -> Path | None:
    """Find the state JSON saved alongside a report markdown file."""
    report_name = report_file.stem
    suffix = ""
    if "_" in report_name:
        suffix = report_name.split("_", 3)[-1]

    state_files = sorted(engine_dir.glob("state_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if suffix:
        for path in state_files:
            if suffix in path.stem:
                return path
    return state_files[0] if state_files else None


def run_engine_task(engine_type: str, query: str, options: Dict[str, Any] | None = None):
    """Run an engine agent in the current thread, publishing progress via SSE."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = str(_LOG_DIR / f"{engine_type}.log")
    # 按照模块名称，对不同的engine的日志，进行分流
    logger.add(
        _log_file,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} - {message}",
        level=config_module.settings.LOG_LEVEL, encoding="utf-8", rotation="10 MB",
        # record["name"] 是模块的 __name__，例如engines.InsightEngine.agent
        filter=lambda record: engine_type.lower() in record["name"].lower() or "common" in record["name"].lower(),
    )

    try:
        publish(EventType.ENGINE_PROGRESS, {
            "engine": engine_type, "status": "starting",
            "message": "正在初始化引擎...", "progress_pct": 0,
        })

        if engine_type == 'trendscope':
            result = _run_trendscope_research(query, options)
        elif engine_type == 'insight':
            result = _run_insight_research(query)
        elif engine_type == 'media':
            result = _run_media_research(query, options)
        elif engine_type == 'query':
            result = _run_query_research(query, options)
        else:
            raise ValueError(f"Unknown engine type: {engine_type}")

        final_report = result.get("final_report", "")
        citations = _extract_citations_from_result(result)

        publish(EventType.ENGINE_PROGRESS, {
            "engine": engine_type, "status": "finalizing",
            "message": "研究完成", "progress_pct": 100,
        })
        publish(EventType.ENGINE_RESULT, {
            "engine": engine_type, "final_report": final_report,
            "citations": citations,
        })

    except Exception as exc:
        import traceback
        logger.exception(f"{engine_type} engine error: {exc}")
        publish(EventType.ENGINE_ERROR, {
            "engine": engine_type, "error": str(exc),
            "traceback": traceback.format_exc(),
        })



def _run_insight_research(query: str) -> Dict[str, Any]:
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

    # 这里也是一种抽象，InsightEngine当中所有的节点的事件，event_type全部都是engine_progress，
    def progress_callback(data):
        "回调函数，用以通过SSE机制，在前端展示进度"
        publish(EventType.ENGINE_PROGRESS, {"engine": "insight", **data})

    return run_research(query, config, llm_client, progress_callback)


def _run_trendscope_research(query: str, options: Dict[str, Any] | None = None) -> Dict[str, Any]:
    trend_options = TrendScopeOptions.from_payload(options)
    search_agency = _build_public_search_agency()

    def progress_callback(data):
        publish(EventType.ENGINE_PROGRESS, {"engine": "trendscope", **data})

    return TrendScopeOrchestrator().run(
        query,
        options=trend_options,
        search_agency=search_agency,
        progress_callback=progress_callback,
        save_report=True,
    )


def _build_public_search_agency() -> Any:
    from engines.MediaEngine.tools import (
        BochaMultimodalSearch, AnspireAISearch, TavilySearchWrapper,
    )

    settings = config_module.settings
    search_type = settings.SEARCH_TOOL_TYPE or "TavilyAPI"
    if search_type == "TavilyAPI" and not _configured(settings.TAVILY_API_KEY):
        return _NoopSearchAgency("TAVILY_API_KEY not configured")
    if search_type == "AnspireAPI" and not _configured(settings.ANSPIRE_API_KEY):
        return _NoopSearchAgency("ANSPIRE_API_KEY not configured")
    if search_type == "BochaAPI" and not _configured(settings.BOCHA_WEB_SEARCH_API_KEY):
        return _NoopSearchAgency("BOCHA_WEB_SEARCH_API_KEY not configured")
    if search_type == "TavilyAPI":
        return TavilySearchWrapper(api_key=settings.TAVILY_API_KEY)
    if search_type == "AnspireAPI":
        return AnspireAISearch(api_key=settings.ANSPIRE_API_KEY)
    return BochaMultimodalSearch(api_key=settings.BOCHA_WEB_SEARCH_API_KEY)


def _run_media_research(query: str, options: Dict[str, Any] | None = None) -> Dict[str, Any]:
    from app.config import settings, Settings
    from engines.MediaEngine.agent import run_research
    from engines.MediaEngine.llms import LLMClient
    from engines.MediaEngine.tools import (
        BochaMultimodalSearch, AnspireAISearch, TavilySearchWrapper,
    )

    model = settings.MEDIA_ENGINE_MODEL_NAME or "gemini-2.5-pro"
    search_type = settings.SEARCH_TOOL_TYPE or "TavilyAPI"
    trend_options = TrendScopeOptions.from_payload(options)
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
    config.SEARCH_ENHANCEMENT_MODE = trend_options.search_enhancement_mode
    llm_client = LLMClient(
        api_key=config.MEDIA_ENGINE_API_KEY,
        model_name=config.MEDIA_ENGINE_MODEL_NAME,
        base_url=config.MEDIA_ENGINE_BASE_URL,
    )

    if search_type == "TavilyAPI" and not _configured(config.TAVILY_API_KEY):
        search_agency = _NoopSearchAgency("TAVILY_API_KEY 未配置")
    elif search_type == "AnspireAPI" and not _configured(config.ANSPIRE_API_KEY):
        search_agency = _NoopSearchAgency("ANSPIRE_API_KEY 未配置")
    elif search_type == "BochaAPI" and not _configured(config.BOCHA_WEB_SEARCH_API_KEY):
        search_agency = _NoopSearchAgency("BOCHA_WEB_SEARCH_API_KEY 未配置")
    elif search_type == "TavilyAPI":
        search_agency = TavilySearchWrapper(api_key=config.TAVILY_API_KEY)
    elif search_type == "AnspireAPI":
        search_agency = AnspireAISearch(api_key=config.ANSPIRE_API_KEY)
    else:
        search_agency = BochaMultimodalSearch(api_key=config.BOCHA_WEB_SEARCH_API_KEY)

    def progress_callback(data):
        publish(EventType.ENGINE_PROGRESS, {"engine": "media", **data})

    return run_research(query, config, llm_client, search_agency, progress_callback)


def _run_query_research(query: str, options: Dict[str, Any] | None = None) -> Dict[str, Any]:
    from app.config import settings, Settings
    from engines.QueryEngine.agent import run_research
    from engines.QueryEngine.llms import LLMClient
    from engines.MediaEngine.tools.search import TavilySearchWrapper

    model = settings.QUERY_ENGINE_MODEL_NAME or "deepseek-chat"
    trend_options = TrendScopeOptions.from_payload(options)
    config = Settings(
        QUERY_ENGINE_API_KEY=settings.QUERY_ENGINE_API_KEY,
        QUERY_ENGINE_BASE_URL=settings.QUERY_ENGINE_BASE_URL,
        QUERY_ENGINE_MODEL_NAME=model,
        TAVILY_API_KEY=settings.TAVILY_API_KEY,
        MAX_REFLECTIONS=2, SEARCH_CONTENT_MAX_LENGTH=20000,
        OUTPUT_DIR=OUTPUT_DIRS['query'],
    )
    config.SEARCH_ENHANCEMENT_MODE = trend_options.search_enhancement_mode
    llm_client = LLMClient(
        api_key=config.QUERY_ENGINE_API_KEY,
        model_name=config.QUERY_ENGINE_MODEL_NAME,
        base_url=config.QUERY_ENGINE_BASE_URL,
    )
    if _configured(config.TAVILY_API_KEY):
        search_agency = TavilySearchWrapper(api_key=config.TAVILY_API_KEY)
    else:
        search_agency = _NoopSearchAgency("TAVILY_API_KEY 未配置")

    def progress_callback(data):
        publish(EventType.ENGINE_PROGRESS, {"engine": "query", **data})

    return run_research(query, config, llm_client, search_agency, progress_callback)


def _extract_citations_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract search history from run_research() result dict."""
    citations: List[Dict[str, Any]] = []
    paragraphs = result.get("paragraphs", [])
    for p_idx, p_dict in enumerate(paragraphs):
        research = p_dict.get("research", {}) if isinstance(p_dict, dict) else {}
        search_history = research.get("search_history", [])
        for search in search_history:
            citations.append({
                "paragraph_index": p_idx,
                "paragraph_title": p_dict.get("title", "") if isinstance(p_dict, dict) else "",
                "query": _json_safe_value(search.get("query", "")),
                "url": _json_safe_value(search.get("url")),
                "title": _json_safe_value(search.get("title")),
                "content": _json_safe_value((search.get("content", "") or "")[:500]),
                "score": _json_safe_value(search.get("score")),
                "source_type": _json_safe_value(search.get("source_type", "")),
                "credibility": _json_safe_value(search.get("credibility", "")),
                "source_label": _json_safe_value(search.get("source_label", "")),
                "source_domain": _json_safe_value(search.get("source_domain", "")),
                "published_date": _json_safe_value(search.get("published_date", "")),
                "credibility_reason": _json_safe_value(search.get("credibility_reason", "")),
                "search_count": len(search_history),
                "reflection_count": _json_safe_value(research.get("reflection_iteration", 0)),
            })
    return citations


def _json_safe_value(value: Any) -> Any:
    """Normalize engine citation values so SSE json.dumps never drops engine_result."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
