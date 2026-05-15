"""Shared search execution utility for QueryEngine nodes.

QueryEngine uses the same Bocha search backend as MediaEngine,
but LLM prompts drive it toward authoritative/official source verification.
"""

from loguru import logger


def execute_search_and_convert(ctx, search_output: dict, search_query: str, search_tool: str) -> list[dict]:
    kwargs = {}
    if search_tool == "search_last_24_hours":
        kwargs["max_results"] = 10
    elif search_tool == "search_last_week":
        kwargs["max_results"] = 10

    logger.info("  - 执行权威信息搜索...")
    response = ctx.execute_search(search_tool, search_query, **kwargs)

    results: list[dict] = []
    if response and response.webpages:
        limit = min(len(response.webpages), 15)
        for w in response.webpages[:limit]:
            results.append({
                "title": w.name, "url": w.url, "content": w.snippet,
                "score": None, "raw_content": w.snippet,
                "published_date": w.date_last_crawled,
            })

    if results:
        msg = f"  - 找到 {len(results)} 个搜索结果"
        for r in results[:5]:
            msg += f"\n    {r['title'][:50]}..."
        logger.info(msg)
    else:
        logger.info("  - 未找到搜索结果")
    return results
