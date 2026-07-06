"""TrendScope v1 orchestration and lightweight result-memory RAG.

The service stays deliberately small:
- deterministic intent detection with optional keyword optimizer support
- public-search aggregation through the existing search agency interface
- SQLite result memory in the project data directory
- Markdown output shaped for the TrendScope overview panel
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

try:
    from loguru import logger
except Exception:  # pragma: no cover - fallback for minimal test envs
    import logging
    logger = logging.getLogger(__name__)

try:
    from app.config import PROJECT_ROOT, settings
except Exception:  # pragma: no cover - fallback for minimal test envs
    PROJECT_ROOT = Path(__file__).resolve().parents[2]

    class _SettingsStub:
        KEYWORD_OPTIMIZER_API_KEY = None
        KEYWORD_OPTIMIZER_BASE_URL = None
        KEYWORD_OPTIMIZER_MODEL_NAME = None

    settings = _SettingsStub()

from app.services.search_enhancement import (
    build_enhanced_search_plan,
    coerce_search_enhancement_mode,
    normalize_result_key,
    rank_public_search_results,
)

TREND_DATA_DIR = PROJECT_ROOT / "data" / "trendscope"
TREND_REPORT_DIR = PROJECT_ROOT / "data" / "report" / "trendscope"
TREND_DB_PATH = TREND_DATA_DIR / "trendscope_memory.sqlite3"


@dataclass
class TrendScopeOptions:
    enable_network_search: bool = True
    enable_video_hotspots: bool = True
    enable_local_knowledge: bool = True
    enable_risk_analysis: bool = False
    enable_deep_report: bool = False
    search_enhancement_mode: str = "off"

    def __post_init__(self) -> None:
        self.search_enhancement_mode = coerce_search_enhancement_mode(self.search_enhancement_mode)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "TrendScopeOptions":
        payload = payload or {}
        return cls(
            enable_network_search=_bool(payload.get("enable_network_search"), True),
            enable_video_hotspots=_bool(payload.get("enable_video_hotspots"), True),
            enable_local_knowledge=_bool(payload.get("enable_local_knowledge"), True),
            enable_risk_analysis=_bool(payload.get("enable_risk_analysis"), False),
            enable_deep_report=_bool(payload.get("enable_deep_report"), False),
            search_enhancement_mode=coerce_search_enhancement_mode(payload.get("search_enhancement_mode")),
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "enable_network_search": self.enable_network_search,
            "enable_video_hotspots": self.enable_video_hotspots,
            "enable_local_knowledge": self.enable_local_knowledge,
            "enable_risk_analysis": self.enable_risk_analysis,
            "enable_deep_report": self.enable_deep_report,
            "search_enhancement_mode": self.search_enhancement_mode,
        }


@dataclass
class TrendIntent:
    original_query: str
    normalized_query: str
    topic: str
    intent: str
    entities: list[str]
    time_window_days: int
    preferred_sources: list[str]
    excluded_directions: list[str]
    rewritten_queries: list[str]
    selected_agents: list[str]
    requires_timeline: bool
    search_enhancement_mode: str
    search_enhancement_summary: str

    def model_dump(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "normalized_query": self.normalized_query,
            "topic": self.topic,
            "intent": self.intent,
            "entities": self.entities,
            "time_window_days": self.time_window_days,
            "preferred_sources": self.preferred_sources,
            "excluded_directions": self.excluded_directions,
            "rewritten_queries": self.rewritten_queries,
            "selected_agents": self.selected_agents,
            "requires_timeline": self.requires_timeline,
            "search_enhancement_mode": self.search_enhancement_mode,
            "search_enhancement_summary": self.search_enhancement_summary,
        }


@dataclass
class TrendSource:
    title: str
    url: str
    content: str
    published_date: str = ""
    source_type: str = "media_or_unknown"
    credibility: str = "medium"
    source_label: str = "普通媒体或未知来源"
    source_domain: str = ""
    credibility_score: int = 45
    credibility_reason: str = ""
    query: str = ""

    def model_dump(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TimelineItem:
    date: str
    event: str
    source_title: str = ""
    source_url: str = ""

    def model_dump(self) -> dict[str, str]:
        return self.__dict__.copy()


@dataclass
class MemoryEntry:
    normalized_query: str
    intent: dict[str, Any]
    sources: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    summary: str
    final_report: str
    created_at: str
    fresh_until: str
    source_hash: str
    similarity: float = 1.0


@dataclass
class FreshnessCheck:
    reused_cache: bool = False
    cache_hit: bool = False
    refreshed: bool = False
    message: str = ""
    new_source_hash: str = ""
    current_sources: list[TrendSource] = field(default_factory=list)


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def normalize_query(query: str) -> str:
    text = (query or "").strip().lower()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[，。！？、,.!?;；:：\"'“”‘’（）()\[\]{}<>《》]", "", text)
    return text


class TrendMemoryStore:
    """SQLite result-memory store for TrendScope."""

    def __init__(self, db_path: Path = TREND_DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trend_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    normalized_query TEXT NOT NULL UNIQUE,
                    original_query TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    rewritten_queries_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    timeline_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    final_report TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    fresh_until TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trend_memory_fresh_until "
                "ON trend_memory(fresh_until)"
            )

    def save(self, intent: TrendIntent, sources: list[TrendSource], timeline: list[TimelineItem],
             final_report: str, source_hash: str, options: TrendScopeOptions) -> None:
        now = datetime.now()
        fresh_until = now + timedelta(hours=6 if intent.requires_timeline else 24)
        summary = _build_summary(intent, sources)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trend_memory (
                    normalized_query, original_query, intent_json, entities_json,
                    rewritten_queries_json, sources_json, timeline_json, summary,
                    final_report, created_at, fresh_until, source_hash, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_query) DO UPDATE SET
                    original_query=excluded.original_query,
                    intent_json=excluded.intent_json,
                    entities_json=excluded.entities_json,
                    rewritten_queries_json=excluded.rewritten_queries_json,
                    sources_json=excluded.sources_json,
                    timeline_json=excluded.timeline_json,
                    summary=excluded.summary,
                    final_report=excluded.final_report,
                    created_at=excluded.created_at,
                    fresh_until=excluded.fresh_until,
                    source_hash=excluded.source_hash,
                    metadata_json=excluded.metadata_json
                """,
                (
                    intent.normalized_query,
                    intent.original_query,
                    json.dumps(intent.model_dump(), ensure_ascii=False),
                    json.dumps(intent.entities, ensure_ascii=False),
                    json.dumps(intent.rewritten_queries, ensure_ascii=False),
                    json.dumps([s.model_dump() for s in sources], ensure_ascii=False),
                    json.dumps([t.model_dump() for t in timeline], ensure_ascii=False),
                    summary,
                    final_report,
                    now.isoformat(timespec="seconds"),
                    fresh_until.isoformat(timespec="seconds"),
                    source_hash,
                    json.dumps({"options": options.model_dump()}, ensure_ascii=False),
                ),
            )

    def find(self, query: str, threshold: float = 0.72) -> MemoryEntry | None:
        normalized = normalize_query(query)
        with self._connect() as conn:
            exact = conn.execute(
                "SELECT * FROM trend_memory WHERE normalized_query = ?",
                (normalized,),
            ).fetchone()
            if exact:
                return self._row_to_entry(exact, similarity=1.0)

            rows = conn.execute("SELECT * FROM trend_memory").fetchall()

        if not rows:
            return None

        candidates = [str(row["normalized_query"]) for row in rows]
        best_idx, best_score = self._best_similarity(normalized, candidates)
        if best_idx is None or best_score < threshold:
            return None
        return self._row_to_entry(rows[best_idx], similarity=best_score)

    @staticmethod
    def _best_similarity(query: str, candidates: list[str]) -> tuple[int | None, float]:
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity

            vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 4))
            matrix = vectorizer.fit_transform([query] + candidates)
            scores = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
            if len(scores) == 0:
                return None, 0.0
            best_idx = int(scores.argmax())
            return best_idx, float(scores[best_idx])
        except Exception:
            scores = [SequenceMatcher(None, query, candidate).ratio() for candidate in candidates]
            if not scores:
                return None, 0.0
            best_idx = max(range(len(scores)), key=scores.__getitem__)
            return best_idx, float(scores[best_idx])

    @staticmethod
    def _row_to_entry(row: sqlite3.Row, similarity: float) -> MemoryEntry:
        return MemoryEntry(
            normalized_query=str(row["normalized_query"]),
            intent=json.loads(row["intent_json"] or "{}"),
            sources=json.loads(row["sources_json"] or "[]"),
            timeline=json.loads(row["timeline_json"] or "[]"),
            summary=str(row["summary"] or ""),
            final_report=str(row["final_report"] or ""),
            created_at=str(row["created_at"] or ""),
            fresh_until=str(row["fresh_until"] or ""),
            source_hash=str(row["source_hash"] or ""),
            similarity=similarity,
        )


