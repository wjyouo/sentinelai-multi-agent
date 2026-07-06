"""Shared public-search enhancement helpers.

These helpers are intentionally deterministic. They improve public-search
precision without adding a new TrendScope LLM dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import urlparse


SearchEnhancementMode = str

VALID_SEARCH_ENHANCEMENT_MODES = {"off", "light", "full"}

RECENT_WORDS = {
    "最近", "最新", "刚刚", "今天", "昨日", "昨天", "近况", "进展",
    "回应", "通报", "澄清", "声明", "后续",
}
EVENT_WORDS = {
    "事件", "塌房", "争议", "风波", "翻车", "爆料", "时间线",
    "发生了什么", "怎么回事", "始末", "舆情", "热搜",
}
NOISE_WORDS = RECENT_WORDS | EVENT_WORDS | {
    "分析", "介绍", "相关", "关于", "一下", "请问", "什么", "为何",
    "为什么", "如何", "怎样", "帮我", "看看",
}
PLATFORM_HINTS = ("微博", "抖音", "B站", "小红书")
PLATFORM_QUERY_HINT = "微博 抖音 B站 小红书"
OLD_OR_BACKGROUND_WORDS = {
    "百科", "资料", "简介", "个人资料", "历史", "过往", "旧闻", "回顾",
}
CATEGORY_HINTS = {
    "person_or_influencer": {"网红", "主播", "博主", "明星", "演员", "歌手"},
    "brand": {"品牌", "公司", "企业", "产品", "门店"},
    "place": {"城市", "景区", "学校", "医院", "地点", "旅游"},
    "event": EVENT_WORDS,
}


@dataclass(frozen=True)
class EnhancedSearchPlan:
    original_query: str
    mode: SearchEnhancementMode
    subject: str
    category: str
    time_window_days: int
    queries: list[str]
    summary: str


def coerce_search_enhancement_mode(value: Any) -> SearchEnhancementMode:
    mode = str(value or "off").strip().lower()
    return mode if mode in VALID_SEARCH_ENHANCEMENT_MODES else "off"


def needs_recent_window(query: str) -> bool:
    return _contains_any(query, RECENT_WORDS) or _contains_any(query, EVENT_WORDS)


def extract_search_subject(query: str) -> str:
    text = str(query or "").strip()
    text = re.sub(r"[\s\u3000]+", " ", text)
    text = re.sub(r"[，。！？、；：,.!?;:\"'“”‘’（）()\[\]{}<>《》]", " ", text)
    for word in sorted(NOISE_WORDS, key=len, reverse=True):
        text = text.replace(word, " ")
    text = re.sub(r"\s+", " ", text).strip()
    return (text or str(query or "").strip())[:40]


def classify_query(query: str, subject: str | None = None) -> str:
    text = f"{query} {subject or ''}"
    for category, hints in CATEGORY_HINTS.items():
        if _contains_any(text, hints):
            return category
    if len(subject or "") <= 8 and _contains_any(query, EVENT_WORDS | RECENT_WORDS):
        return "person_or_influencer"
    return "general"


def build_enhanced_search_plan(
    query: str,
    mode: SearchEnhancementMode = "off",
    optimized_queries: Iterable[str] | None = None,
    max_queries: int = 6,
) -> EnhancedSearchPlan:
    mode = coerce_search_enhancement_mode(mode)
    original = str(query or "").strip()
    subject = extract_search_subject(original)
    category = classify_query(original, subject)
    time_window_days = 7 if needs_recent_window(original) else 30

    queries: list[str] = [original] if original else []
    if mode in {"light", "full"} and subject:
        if needs_recent_window(original):
            queries.extend([
                f'"{subject}" 事件 回应 最近{time_window_days}天',
                f'"{subject}" 时间线 官方回应 网友评论',
            ])
        else:
            queries.append(f'"{subject}" 最新 相关信息')
        queries.append(f'"{subject}" {PLATFORM_QUERY_HINT} 热点')

    if mode == "full":
        for item in optimized_queries or []:
            if str(item).strip():
                queries.append(str(item).strip())

    queries = _dedupe(queries)[:max_queries]
    summary = (
        f"模式={mode}；主体={subject or '未识别'}；分类={category}；"
        f"时间窗口=最近{time_window_days}天；查询数={len(queries)}"
    )
    return EnhancedSearchPlan(
        original_query=original,
        mode=mode,
        subject=subject,
        category=category,
        time_window_days=time_window_days,
        queries=queries,
        summary=summary,
    )


def expand_public_search_queries(
    query: str,
    mode: SearchEnhancementMode = "off",
    max_queries: int = 3,
) -> list[str]:
    plan = build_enhanced_search_plan(query, mode=mode, max_queries=max_queries)
    return plan.queries or [query]


def normalize_result_key(title: str = "", url: str = "", content: str = "") -> str:
    host = (urlparse(url or "").hostname or "").lower().lstrip("www.")
    path = (urlparse(url or "").path or "").rstrip("/")
    if host or path:
        return f"{host}{path}".lower()
    text = re.sub(r"\s+", "", f"{title}:{content[:80]}").lower()
    return text


def rank_public_search_results(
    results: Iterable[dict[str, Any]],
    original_query: str,
    mode: SearchEnhancementMode = "off",
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    plan = build_enhanced_search_plan(original_query, mode=mode, max_queries=1)
    now = datetime.now()
    seen: set[str] = set()
    ranked: list[tuple[float, int, dict[str, Any]]] = []

    for index, item in enumerate(results):
        title = str(item.get("title") or item.get("name") or "")
        url = str(item.get("url") or "")
        content = str(item.get("content") or item.get("snippet") or item.get("raw_content") or "")
        key = normalize_result_key(title, url, content)
        if not key or key in seen:
            continue
        if any(SequenceMatcher(None, key, other).ratio() > 0.96 for other in seen):
            continue
        seen.add(key)

        score = _base_score(item)
        score += _subject_score(plan.subject, title, content)
        score += _freshness_score(item.get("published_date") or item.get("date_last_crawled"), plan.time_window_days, now)
        score += _platform_score(url, title, content, mode)
        if _contains_any(f"{title} {content}", OLD_OR_BACKGROUND_WORDS) and needs_recent_window(original_query):
            score -= 18
        if item.get("search_query_used") == original_query:
            score += 5

        enriched = dict(item)
        enriched["enhancement_score"] = round(score, 3)
        enriched["enhancement_subject"] = plan.subject
        ranked.append((score, -index, enriched))

    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    output = [item for _, _, item in ranked]
    return output[:max_results] if max_results else output


def _base_score(item: dict[str, Any]) -> float:
    raw_score = item.get("score")
    try:
        return float(raw_score or 0)
    except (TypeError, ValueError):
        return 0.0


def _subject_score(subject: str, title: str, content: str) -> float:
    if not subject:
        return 0.0
    text = f"{title} {content}"
    if subject in text:
        return 35.0
    tokens = [t for t in re.split(r"\s+", subject) if t]
    if tokens and all(token in text for token in tokens):
        return 22.0
    return -28.0


def _freshness_score(value: Any, window_days: int, now: datetime) -> float:
    parsed = _parse_date(value)
    if not parsed:
        return 0.0
    if parsed >= now - timedelta(days=window_days):
        return 20.0
    if parsed >= now - timedelta(days=window_days * 3):
        return 6.0
    return -16.0


def _platform_score(url: str, title: str, content: str, mode: str) -> float:
    text = f"{url} {title} {content}".lower()
    platform_tokens = ("weibo", "douyin", "bilibili", "b23.tv", "xiaohongshu", "xhs", "kuaishou")
    if any(token in text for token in platform_tokens) or any(label in title + content for label in PLATFORM_HINTS):
        return 8.0 if mode in {"light", "full"} else 4.0
    return 0.0


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(word in text for word in words)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        key = re.sub(r"\s+", "", str(value).lower())
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(str(value))
    return output
