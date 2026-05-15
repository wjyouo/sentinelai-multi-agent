#!/usr/bin/env python3
"""
Bocha vs Tavily 搜索质量对比评估

用法:
    python scripts/compare_search_platforms.py
    python scripts/compare_search_platforms.py "自定义查询1" "自定义查询2"
    python scripts/compare_search_platforms.py --judge query   # 使用 Query Engine(DeepSeek) 当裁判
    python scripts/compare_search_platforms.py --judge insight # 使用 Insight Engine(Kimi) 当裁判

机制:
    1. 对同一组查询分别调用 Bocha 和 Tavily 搜索
    2. 由 LLM 裁判对每个平台的搜索结果从三个维度打分（1-10）：
       - 真实性：信息来源是否可信、内容是否准确
       - 时效性：信息是否是最新的、发布时间是否合理
       - 相关性：结果是否贴合查询意图
    3. 输出逐条评分 + 汇总对比表 + JSON 结果文件到 logs/
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, "engines"))  # MediaEngine 内部有 from common.llm_client 的 import


# ══════════════════════════════════════════════════════════════════
# 测试查询集
# ══════════════════════════════════════════════════════════════════

TEST_QUERIES = [
    {
        "query": "OpenAI最新模型GPT-5发布进展 2026",
        "focus": "时效性",  # 需要最新消息
        "description": "科技新闻 — 考察时效性和信息准确度",
    },
    {
        "query": "中国新能源汽车2026年出口数据",
        "focus": "真实性",  # 需要准确数据
        "description": "经济数据 — 考察来源可信度和数据一致性",
    },
    {
        "query": "北京春季花粉过敏防治方法",
        "focus": "相关性",  # 需要贴近生活
        "description": "生活健康 — 考察中文语境下的相关性和实用性",
    },
    {
        "query": "最近发生的中美贸易最新进展",
        "focus": "时效性",
        "description": "国际时事 — 考察时效性和多角度覆盖",
    },
    {
        "query": "量子计算最新突破 2026",
        "focus": "真实性",
        "description": "前沿科技 — 考察信息准确度和专业深度",
    },
    {
        "query": "五一假期国内旅游热门目的地推荐",
        "focus": "相关性",
        "description": "旅游出行 — 考察中文搜索相关性和实用性",
    },
]

# ══════════════════════════════════════════════════════════════════
# 裁判 LLM 的评分提示词
# ══════════════════════════════════════════════════════════════════

JUDGE_SYSTEM_PROMPT = """你是一位严格的信息检索质量评估专家。你需要对搜索引擎返回的结果进行三维度评分。

## 评分维度（每个维度 1-10 分）

**真实性 (authenticity)**
- 10分：所有来源都是权威机构（政府/学术/知名媒体），内容可交叉验证，无错误信息
- 7-9分：多数来源可信，个别来源不明确但不影响整体
- 4-6分：来源参差不齐，有明显不可靠来源
- 1-3分：大量不可靠来源，存在错误或虚假信息

**时效性 (timeliness)**
- 10分：所有结果都是近一周内发布，紧扣最新动态
- 7-9分：多数结果较新（1个月内），能反映当前状况
- 4-6分：部分结果较旧（3-6个月），信息可能已过时
- 1-3分：结果明显过时（>1年），无法反映当前情况

**相关性 (relevance)**
- 10分：所有结果精确匹配查询意图，无无关条目
- 7-9分：多数结果相关，偶有偏题
- 4-6分：约一半相关，混杂大量无关内容
- 1-3分：基本不相关，答非所问

## 输出格式

严格按以下 JSON 格式输出，不要包含任何其他文字：