class TrendScopeOrchestrator:
    def __init__(self, memory_store: TrendMemoryStore | None = None):
        self.memory_store = memory_store or TrendMemoryStore()

    def analyze_intent(self, query: str, options: TrendScopeOptions | None = None) -> TrendIntent:
        options = options or TrendScopeOptions()
        normalized = normalize_query(query)
        topic = _extract_topic(query)
        intent = _detect_intent(query)
        requires_timeline = _requires_timeline(query, intent)
        time_window_days = 7 if requires_timeline else 30
        preferred_sources = _preferred_sources(intent, options)
        excluded_directions = (
            ["国家政策", "历史百科", "宏观政治"]
            if intent in {"cultural_tourism_promotion", "video_platform_hotspot"}
            else []
        )
        rewritten = self._rewrite_queries(query, topic, intent, time_window_days, options)
        search_plan = build_enhanced_search_plan(
            query,
            mode=options.search_enhancement_mode,
            max_queries=max(len(rewritten), 1),
        )
        selected_agents = select_agents_for_intent(intent, options)
        entities = [topic] if topic else [query.strip()]

        return TrendIntent(
            original_query=query,
            normalized_query=normalized,
            topic=topic or query.strip(),
            intent=intent,
            entities=entities,
            time_window_days=time_window_days,
            preferred_sources=preferred_sources,
            excluded_directions=excluded_directions,
            rewritten_queries=rewritten,
            selected_agents=selected_agents,
            requires_timeline=requires_timeline,
            search_enhancement_mode=options.search_enhancement_mode,
            search_enhancement_summary=search_plan.summary,
        )

    def run(
        self,
        query: str,
        options: TrendScopeOptions | None = None,
        search_agency: Any | None = None,
        progress_callback: Any | None = None,
        save_report: bool = True,
    ) -> dict[str, Any]:
        options = options or TrendScopeOptions()
        self._progress(progress_callback, "intent", "正在识别查询意图并生成搜索计划...", 10)
        intent = self.analyze_intent(query, options)

        memory_hit = self.memory_store.find(query) if options.enable_local_knowledge else None
        if memory_hit:
            self._progress(progress_callback, "rag", "本地知识库命中，正在做时效复核...", 25)
            freshness = self._check_freshness(memory_hit, intent, options, search_agency)
            if freshness.reused_cache:
                report = self._annotate_cached_report(memory_hit.final_report, freshness)
                result = self._build_result(intent, report, memory_hit.sources, memory_hit.timeline, freshness)
                if save_report:
                    self._persist_result(result)
                self._progress(progress_callback, "finalizing", "已复用本地知识库结果并完成时效复核", 100)
                return result
        else:
            freshness = FreshnessCheck(cache_hit=False, message="未命中本地知识库。")

        self._progress(progress_callback, "search", "正在聚合公开搜索结果...", 45)
        sources = freshness.current_sources or self.collect_public_sources(intent, options, search_agency)
        source_hash = compute_source_hash(sources)

        self._progress(progress_callback, "timeline", "正在判断并整理事件时间线...", 70)
        timeline = build_timeline(intent, sources)
        report = build_markdown_report(intent, sources, timeline, options, freshness)

        if options.enable_local_knowledge:
            self.memory_store.save(intent, sources, timeline, report, source_hash, options)

        result = self._build_result(
            intent,
            report,
            [s.model_dump() for s in sources],
            [t.model_dump() for t in timeline],
            FreshnessCheck(
                cache_hit=bool(memory_hit),
                refreshed=bool(memory_hit),
                message=(
                    "本地知识库命中但发现来源变化，已刷新分析。"
                    if memory_hit else "已完成公开搜索聚合。"
                ),
                new_source_hash=source_hash,
            ),
        )
        if save_report:
            self._persist_result(result)
        self._progress(progress_callback, "finalizing", "TrendScope 洞察完成", 100)
        return result

    def _rewrite_queries(
        self,
        query: str,
        topic: str,
        intent: str,
        time_window_days: int,
        options: TrendScopeOptions,
    ) -> list[str]:
        optimized = _try_keyword_optimizer(query) if options.search_enhancement_mode == "full" else []
        plan = build_enhanced_search_plan(
            query,
            mode=options.search_enhancement_mode,
            optimized_queries=optimized,
            max_queries=6,
        )
        if options.search_enhancement_mode == "off":
            return plan.queries or [query.strip()]

        rewrites: list[str] = list(plan.queries)
        base = topic or query.strip()
        window = f"最近{time_window_days}天"
        if options.search_enhancement_mode == "full" and intent == "hot_event_tracking":
            rewrites.extend([
                f"{base} 最近发生了什么 {window}",
                f"{base} 事件 时间线 官方回应 网友评论",
                f"{base} 争议 传播 平台 热点",
            ])
        elif options.search_enhancement_mode == "full" and intent == "cultural_tourism_promotion":
            rewrites.extend([
                f"{base} 文旅宣传 热点 {window}",
                f"{base} 短视频 旅游 营销 案例",
                f"{base} 社交平台 传播 爆点",
            ])
        elif options.search_enhancement_mode == "full" and intent == "video_platform_hotspot":
            rewrites.extend([
                f"{base} 视频平台 热门视频 {window}",
                f"{base} 抖音 B站 小红书 热点",
                f"{base} 评论 情绪 爆点",
            ])
        elif options.search_enhancement_mode == "full":
            rewrites.extend([
                f"{base} 热点 {window}",
                f"{base} 新闻 社交平台 传播",
            ])

        if not options.enable_video_hotspots:
            rewrites = [q for q in rewrites if not _contains_any(q, VIDEO_WORDS)]
        return _dedupe([q.strip() for q in rewrites if q.strip()])[:6]

    def collect_public_sources(
        self,
        intent: TrendIntent,
        options: TrendScopeOptions,
        search_agency: Any | None,
    ) -> list[TrendSource]:
        if not options.enable_network_search or search_agency is None:
            return []

        tool_name = "search_last_week" if intent.time_window_days <= 7 else "web_search_only"
        sources: list[TrendSource] = []
        seen: set[str] = set()
        for query in intent.rewritten_queries[:4]:
            response = _call_search(search_agency, tool_name, query)
            for webpage in _iter_webpages(response):
                url = str(getattr(webpage, "url", "") or "")
                title = str(getattr(webpage, "name", "") or "")
                content = str(getattr(webpage, "snippet", "") or "")
                dedupe_key = normalize_result_key(title=title, url=url, content=content)
                if not dedupe_key or dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                published_date = str(getattr(webpage, "date_last_crawled", "") or "")
                sources.append(_score_source(title, url, content, published_date, query))
        ranked = rank_public_search_results(
            [s.model_dump() for s in sources],
            intent.original_query,
            mode=options.search_enhancement_mode,
            max_results=20,
        )
        return [_source_from_dict(item) for item in ranked]

    def _check_freshness(
        self,
        memory_hit: MemoryEntry,
        intent: TrendIntent,
        options: TrendScopeOptions,
        search_agency: Any | None,
    ) -> FreshnessCheck:
        now = datetime.now()
        try:
            fresh_until = datetime.fromisoformat(memory_hit.fresh_until)
        except ValueError:
            fresh_until = now - timedelta(seconds=1)

        if not options.enable_network_search or search_agency is None:
            return FreshnessCheck(
                reused_cache=now <= fresh_until,
                cache_hit=True,
                message=(
                    "来自本地知识库；网络搜索未启用或不可用，按缓存时效复用。"
                    if now <= fresh_until
                    else "本地知识库已过期，且无法完成网络复核。"
                ),
            )

        current_sources = self.collect_public_sources(intent, options, search_agency)
        current_hash = compute_source_hash(current_sources)
        if not current_sources:
            return FreshnessCheck(
                reused_cache=now <= fresh_until,
                cache_hit=True,
                message=(
                    "来自本地知识库；公开搜索未发现新来源，已完成轻量复核。"
                    if now <= fresh_until
                    else "本地知识库已过期，公开搜索未返回可用来源。"
                ),
                new_source_hash=current_hash,
                current_sources=[],
            )

        if current_hash == memory_hit.source_hash:
            return FreshnessCheck(
                reused_cache=True,
                cache_hit=True,
                message="来自本地知识库，已完成时效复核：未发现新高可信来源。",
                new_source_hash=current_hash,
                current_sources=current_sources,
            )

        return FreshnessCheck(
            reused_cache=False,
            cache_hit=True,
            refreshed=True,
            message="本地知识库命中，但公开搜索发现来源集合变化，刷新分析。",
            new_source_hash=current_hash,
            current_sources=current_sources,
        )

    @staticmethod
    def _annotate_cached_report(report: str, freshness: FreshnessCheck) -> str:
        note = f"> {freshness.message}\n\n"
        return report if report.startswith(note) else note + report

    @staticmethod
    def _progress(callback: Any | None, status: str, message: str, pct: int) -> None:
        if callback:
            callback({"status": status, "message": message, "progress_pct": pct})

    @staticmethod
    def _build_result(
        intent: TrendIntent,
        final_report: str,
        sources: list[dict[str, Any]],
        timeline: list[dict[str, Any]],
        freshness: FreshnessCheck,
    ) -> dict[str, Any]:
        paragraphs = [{
            "title": "TrendScope 多源热点洞察",
            "content": "自动编排生成的热点事件洞察。",
            "research": {
                "search_history": sources,
                "latest_summary": _build_summary(intent, [_source_from_dict(s) for s in sources]),
                "is_completed": True,
                "reflection_iteration": 0,
            },
        }]
        return {
            "final_report": final_report,
            "report_title": f"TrendScope: {intent.original_query}",
            "is_completed": True,
            "paragraphs": paragraphs,
            "intent": intent.model_dump(),
            "sources": sources,
            "timeline": timeline,
            "freshness": {
                "cache_hit": freshness.cache_hit,
                "reused_cache": freshness.reused_cache,
                "refreshed": freshness.refreshed,
                "message": freshness.message,
            },
        }

    @staticmethod
    def _persist_result(result: dict[str, Any]) -> None:
        TREND_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = TREND_REPORT_DIR / f"trendscope_{stamp}.md"
        state_file = TREND_REPORT_DIR / f"state_{stamp}.json"
        report_file.write_text(result.get("final_report", ""), encoding="utf-8")
        state_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def select_agents_for_intent(intent: str, options: TrendScopeOptions | dict[str, Any] | None = None) -> list[str]:
    if not isinstance(options, TrendScopeOptions):
        options = TrendScopeOptions.from_payload(options if isinstance(options, dict) else None)

    agents: list[str] = ["trendscope"]
    if options.enable_network_search:
        agents.append("query")

    if (
        options.enable_video_hotspots
        and intent in {"hot_event_tracking", "cultural_tourism_promotion", "video_platform_hotspot", "general_hotspot"}
    ):
        agents.append("media")

    if options.enable_risk_analysis or options.enable_deep_report:
        agents.append("insight")

    if options.enable_deep_report:
        for agent in ("query", "media", "insight"):
            if agent not in agents:
                agents.append(agent)

    return agents


