"""
端到端测试：ReportEngine

Mock 掉调用 LLM 的节点方法（document_layout_node / word_budget_node /
chapter_generation_node），验证 generate_report() 的管道编排行为：
输入 query + reports → 输出 HTML。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── 路径 ─────────────────────────────────────────────────────
_proj_root = Path(__file__).resolve().parent.parent
for _p in [str(_proj_root), str(_proj_root / "engines")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── 模板 ──────────────────────────────────────────────────────
_CUSTOM_TEMPLATE = """# 第一章：市场概况
- 市场整体表现
- 主要数据指标

# 第二章：趋势分析
- 发展趋势
- 前景展望
"""

_MOCK_REPORTS = [
    "# QueryEngine 报告\n查询引擎分析结果。",
    "# MediaEngine 报告\n媒体引擎分析结果。",
    "# InsightEngine 报告\n洞察引擎分析结果。",
]

# ── 节点 mock 返回值 ─────────────────────────────────────────

def _make_layout():
    """返回符合 document_layout 输出契约的 dict。"""
    return {
        "title": "舆情分析报告",
        "subtitle": "综合数据洞察",
        "tagline": "基于多引擎数据的深度分析",
        "tocTitle": "目录",
        "hero": {
            "summary": "本报告综合分析了市场概况和趋势。",
            "highlights": ["市场整体表现良好", "新兴趋势值得关注"],
            "kpis": [{"label": "声量", "value": "125万", "tone": "up"}],
            "actions": ["持续关注市场动态"],
        },
        "tocPlan": [
            {
                "chapterId": "S1", "anchor": "chapter-1",
                "display": "一、市场概况",
                "description": "分析市场整体表现和数据指标",
            },
            {
                "chapterId": "S2", "anchor": "chapter-2",
                "display": "二、趋势分析",
                "description": "分析发展趋势和前景展望",
            },
        ],
        "layoutNotes": ["采用标准报告布局"],
    }


def _make_word_budget():
    """返回符合 word_budget 输出契约的 dict。"""
    return {
        "totalWords": 2000,
        "tolerance": 200,
        "chapters": [
            {
                "chapterId": "S1", "title": "市场概况", "targetWords": 1000,
                "sections": [
                    {"title": "市场整体表现", "anchor": "sec-1-1", "targetWords": 500, "minWords": 200, "maxWords": 600},
                    {"title": "主要数据指标", "anchor": "sec-1-2", "targetWords": 500, "minWords": 200, "maxWords": 600},
                ],
            },
            {
                "chapterId": "S2", "title": "趋势分析", "targetWords": 1000,
                "sections": [
                    {"title": "发展趋势", "anchor": "sec-2-1", "targetWords": 500, "minWords": 200, "maxWords": 600},
                    {"title": "前景展望", "anchor": "sec-2-2", "targetWords": 500, "minWords": 200, "maxWords": 600},
                ],
            },
        ],
    }


def _make_chapter(chapter_id: str, title: str) -> dict:
    """返回符合 chapter JSON 输出契约的 dict。"""
    return {
        "chapter_id": chapter_id,
        "title": title,
        "slug": f"chapter-{chapter_id}",
        "content": [
            {"type": "heading", "anchor": f"sec-{chapter_id}-1", "text": title},
            {"type": "paragraph", "inlines": [{"text": f"这是{title}的正文内容。"}]},
        ],
    }


# ── Fixture ───────────────────────────────────────────────────

@pytest.fixture
def agent():
    """创建 ReportAgent，mock 掉 3 个调用 LLM 的节点方法。"""
    import os
    os.environ["GRAPHRAG_ENABLED"] = "False"

    from ReportEngine.agent import create_agent
    instance = create_agent()

    # Mock 文档布局节点（原调用 LLM 生成）
    instance.document_layout_node.run = MagicMock(return_value=_make_layout())

    # Mock 字数规划节点
    instance.word_budget_node.run = MagicMock(return_value=_make_word_budget())

    # Mock 章节生成节点（返回模拟章节内容）
    chapter_responses = {
        "S1": _make_chapter("S1", "市场概况"),
        "S2": _make_chapter("S2", "趋势分析"),
    }

    def fake_chapter_run(section, context, run_dir, **kwargs):
        cid = getattr(section, "chapter_id", None)
        if cid and cid in chapter_responses:
            return chapter_responses[cid]
        return _make_chapter(cid or "S0", getattr(section, "title", "未知章节"))

    instance.chapter_generation_node.run = MagicMock(side_effect=fake_chapter_run)

    return instance


class TestReportEngineBehavior:
    """ReportEngine 行为级端到端测试，只验证外部契约。"""

    def test_generate_report_returns_html(self, agent):
        """generate_report() 返回含 html_content 的 dict，内容为合法 HTML。"""
        result = agent.generate_report(
            query="市场分析",
            reports=_MOCK_REPORTS,
            forum_logs="论坛讨论内容",
            custom_template=_CUSTOM_TEMPLATE,
            save_report=False,
        )
        assert isinstance(result, dict), f"返回值应为 dict，实际为 {type(result)}"
        html = result.get("html_content", "")
        assert html, "html_content 不应为空"
        assert isinstance(html, str)
        assert len(html) > 100, f"HTML 长度不足: {len(html)}"
        assert "<html" in html.lower() or "<!doctype" in html.lower(), "输出应包含 HTML 文档标记"

    def test_generate_report_with_chinese_query(self, agent):
        """中文查询正常产出 HTML。"""
        result = agent.generate_report(
            query="人工智能行业舆情分析",
            reports=_MOCK_REPORTS,
            forum_logs="",
            custom_template=_CUSTOM_TEMPLATE,
            save_report=False,
        )
        html = result.get("html_content", "")
        assert html and len(html) > 100

    def test_generate_report_save_creates_file(self, agent, tmp_path):
        """save_report=True 时 .html 文件写入磁盘。"""
        agent.config.OUTPUT_DIR = str(tmp_path)
        result = agent.generate_report(
            query="测试保存",
            reports=_MOCK_REPORTS,
            custom_template=_CUSTOM_TEMPLATE,
            save_report=True,
        )
        html_content = result.get("html_content", "")
        assert html_content
        html_files = [f for f in tmp_path.rglob("*.html")]
        assert len(html_files) > 0, f"tmp_path 中没有 .html 文件: {list(tmp_path.rglob('*'))}"
        saved = html_files[0].read_text(encoding="utf-8")
        assert saved == html_content, "文件内容应与返回的 html_content 一致"

    def test_empty_reports_still_generates_html(self, agent):
        """空 reports 列表仍产出 HTML，不崩溃。"""
        result = agent.generate_report(
            query="测试",
            reports=[],
            custom_template=_CUSTOM_TEMPLATE,
            save_report=False,
        )
        html = result.get("html_content", "")
        assert html and len(html) > 50

    def test_empty_forum_logs_still_generates_html(self, agent):
        """空 forum_logs 仍产出 HTML，不崩溃。"""
        result = agent.generate_report(
            query="测试",
            reports=_MOCK_REPORTS,
            forum_logs="",
            custom_template=_CUSTOM_TEMPLATE,
            save_report=False,
        )
        html = result.get("html_content", "")
        assert html and len(html) > 50

    def test_custom_template_affects_output(self, agent):
        """不同 custom_template 产生不同内容的 HTML。"""
        tmpl_a = "# 标题A\n- 内容A"
        tmpl_b = "# 标题B\n- 内容B"
        r1 = agent.generate_report(query="测试", reports=_MOCK_REPORTS,
                                    custom_template=tmpl_a, save_report=False)
        r2 = agent.generate_report(query="测试", reports=_MOCK_REPORTS,
                                    custom_template=tmpl_b, save_report=False)
        assert r1["html_content"] != r2["html_content"], "不同模板应产生不同 HTML"
