"""
Shared search execution utility for MediaEngine nodes.
"""

from loguru import logger
from ..context import MediaContext
from app.services.search_enhancement import (
    expand_public_search_queries,
    needs_recent_window,
    rank_public_search_results,
)

def execute_search_and_convert(ctx: MediaContext, search_output: dict, search_query: str, search_tool: str) -> list[dict]:
    """Execute search and convert results to standard dict list."""
    mode = getattr(ctx.config, "SEARCH_ENHANCEMENT_MODE", "off")
    effective_tool = search_tool
    if needs_recent_window(search_query) and search_tool in {"comprehensive_search", "web_search_only"}:
        effective_tool = "search_last_week"

    kwargs = {}
    if effective_tool in ("comprehensive_search", "web_search_only"):
        kwargs["max_results"] = 10

    logger.info("  - 执行网络搜索...")
    first_error: Exception | None = None
    results: list[dict] = []
    queries = expand_public_search_queries(
        search_query,
        mode=mode,
        max_queries=1 if mode == "off" else 3,
    )
    for query in queries:
        try:
            response = ctx.execute_search(effective_tool, query, **kwargs)
        except Exception as exc:
            if first_error is None:
                first_error = exc
            logger.exception(f"  - 搜索调用失败，跳过本轮搜索: {exc}")
            continue

        if response and getattr(response, "webpages", None):
            limit = min(len(response.webpages), 10)
            for r in response.webpages[:limit]:
                results.append({
                    "title": r.name, "url": r.url,
                    "content": r.snippet, "score": None,
                    "raw_content": r.snippet,
                    "published_date": r.date_last_crawled,
                    "search_query_used": query,
                })

    results = rank_public_search_results(results, search_query, mode=mode, max_results=10)

    if results:
        msg = f"  - 找到 {len(results)} 个搜索结果"
        for r in results[:5]:
            msg += f"\n    {r['title'][:50]}..."
        logger.info(msg)
    else:
        logger.info("  - 未找到搜索结果")

    if not results and first_error is not None:
        raise first_error

    return results
