"""Shared search execution utility for QueryEngine nodes."""

from loguru import logger

from ..utils.source_classifier import (
    build_authority_queries,
    classify_source,
    source_rank,
)
from ..context import QueryContext
from app.services.search_enhancement import (
    expand_public_search_queries,
    needs_recent_window,
    rank_public_search_results,
)


def _extract_webpages(response) -> list:
    webpages = getattr(response, "webpages", None)
    if webpages is not None:
        return list(webpages)

    results = getattr(response, "results", None)
    if results is not None:
        return list(results)

    if isinstance(response, dict):
        return list(response.get("webpages") or response.get("results") or [])

    return []


def _read_field(item, *names: str, default=""):
    for name in names:
        if isinstance(item, dict) and name in item:
            return item.get(name) or default
        if hasattr(item, name):
            return getattr(item, name) or default
    return default


def execute_search_and_convert(ctx: QueryContext, search_output: dict, search_query: str, search_tool: str) -> list[dict]:
    mode = getattr(ctx.config, "SEARCH_ENHANCEMENT_MODE", "off")
    effective_tool = search_tool
    if needs_recent_window(search_query) and search_tool in {"comprehensive_search", "web_search_only"}:
        effective_tool = "search_last_week"

    kwargs = {}
    if effective_tool == "search_last_24_hours":
        kwargs["max_results"] = 10
    elif effective_tool == "search_last_week":
        kwargs["max_results"] = 10

    logger.info("  - 执行权威信息搜索...")
    expanded_queries = expand_public_search_queries(
        search_query,
        mode=mode,
        max_queries=1 if mode == "off" else 3,
    )
    queries: list[str] = []
    seen_query_keys: set[str] = set()
    for expanded in expanded_queries:
        for query in build_authority_queries(expanded, max_domains=1 if mode != "off" else 2):
            key = query.lower().strip()
            if key not in seen_query_keys:
                seen_query_keys.add(key)
                queries.append(query)
    seen_urls: set[str] = set()

    results: list[dict] = []
    first_error: Exception | None = None
    successful_calls = 0
    for query in queries:
        try:
            response = ctx.execute_search(effective_tool, query, **kwargs)
        except Exception as exc:
            if first_error is None:
                first_error = exc
            logger.exception(f"  - 搜索调用失败，跳过本轮搜索: {exc}")
            continue
        successful_calls += 1
        webpages = _extract_webpages(response)
        if not response or not webpages:
            continue

        for w in webpages:
            title = _read_field(w, "name", "title")
            url = _read_field(w, "url")
            snippet = _read_field(w, "snippet", "content", "raw_content")
            published_date = _read_field(w, "date_last_crawled", "published_date")
            score = _read_field(w, "score", default=None)
            dedupe_key = url or f"{title}:{snippet}"
            if dedupe_key in seen_urls:
                continue
            seen_urls.add(dedupe_key)

            rating = classify_source(url)
            credibility_boost = max(0, 4 - source_rank(rating.get("source_type", ""))) * 10
            try:
                adjusted_score = (float(score) if score is not None else 0.0) + credibility_boost
            except (TypeError, ValueError):
                adjusted_score = credibility_boost
            results.append({
                "title": title, "url": url, "content": snippet,
                "score": adjusted_score, "raw_content": snippet,
                "published_date": published_date,
                "search_query_used": query,
                **rating,
            })

    results = rank_public_search_results(results, search_query, mode=mode, max_results=15)

    if not results and first_error is not None and successful_calls == 0:
        raise first_error

    if results:
        msg = f"  - 找到 {len(results)} 个搜索结果"
        for r in results[:5]:
            msg += f"\n    [{r.get('source_label')}] {r['title'][:50]}..."
        logger.info(msg)
    else:
        logger.info("  - 未找到搜索结果")
    return results