def build_timeline(intent: TrendIntent, sources: list[TrendSource]) -> list[TimelineItem]:
    if not intent.requires_timeline:
        return []

    items: dict[str, TimelineItem] = {}
    current_year = datetime.now().year
    for source in sources:
        date_text = _extract_date(source, current_year)
        if not date_text:
            continue
        event = source.title or source.content[:80] or "相关进展"
        if date_text not in items:
            items[date_text] = TimelineItem(
                date=date_text,
                event=event[:120],
                source_title=source.title,
                source_url=source.url,
            )

    return [items[key] for key in sorted(items.keys())][:8]


def build_markdown_report(
    intent: TrendIntent,
    sources: list[TrendSource],
    timeline: list[TimelineItem],
    options: TrendScopeOptions,
    freshness: FreshnessCheck,
) -> str:
    main_events = sources[:5]
    platform_names = _platform_names(sources)
    credible = sorted(sources, key=lambda s: s.credibility_score, reverse=True)[:6]
    controversy = _controversy_points(sources)
    risk_level, risk_reason = _risk_level(intent, sources, controversy, options)
    source_quality = _source_quality_summary(sources)

    lines = [
        f"# TrendScope 热点洞察：{intent.original_query}",
        "",
        "## 事件概览",
        f"- 识别主题：{intent.topic}",
        f"- 查询意图：{_intent_label(intent.intent)}",
        f"- 时间范围：最近 {intent.time_window_days} 天",
        f"- 自动启用 Agent：{', '.join(intent.selected_agents)}",
        f"- 搜索增强：{_search_enhancement_label(intent.search_enhancement_mode)}",
        f"- 知识库状态：{freshness.message or '本次为实时公开搜索聚合。'}",
        "",
        "## 最近主要事件",
    ]

    if intent.search_enhancement_mode == "full":
        lines.extend([
            "",
            "## 搜索增强摘要",
            f"- {intent.search_enhancement_summary}",
        ])

    if main_events:
        lines.extend([f"- {s.title or s.content[:80]}（{s.source_label}，{s.source_domain or '未知来源'}）" for s in main_events])
    else:
        lines.append("- 暂未从公开搜索中获得可用来源；请检查搜索 API 配置或放宽查询条件。")

    lines.extend(["", "## 时间线"])
    if timeline:
        lines.extend([f"- {item.date}：{item.event}" for item in timeline])
    else:
        lines.append("- 本次查询未触发时间线，或公开来源中没有足够明确的时间节点。")

    lines.extend([
        "",
        "## 涉及人物",
        f"- {', '.join(intent.entities) if intent.entities else '暂无明确人物或实体'}",
        "",
        "## 传播平台",
        f"- {', '.join(platform_names) if platform_names else '暂无明确平台信号'}",
        "",
        "## 争议点",
    ])
    lines.extend([f"- {point}" for point in controversy] if controversy else ["- 暂未发现明确争议点，建议继续关注后续公开回应和多源交叉验证。"])

    lines.extend([
        "",
        "## 网友观点",
        _public_view_summary(sources),
        "",
        "## 可信来源",
    ])
    if credible:
        lines.extend([
            f"- {s.source_label}｜{s.title}｜可信度 {s.credibility}（{s.credibility_reason}）"
            for s in credible
        ])
    else:
        lines.append("- 暂无可评级来源。")

    lines.extend([
        "",
        "## 风险等级",
        f"- {risk_level}：{risk_reason}",
        "",
        "## 后续关注点",
        "- 是否出现官方回应、权威媒体跟进或多平台交叉验证。",
        "- 视频/社交平台评论是否持续升温，是否出现明显情绪反转。",
        "- 低可信来源中的爆料是否被高可信来源证实或澄清。",
        "",
        "## 查询改写与来源策略",
        f"- 改写查询：{'; '.join(intent.rewritten_queries)}",
        f"- 优先来源：{', '.join(intent.preferred_sources)}",
        f"- 排除方向：{', '.join(intent.excluded_directions) if intent.excluded_directions else '无'}",
        f"- 来源质量：{source_quality}",
    ])
    return "\n".join(lines)