```json
{
  "overall_assessment": "一句话总评,对比两个平台在本查询上的表现",
  "bocha": {
    "authenticity": {"score": 8, "reason": "..."},
    "timeliness": {"score": 7, "reason": "..."},
    "relevance": {"score": 9, "reason": "..."}
  },
  "tavily": {
    "authenticity": {"score": 8, "reason": "..."},
    "timeliness": {"score": 7, "reason": "..."},
    "relevance": {"score": 9, "reason": "..."}
  }
}
```"""


def build_judge_user_prompt(query: str, bocha_result: dict, tavily_result: dict) -> str:
    """构建给裁判 LLM 的 user prompt，包含两个平台的搜索结果."""
    prompt_parts = [
        f"## 查询\n{query}",
        "",
        "## Bocha 搜索结果",
    ]

    if bocha_result.get("error"):
        prompt_parts.append(f"搜索失败: {bocha_result['error']}")
    else:
        if bocha_result.get("answer"):
            prompt_parts.append(f"### AI 摘要\n{str(bocha_result['answer'])[:1500]}")
        prompt_parts.append(f"### 网页结果 ({len(bocha_result.get('webpages', []))} 条)")
        for i, wp in enumerate(bocha_result.get("webpages", [])[:10], 1):
            prompt_parts.append(
                f"{i}. **{wp.get('name', '无标题')}**\n"
                f"   URL: {wp.get('url', '')}\n"
                f"   摘要: {str(wp.get('snippet', ''))[:300]}\n"
                f"   抓取时间: {wp.get('date_last_crawled', '未知')}"
            )

    prompt_parts.append("")
    prompt_parts.append("## Tavily 搜索结果")

    if tavily_result.get("error"):
        prompt_parts.append(f"搜索失败: {tavily_result['error']}")
    else:
        if tavily_result.get("answer"):
            prompt_parts.append(f"### AI 摘要\n{str(tavily_result['answer'])[:1500]}")
        prompt_parts.append(f"### 网页结果 ({len(tavily_result.get('webpages', []))} 条)")
        for i, wp in enumerate(tavily_result.get("webpages", [])[:10], 1):
            prompt_parts.append(
                f"{i}. **{wp.get('name', '无标题')}**\n"
                f"   URL: {wp.get('url', '')}\n"
                f"   摘要: {str(wp.get('snippet', ''))[:300]}\n"
                f"   发布时间: {wp.get('date_last_crawled', '未知')}"
            )

    prompt_parts.append("")
    prompt_parts.append("请根据上述搜索结果，对 Bocha 和 Tavily 分别进行三维度评分，严格按 JSON 格式输出。")

    return "\n".join(prompt_parts)


# ══════════════════════════════════════════════════════════════════
# 搜索 & 评分工序
# ══════════════════════════════════════════════════════════════════

@dataclass
class SearchResult:
    platform: str
    query: str
    status: str
    elapsed_ms: float
    answer: str
    webpages: list
    images_count: int
    modal_cards_count: int
    follow_ups_count: int
    error: str


@dataclass
class JudgeScore:
    query: str
    bocha: dict
    tavily: dict
    overall: str
    raw_response: str


def search_bocha(query: str) -> dict:
    start = time.perf_counter()
    try:
        from engines.MediaEngine.tools.search import BochaMultimodalSearch
        client = BochaMultimodalSearch()
        resp = client.comprehensive_search(query, max_results=10)
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "status": "success", "elapsed_ms": round(elapsed, 1),
            "answer": resp.answer or "",
            "webpages": [{"name": w.name, "url": w.url, "snippet": w.snippet or "",
                          "display_url": w.display_url or "", "date_last_crawled": w.date_last_crawled or ""}
                         for w in (resp.webpages or [])],
            "images_count": len(resp.images) if resp.images else 0,
            "modal_cards_count": len(resp.modal_cards) if resp.modal_cards else 0,
            "follow_ups_count": len(resp.follow_ups) if resp.follow_ups else 0,
            "error": None,
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"status": "error", "elapsed_ms": round(elapsed, 1), "error": str(e),
                "answer": "", "webpages": [], "images_count": 0,
                "modal_cards_count": 0, "follow_ups_count": 0}


def search_tavily(query: str) -> dict:
    start = time.perf_counter()
    try:
        from engines.MediaEngine.tools.search import TavilySearchWrapper
        client = TavilySearchWrapper()
        resp = client.comprehensive_search(query, max_results=10)
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "status": "success", "elapsed_ms": round(elapsed, 1),
            "answer": resp.answer or "",
            "webpages": [{"name": w.name, "url": w.url, "snippet": w.snippet or "",
                          "display_url": w.display_url or "", "date_last_crawled": w.date_last_crawled or ""}
                         for w in (resp.webpages or [])],
            "images_count": len(resp.images) if resp.images else 0,
            "modal_cards_count": len(resp.modal_cards) if resp.modal_cards else 0,
            "follow_ups_count": len(resp.follow_ups) if resp.follow_ups else 0,
            "error": None,
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"status": "error", "elapsed_ms": round(elapsed, 1), "error": str(e),
                "answer": "", "webpages": [], "images_count": 0,
                "modal_cards_count": 0, "follow_ups_count": 0}


def create_judge_llm(judge_type: str):
    """根据 --judge 参数创建裁判 LLM 客户端"""
    from app.config import settings
    from engines.common.llm_client import LLMClient

    if judge_type == "insight":
        return LLMClient(
            api_key=settings.INSIGHT_ENGINE_API_KEY,
            model_name=settings.INSIGHT_ENGINE_MODEL_NAME,
            base_url=settings.INSIGHT_ENGINE_BASE_URL,
            engine_name="Judge-Insight",
        )
    elif judge_type == "query":
        return LLMClient(
            api_key=settings.QUERY_ENGINE_API_KEY,
            model_name=settings.QUERY_ENGINE_MODEL_NAME,
            base_url=settings.QUERY_ENGINE_BASE_URL,
            engine_name="Judge-Query",
        )
    else:
        # 默认用 media engine (gemini) 当裁判
        return LLMClient(
            api_key=settings.MEDIA_ENGINE_API_KEY,
            model_name=settings.MEDIA_ENGINE_MODEL_NAME,
            base_url=settings.MEDIA_ENGINE_BASE_URL,
            engine_name="Judge-Media",
        )


def judge(judge_llm, query: str, bocha_raw: dict, tavily_raw: dict) -> JudgeScore:
    """调用裁判 LLM 对两个平台的结果打分"""
    user_prompt = build_judge_user_prompt(query, bocha_raw, tavily_raw)

    try:
        raw = judge_llm.invoke(JUDGE_SYSTEM_PROMPT, user_prompt, temperature=0.3)
    except Exception as e:
        return JudgeScore(query=query, bocha={}, tavily={},
                          overall=f"裁判调用失败: {e}", raw_response=str(e))

    # 解析 JSON
    try:
        import re
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if json_match:
            parsed = json.loads(json_match.group())
        else:
            parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}

    return JudgeScore(
        query=query,
        bocha=parsed.get("bocha", {}),
        tavily=parsed.get("tavily", {}),
        overall=parsed.get("overall_assessment", raw[:200]),
        raw_response=raw,
    )


# ══════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════

def print_sep(char="─", width=90):
    print(char * width)


def print_header(title: str):
    print()
    print_sep("=")
    print(f"  {title}")
    print_sep("=")


def print_score_card(label: str, scores: dict):
    """打印一个平台的评分卡"""
    if not scores:
        print(f"  [{label}] 无评分数据")
        return
    auth = scores.get("authenticity", {})
    time_s = scores.get("timeliness", {})
    rel = scores.get("relevance", {})
    total = auth.get("score", 0) + time_s.get("score", 0) + rel.get("score", 0)

    print(f"  [{label}] 总分: {total}/30")
    print(f"    真实性: {auth.get('score', '?')}/10 — {auth.get('reason', '无')[:120]}")
    print(f"    时效性: {time_s.get('score', '?')}/10 — {time_s.get('reason', '无')[:120]}")
    print(f"    相关性: {rel.get('score', '?')}/10 — {rel.get('reason', '无')[:120]}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Bocha vs Tavily 搜索质量对比")
    parser.add_argument("queries", nargs="*", help="自定义查询词（可多个）")
    parser.add_argument("--judge", choices=["insight", "query", "media"], default="query",
                        help="裁判 LLM: insight(Kimi) / query(DeepSeek) / media(Gemini)，默认 query")
    parser.add_argument("--no-llm", action="store_true", help="仅搜索不评分（快速检查 API 连通性）")
    args = parser.parse_args()

    queries = args.queries if args.queries else [q["query"] for q in TEST_QUERIES]
    if not args.queries:
        # 打印测试用例描述
        print("使用内置测试用例：")
        for i, tq in enumerate(TEST_QUERIES, 1):
            print(f"  {i}. [{tq['focus']}] {tq['query']}")
            print(f"     {tq['description']}")
        print()

    print_header("Bocha vs Tavily 搜索质量对比评估")
    print(f"  查询数: {len(queries)}")
    print(f"  裁判 LLM: {args.judge}")
    print(f"  LLM 评分: {'禁用' if args.no_llm else '启用'}")
    print(f"  时间: {datetime.now(tz=timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 检查 API Key
    from app.config import settings
    bocha_ok = bool(settings.BOCHA_WEB_SEARCH_API_KEY or os.getenv("BOCHA_WEB_SEARCH_API_KEY"))
    tavily_ok = bool(settings.TAVILY_API_KEY or os.getenv("TAVILY_API_KEY"))
    print(f"  API Key: Bocha={'✓' if bocha_ok else '✗'}  Tavily={'✓' if tavily_ok else '✗'}")

    if not args.no_llm:
        judge_llm = create_judge_llm(args.judge)
        print(f"  裁判模型: {judge_llm.model_name}")

    # ── 执行搜索 & 评分 ──
    all_scores: list[JudgeScore] = []
    all_raw: list[dict] = []

    for i, q in enumerate(queries, 1):
        print_sep("-")
        print(f"  [{i}/{len(queries)}] 搜索: \"{q}\"")

        bocha_raw = search_bocha(q)
        tavily_raw = search_tavily(q)

        # 简要统计
        for name, raw in [("Bocha", bocha_raw), ("Tavily", tavily_raw)]:
            icon = "✓" if raw["status"] == "success" else "✗"
            extra = ""
            if raw["status"] == "success":
                extra = f" | {len(raw['answer'])}字答案 | {len(raw['webpages'])}条网页 | {raw['elapsed_ms']}ms"
                if raw.get("modal_cards_count"):
                    extra += f" | {raw['modal_cards_count']}卡片"
                if raw.get("follow_ups_count"):
                    extra += f" | {raw['follow_ups_count']}追问"
            else:
                extra = f" | 错误: {raw.get('error', 'unknown')[:120]}"
            print(f"    {name}: {icon}{extra}")

        all_raw.append({"query": q, "bocha": bocha_raw, "tavily": tavily_raw,
                         "timestamp": datetime.now(tz=timezone(timedelta(hours=8))).isoformat()})

        if not args.no_llm and bocha_raw["status"] == "success" and tavily_raw["status"] == "success":
            print(f"    等待 LLM 裁判评分...")
            score = judge(judge_llm, q, bocha_raw, tavily_raw)
            all_scores.append(score)
            print_score_card("Bocha", score.bocha)
            print_score_card("Tavily", score.tavily)
            if score.overall:
                print(f"    总评: {score.overall[:200]}")
        elif not args.no_llm:
            print(f"    跳过评分（搜索失败）")

    # ── 汇总 ──
    if all_scores:
        print_header("汇总排名")
        bocha_total = sum(
            s.bocha.get("authenticity", {}).get("score", 0) +
            s.bocha.get("timeliness", {}).get("score", 0) +
            s.bocha.get("relevance", {}).get("score", 0)
            for s in all_scores
        )
        tavily_total = sum(
            s.tavily.get("authenticity", {}).get("score", 0) +
            s.tavily.get("timeliness", {}).get("score", 0) +
            s.tavily.get("relevance", {}).get("score", 0)
            for s in all_scores
        )
        max_total = len(all_scores) * 30

        print(f"  Bocha  总分: {bocha_total}/{max_total}  ({bocha_total / max_total * 100:.0f}%)" if max_total else "")
        print(f"  Tavily 总分: {tavily_total}/{max_total}  ({tavily_total / max_total * 100:.0f}%)" if max_total else "")

        # 分维度汇总
        for dim, label in [("authenticity", "真实性"), ("timeliness", "时效性"), ("relevance", "相关性")]:
            bocha_dim = sum(s.bocha.get(dim, {}).get("score", 0) for s in all_scores)
            tavily_dim = sum(s.tavily.get(dim, {}).get("score", 0) for s in all_scores)
            max_dim = len(all_scores) * 10
            winner = "Bocha" if bocha_dim > tavily_dim else "Tavily" if tavily_dim > bocha_dim else "平局"
            print(f"  {label}: Bocha {bocha_dim}/{max_dim} | Tavily {tavily_dim}/{max_dim}  ← {winner}")

    # 性能汇总
    print_header("性能汇总")
    bocha_times = [r["bocha"]["elapsed_ms"] for r in all_raw if r["bocha"]["status"] == "success"]
    tavily_times = [r["tavily"]["elapsed_ms"] for r in all_raw if r["tavily"]["status"] == "success"]
    if bocha_times:
        print(f"  Bocha  平均耗时: {sum(bocha_times) / len(bocha_times):.0f}ms")
    if tavily_times:
        print(f"  Tavily 平均耗时: {sum(tavily_times) / len(tavily_times):.0f}ms")

    # 能力差异
    print_header("能力差异")
    print("  Bocha 专属: AI 摘要(answer) + 多模态卡片(天气/股票等) + 追问建议(follow_ups)")
    print("  Tavily 专属: search_depth=advanced 深度搜索模式")
    bocha_answers = sum(1 for r in all_raw if r["bocha"]["status"] == "success" and r["bocha"]["answer"])
    tavily_answers = sum(1 for r in all_raw if r["tavily"]["status"] == "success" and r["tavily"]["answer"])
    print(f"  有 AI 摘要: Bocha {bocha_answers}/{len(queries)}, Tavily {tavily_answers}/{len(queries)}")

    # ── 保存 JSON ──
    ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    os.makedirs("logs", exist_ok=True)
    output = {
        "config": {"judge": args.judge, "llm_enabled": not args.no_llm,
                    "timestamp": datetime.now(tz=timezone(timedelta(hours=8))).isoformat()},
        "queries": queries,
        "raw_results": all_raw,
        "judge_scores": [
            {"query": s.query, "overall": s.overall,
             "bocha": s.bocha, "tavily": s.tavily}
            for s in all_scores
        ],
    }
    filepath = f"logs/search_compare_{ts}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果已保存: {filepath}")
    print("用以下命令查看: cat " + filepath + " | python -m json.tool | less")


if __name__ == "__main__":
    main()
