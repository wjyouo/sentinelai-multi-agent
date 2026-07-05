"""
InsightEngine entry point — 提供模块级别方法： run_research().

The core logic lives in run_research() at module level.
DeepSearchAgent has been removed; graph.py + context.py handle everything.
"""

import os
from typing import Any, Callable, Dict, Optional

from loguru import logger

from .context import InsightContext
from .graph import build_insight_graph
from .llms import LLMClient
from app.config import Settings, settings


PLATFORM_CONTENT_TABLES = (
    "douyin_aweme",
    "douyin_aweme_comment",
    "xhs_note",
    "xhs_note_comment",
    "weibo_note",
    "weibo_note_comment",
    "bilibili_video",
    "bilibili_video_comment",
    "kuaishou_video",
    "kuaishou_video_comment",
    "zhihu_content",
    "zhihu_comment",
    "tieba_note",
    "tieba_comment",
)


def _collect_platform_table_counts(ctx: InsightContext) -> Dict[str, Optional[int]]:
    """Return row counts for the local MediaCrawler tables used by Insight."""
    existing_tables = ctx.search_agency._get_existing_tables()

    counts: Dict[str, Optional[int]] = {}
    for table in PLATFORM_CONTENT_TABLES:
        if table.lower() not in existing_tables:
            counts[table] = None
            continue

        count_rows = ctx.search_agency._execute_query(f"SELECT COUNT(*) AS cnt FROM `{table}`")
        counts[table] = int(count_rows[0].get("cnt", 0)) if count_rows else 0
    return counts


def _format_platform_table_counts(counts: Dict[str, Optional[int]]) -> str:
    parts = []
    for table, count in counts.items():
        value = "表不存在" if count is None else str(count)
        parts.append(f"{table}={value}")
    return "，".join(parts)


# ── Module-level research function ────────────────────────────────────────

def run_research(
    query: str,
    config: Settings,
    llm_client: LLMClient,
    progress_callback: Optional[Callable] = None,
    save_report: bool = True,
) -> Dict[str, Any]:
    """Execute deep research, return dict with final_report and paragraphs."""
    logger.info(f"\n{'=' * 60}\n开始深度研究: {query}\n{'=' * 60}")

    ctx = InsightContext(
        llm_client=llm_client,
        config=config,
        progress_callback=progress_callback,
    )

    # 快速检查本地舆情数据库是否有数据
    try:
        probe = ctx.execute_search(
            "search_hot_content",
            query,
            time_period="year",
            limit=1,
            enable_sentiment=False,
        )
        if not probe.results:
            table_counts = _collect_platform_table_counts(ctx)
            table_counts_text = _format_platform_table_counts(table_counts)
            raise RuntimeError(
                "本地舆情数据库暂无可分析数据：平台表存在但内容记录为空。\n"
                f"当前平台表行数: {table_counts_text}\n"
                "原因: InsightEngine 只分析本地 MySQL 中由 MediaCrawler 写入的数据，"
                "LLM key 只负责后续总结，不能替代本地舆情数据。\n"
                "请先在宿主机运行真实爬虫，例如:\n"
                "cd E:\\SentinelAI-MultiAgent\\tools\\SentinelSpider\\DeepSentimentCrawling\\MediaCrawler\n"
                "E:\\SentinelAI-MultiAgent\\.venv-spider\\Scripts\\python.exe main.py "
                "--platform dy --lt qrcode --type search --keywords \"西藏旅游宣传\" "
                "--headless no --save_data_option db"
            )
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning(f"数据库连通性检查失败: {e}")
        raise RuntimeError("无法连接到本地舆情数据库，请检查数据库配置（DB_HOST/DB_PORT/DB_USER/DB_NAME）。") from e

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    try:
        graph = build_insight_graph(ctx)
        initial_state = {
            "query": query,
            "save_report": save_report,
            "max_reflections": config.MAX_REFLECTIONS,
        }
        result = graph.invoke(initial_state, {"recursion_limit": 100})
        logger.info("深度研究完成！")
        return {
            "final_report": result.get("final_report", ""),
            "report_title": result.get("report_title", ""),
            "is_completed": result.get("is_completed", False),
            "paragraphs": result.get("paragraphs", []),
        }
    except Exception as e:
        logger.exception(f"研究过程中发生错误: {str(e)}")
        raise