def compute_source_hash(sources: Iterable[TrendSource]) -> str:
    parts = []
    for source in sources:
        parts.append("|".join([
            source.url or "",
            source.title or "",
            source.published_date or "",
            source.source_type or "",
        ]))
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def _detect_intent(query: str) -> str:
    if _contains_any(query, EVENT_WORDS):
        return "hot_event_tracking"
    if _contains_any(query, TOURISM_WORDS):
        return "cultural_tourism_promotion"
    if _contains_any(query, VIDEO_WORDS):
        return "video_platform_hotspot"
    return "general_hotspot"


def _requires_timeline(query: str, intent: str) -> bool:
    if intent == "hot_event_tracking":
        return True
    return _contains_any(query, {"时间线", "过程", "始末", "发生了什么", "怎么了", "最近"})


def _extract_topic(query: str) -> str:
    text = (query or "").strip()
    for word in TOPIC_NOISE_WORDS:
        text = text.replace(word, "")
    text = re.sub(r"\s+", " ", text).strip(" ?？。！!")
    return text[:40] or query.strip()[:40]


def _preferred_sources(intent: str, options: TrendScopeOptions) -> list[str]:
    if intent == "cultural_tourism_promotion":
        sources = ["视频平台", "社交平台", "新闻平台"]
    elif intent == "video_platform_hotspot":
        sources = ["视频平台", "社交平台", "新闻平台"]
    elif intent == "hot_event_tracking":
        sources = ["新闻平台", "社交平台", "视频平台", "官方来源"]
    else:
        sources = ["新闻平台", "社交平台"]
    if not options.enable_video_hotspots:
        sources = [s for s in sources if s != "视频平台"]
    return sources


