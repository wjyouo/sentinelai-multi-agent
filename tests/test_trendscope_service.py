from dataclasses import dataclass
from pathlib import Path


@dataclass
class FakeWebpage:
    name: str
    url: str
    snippet: str
    date_last_crawled: str = "2026-07-05"


class FakeResponse:
    def __init__(self, webpages):
        self.webpages = webpages


class FakeSearchAgency:
    def __init__(self, webpages):
        self.webpages = webpages
        self.calls = []

    def search_last_week(self, query):
        self.calls.append(("search_last_week", query))
        return FakeResponse(self.webpages)

    def web_search_only(self, query, max_results=8):
        self.calls.append(("web_search_only", query, max_results))
        return FakeResponse(self.webpages)

    def comprehensive_search(self, query, max_results=8):
        self.calls.append(("comprehensive_search", query, max_results))
        return FakeResponse(self.webpages)


def _service(tmp_path: Path):
    from app.services.trendscope_service import TrendMemoryStore, TrendScopeOrchestrator

    return TrendScopeOrchestrator(
        memory_store=TrendMemoryStore(tmp_path / "trendscope_memory.sqlite3")
    )


def test_tibet_promotion_intent_rewrites_and_excludes_policy(tmp_path, monkeypatch):
    from app.services import trendscope_service
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    monkeypatch.setattr(trendscope_service, "_try_keyword_optimizer", lambda query: [])
    intent = svc.analyze_intent("西藏宣传", TrendScopeOptions(search_enhancement_mode="full"))

    assert intent.intent == "cultural_tourism_promotion"
    assert intent.topic == "西藏"
    assert "国家政策" in intent.excluded_directions
    assert "历史百科" in intent.excluded_directions
    assert any("文旅宣传" in query for query in intent.rewritten_queries)
    assert "trendscope" in intent.selected_agents
    assert "media" in intent.selected_agents
    assert intent.search_enhancement_mode == "full"


def test_search_enhancement_defaults_to_off_and_keeps_original_query(tmp_path):
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    options = TrendScopeOptions.from_payload({})
    intent = svc.analyze_intent("西藏宣传", options)

    assert options.search_enhancement_mode == "off"
    assert intent.rewritten_queries == ["西藏宣传"]
    assert intent.search_enhancement_mode == "off"


def test_light_search_enhancement_generates_precise_event_queries(tmp_path):
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    intent = svc.analyze_intent(
        "某某网红最近发生了什么",
        TrendScopeOptions(search_enhancement_mode="light"),
    )

    assert intent.rewritten_queries[0] == "某某网红最近发生了什么"
    assert any('"某某网红"' in query for query in intent.rewritten_queries)
    assert any("微博 抖音 B站 小红书" in query for query in intent.rewritten_queries)


def test_invalid_search_enhancement_mode_falls_back_to_off():
    from app.services.trendscope_service import TrendScopeOptions

    options = TrendScopeOptions.from_payload({"search_enhancement_mode": "unknown"})

    assert options.search_enhancement_mode == "off"


def test_recent_person_event_requires_timeline(tmp_path):
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    agency = FakeSearchAgency([
        FakeWebpage(
            name="7月1日 某某网红相关事件首次出现",
            url="https://news.example.com/a",
            snippet="7月2日 多个账号转发，7月3日 当事人回应。",
            date_last_crawled="2026-07-03",
        )
    ])

    result = svc.run("某某网红最近发生了什么", TrendScopeOptions(), agency, save_report=False)

    assert result["intent"]["requires_timeline"] is True
    assert result["timeline"]
    assert "时间线" in result["final_report"]


def test_full_search_enhancement_falls_back_when_optimizer_unavailable(tmp_path, monkeypatch):
    from app.services import trendscope_service
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    monkeypatch.setattr(trendscope_service, "_try_keyword_optimizer", lambda query: [])
    intent = svc.analyze_intent(
        "某品牌最近发生了什么",
        TrendScopeOptions(search_enhancement_mode="full"),
    )

    assert intent.search_enhancement_mode == "full"
    assert intent.rewritten_queries


def test_public_search_ranking_demotes_unmatched_old_results():
    from app.services.search_enhancement import rank_public_search_results

    ranked = rank_public_search_results(
        [
            {
                "title": "同名人物百科资料",
                "url": "https://example.com/old",
                "content": "十年前的旧闻回顾。",
                "published_date": "2020-01-01",
            },
            {
                "title": "某某网红最新回应",
                "url": "https://news.example.com/new",
                "content": "某某网红针对事件发布回应。",
                "published_date": "2026-07-05",
            },
        ],
        "某某网红最近发生了什么",
        mode="light",
    )

    assert ranked[0]["title"] == "某某网红最新回应"


def test_non_event_query_does_not_force_timeline(tmp_path):
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    intent = svc.analyze_intent("新能源汽车市场分析", TrendScopeOptions())

    assert intent.requires_timeline is False


def test_rag_hit_reuses_cache_after_freshness_check(tmp_path):
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    agency = FakeSearchAgency([
        FakeWebpage(
            name="7月1日 品牌事件进展",
            url="https://xinhuanet.com/a",
            snippet="权威媒体报道同一进展。",
            date_last_crawled="2026-07-01",
        )
    ])

    first = svc.run("某品牌最近发生了什么", TrendScopeOptions(), agency, save_report=False)
    second = svc.run("某品牌最近发生了什么", TrendScopeOptions(), agency, save_report=False)

    assert first["freshness"]["cache_hit"] is False
    assert second["freshness"]["cache_hit"] is True
    assert second["freshness"]["reused_cache"] is True
    assert "本地知识库" in second["final_report"]


def test_rag_hit_refreshes_when_sources_change(tmp_path):
    from app.services.trendscope_service import TrendScopeOptions

    svc = _service(tmp_path)
    first_agency = FakeSearchAgency([
        FakeWebpage(
            name="7月1日 城市热点首次传播",
            url="https://news.example.com/old",
            snippet="初始报道。",
            date_last_crawled="2026-07-01",
        )
    ])
    changed_agency = FakeSearchAgency([
        FakeWebpage(
            name="7月2日 城市热点出现新回应",
            url="https://news.example.com/new",
            snippet="新来源出现。",
            date_last_crawled="2026-07-02",
        )
    ])

    svc.run("某城市最近发生了什么", TrendScopeOptions(), first_agency, save_report=False)
    refreshed = svc.run("某城市最近发生了什么", TrendScopeOptions(), changed_agency, save_report=False)

    assert refreshed["freshness"]["cache_hit"] is True
    assert refreshed["freshness"]["refreshed"] is True
    assert "新回应" in refreshed["final_report"]