def _try_keyword_optimizer(query: str) -> list[str]:
    if not (settings.KEYWORD_OPTIMIZER_API_KEY and settings.KEYWORD_OPTIMIZER_BASE_URL and settings.KEYWORD_OPTIMIZER_MODEL_NAME):
        return []
    try:
        from engines.InsightEngine.tools.keyword_optimizer import get_keyword_optimizer

        result = get_keyword_optimizer().optimize_keywords(query)
        if result.success:
            return [str(item) for item in result.optimized_keywords if str(item).strip()]
    except Exception as exc:
        logger.warning(f"Keyword optimizer unavailable, using heuristic rewrites: {exc}")
    return []


def _call_search(search_agency: Any, tool_name: str, query: str) -> Any:
    fn = getattr(search_agency, tool_name, None)
    if fn is None:
        fn = getattr(search_agency, "web_search_only", None) or getattr(search_agency, "comprehensive_search", None)
    if fn is None:
        return None
    try:
        if tool_name in {"web_search_only", "comprehensive_search"}:
            return fn(query, max_results=8)
        return fn(query)
    except TypeError:
        return fn(query)
    except Exception as exc:
        logger.warning(f"TrendScope search failed for {query}: {exc}")
        return None


def _iter_webpages(response: Any) -> Iterable[Any]:
    if response is None:
        return []
    webpages = getattr(response, "webpages", None)
    if webpages is not None:
        return webpages
    results = getattr(response, "results", None)
    if results is not None:
        return [
            _SimpleWebpage(
                name=getattr(item, "title", ""),
                url=getattr(item, "url", ""),
                snippet=getattr(item, "content", ""),
                date_last_crawled=getattr(item, "published_date", ""),
            )
            for item in results
        ]
    return []


@dataclass
class _SimpleWebpage:
    name: str
    url: str
    snippet: str
    date_last_crawled: str = ""


def _score_source(title: str, url: str, content: str, published_date: str, query: str) -> TrendSource:
    rating = classify_source(url)
    domain = rating.get("source_domain") or _domain(url)
    source_type = rating.get("source_type", "media_or_unknown")
    source_label = rating.get("source_label", "普通媒体或未知来源")
    credibility = rating.get("credibility", "medium")

    if _is_video_or_social_domain(domain):
        source_type = "video_or_social"
        source_label = "视频/社交平台"
        credibility = "medium"

    score = {
        "official": 90,
        "academic": 82,
        "authoritative_media": 78,
        "video_or_social": 64,
        "media_or_unknown": 48,
    }.get(source_type, 45)

    reason_parts = [source_label]
    if _is_recent(published_date, 7):
        score += 8
        reason_parts.append("发布时间较近")
    elif _is_recent(published_date, 30):
        score += 4
        reason_parts.append("30天内来源")

    if _contains_any(title + content, RISKY_TITLE_WORDS):
        score -= 10
        reason_parts.append("标题或摘要存在待核实表述")

    score = max(10, min(100, score))
    return TrendSource(
        title=title,
        url=url,
        content=content,
        published_date=published_date,
        source_type=source_type,
        credibility=_credibility_label(score),
        source_label=source_label,
        source_domain=domain,
        credibility_score=score,
        credibility_reason="、".join(reason_parts),
        query=query,
    )


def classify_source(url: str) -> dict[str, str]:
    host = _domain(url)
    if any(_matches_domain(host, domain) for domain in OFFICIAL_DOMAINS):
        return {
            "source_type": "official",
            "credibility": "very_high",
            "source_label": "官方来源",
            "source_domain": host,
        }
    if any(_matches_domain(host, domain) for domain in ACADEMIC_DOMAINS):
        return {
            "source_type": "academic",
            "credibility": "high",
            "source_label": "学术/研究来源",
            "source_domain": host,
        }
    if any(_matches_domain(host, domain) for domain in AUTHORITATIVE_MEDIA_DOMAINS):
        return {
            "source_type": "authoritative_media",
            "credibility": "high",
            "source_label": "权威媒体",
            "source_domain": host,
        }
    return {
        "source_type": "media_or_unknown",
        "credibility": "medium",
        "source_label": "普通媒体或未知来源",
        "source_domain": host,
    }


def _matches_domain(host: str, domain: str) -> bool:
    domain = domain.lower().lstrip("www.")
    return host == domain or host.endswith("." + domain)


def _source_from_dict(data: dict[str, Any]) -> TrendSource:
    return TrendSource(
        title=str(data.get("title", "")),
        url=str(data.get("url", "")),
        content=str(data.get("content", "")),
        published_date=str(data.get("published_date", "")),
        source_type=str(data.get("source_type", "media_or_unknown")),
        credibility=str(data.get("credibility", "medium")),
        source_label=str(data.get("source_label", "普通媒体或未知来源")),
        source_domain=str(data.get("source_domain", "")),
        credibility_score=int(data.get("credibility_score") or 45),
        credibility_reason=str(data.get("credibility_reason", "")),
        query=str(data.get("query", "")),
    )


def _extract_date(source: TrendSource, current_year: int) -> str:
    parsed = _parse_date(source.published_date)
    if parsed:
        return parsed.strftime("%Y-%m-%d")

    text = f"{source.title} {source.content}"
    match = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if match:
        return f"{current_year:04d}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    return ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _is_recent(value: str, days: int) -> bool:
    parsed = _parse_date(value)
    if not parsed:
        return False
    return parsed >= datetime.now() - timedelta(days=days)


def _credibility_label(score: int) -> str:
    if score >= 75:
        return "高"
    if score >= 55:
        return "中"
    return "低"


def _domain(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().lstrip("www.")


def _is_video_or_social_domain(domain: str) -> bool:
    return any(token in domain for token in (
        "douyin", "bilibili", "b23.tv", "xiaohongshu", "xhs", "kuaishou",
        "weibo", "zhihu", "tieba", "toutiao",
    ))


def _platform_names(sources: list[TrendSource]) -> list[str]:
    names = []
    mapping = {
        "douyin": "抖音",
        "bilibili": "B站",
        "b23.tv": "B站",
        "xiaohongshu": "小红书",
        "xhs": "小红书",
        "kuaishou": "快手",
        "weibo": "微博",
        "zhihu": "知乎",
        "tieba": "贴吧",
        "toutiao": "头条",
    }
    for source in sources:
        for token, label in mapping.items():
            if token in source.source_domain and label not in names:
                names.append(label)
    return names


def _controversy_points(sources: list[TrendSource]) -> list[str]:
    points = []
    for source in sources:
        text = f"{source.title} {source.content}"
        if _contains_any(text, CONTROVERSY_WORDS):
            points.append((source.title or source.content[:80])[:120])
    return _dedupe(points)[:5]


def _public_view_summary(sources: list[TrendSource]) -> str:
    social = [s for s in sources if s.source_type == "video_or_social"]
    if not social:
        return "- 暂未获得足够的视频/社交平台评论摘要；可开启视频平台热点分析或补充公开来源。"
    samples = "；".join([(s.title or s.content[:60])[:80] for s in social[:3]])
    return f"- 公开社交/视频结果主要集中在：{samples}。评论情绪需要结合更完整评论样本继续验证。"


def _risk_level(
    intent: TrendIntent,
    sources: list[TrendSource],
    controversy: list[str],
    options: TrendScopeOptions,
) -> tuple[str, str]:
    if not sources:
        return "中", "公开来源不足，无法完成交叉验证。"
    avg_score = sum(s.credibility_score for s in sources) / len(sources)
    unknown_ratio = len([s for s in sources if s.credibility == "低"]) / max(len(sources), 1)
    if options.enable_risk_analysis and (controversy or unknown_ratio > 0.35):
        return "高", "存在争议表达或低可信来源占比较高，需等待权威来源交叉验证。"
    if controversy or avg_score < 58:
        return "中", "存在部分争议或来源可信度一般，建议持续复核。"
    return "低", "当前来源可信度整体较高，暂未发现明显舆情风险。"


def _source_quality_summary(sources: list[TrendSource]) -> str:
    if not sources:
        return "暂无来源。"
    high = len([s for s in sources if s.credibility == "高"])
    mid = len([s for s in sources if s.credibility == "中"])
    low = len([s for s in sources if s.credibility == "低"])
    return f"高可信 {high} 条，中可信 {mid} 条，低可信 {low} 条。"


def _build_summary(intent: TrendIntent, sources: list[TrendSource]) -> str:
    return f"{intent.topic}｜{_intent_label(intent.intent)}｜来源 {len(sources)} 条"


def _intent_label(intent: str) -> str:
    return {
        "hot_event_tracking": "热点事件追踪",
        "cultural_tourism_promotion": "文旅宣传/热点传播",
        "video_platform_hotspot": "视频平台热点",
        "general_hotspot": "综合热点分析",
    }.get(intent, intent)


def _search_enhancement_label(mode: str) -> str:
    return {
        "off": "关闭（仅基础降噪）",
        "light": "轻量（规则改写与平台提示）",
        "full": "完整（关键词优化、分类与摘要）",
    }.get(mode, mode or "关闭")


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(word in text for word in words)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


EVENT_WORDS = {
    "最近", "发生了什么", "怎么了", "咋了", "事件", "争议", "回应", "爆料", "塌房",
    "翻车", "后续", "时间线", "始末", "热点", "热搜",
}
TOURISM_WORDS = {"文旅", "宣传", "旅游", "景区", "城市营销", "地方营销", "打卡", "游客"}
VIDEO_WORDS = {"视频", "短视频", "抖音", "b站", "哔哩", "小红书", "快手", "网红", "博主", "up主", "直播"}
CONTROVERSY_WORDS = {"争议", "质疑", "回应", "道歉", "辟谣", "网传", "爆料", "冲突", "反转", "投诉"}
RISKY_TITLE_WORDS = {"震惊", "网传", "爆料", "知情人", "内幕", "疑似", "据说", "疯传"}
TOPIC_NOISE_WORDS = [
    "最近发生了什么", "发生了什么", "最近怎么了", "怎么了", "咋了", "最近",
    "文旅宣传", "宣传", "热点传播", "视频平台内容", "视频平台", "短视频",
    "时间线", "事件", "热点", "热搜",
]
OFFICIAL_DOMAINS = (
    "gov.cn", "stats.gov.cn", "ndrc.gov.cn", "mof.gov.cn", "miit.gov.cn",
    "pbc.gov.cn", "csrc.gov.cn", "samr.gov.cn", "mfa.gov.cn", "nhc.gov.cn",
)
AUTHORITATIVE_MEDIA_DOMAINS = (
    "xinhuanet.com", "people.com.cn", "cctv.com", "china.com.cn",
    "chinanews.com.cn", "gmw.cn", "ce.cn",
)
ACADEMIC_DOMAINS = ("edu.cn", "ac.cn", "cnki.net")
