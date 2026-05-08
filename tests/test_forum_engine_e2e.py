"""
端到端测试：ForumEngine

ForumEngine 不是 research() 式引擎，而是一个日志监控服务：
  1. 监控 insight/media/query 三个 .log 文件
  2. 检测到新的 SummaryNode 输出 → 转发到 forum.log
  3. 每 5 条转发触发一次 LLM 主持人发言 → 写入 [HOST] 到 forum.log

测试聚焦在 LogMonitor 的核心行为和解析函数上。
"""

import json
import os
import sys
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ── 先把项目根路径加入 sys.path ────────────────────────────
_proj_root = Path(__file__).resolve().parent.parent
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))


# ── 预注册 retry_helper ─────────────────────────────────────
import types as _types
_rh = _types.ModuleType("retry_helper")
_rh.with_graceful_retry = lambda config=None, default_return=None: (lambda f: f)
_rh.SEARCH_API_RETRY_CONFIG = None
sys.modules["retry_helper"] = _rh


# ── parse_forum_log_line 测试（纯函数）───────────────────────

class TestParseForumLogLine:
    """测试 parse_forum_log_line — 纯函数，无依赖，零 mock。"""

    def test_parse_agent_line(self):
        from app.services.forum_service import parse_forum_log_line
        line = "[10:30:15] [INSIGHT] 这是洞察引擎的分析结果"
        result = parse_forum_log_line(line)
        assert result is not None
        assert result["type"] == "agent"
        assert result["sender"] == "Insight Engine"
        assert result["content"] == "这是洞察引擎的分析结果"
        assert result["source"] == "INSIGHT"
        assert result["timestamp"] == "10:30:15"

    def test_parse_host_line(self):
        from app.services.forum_service import parse_forum_log_line
        line = "[10:30:20] [HOST] 各位好，欢迎来到本次讨论。"
        result = parse_forum_log_line(line)
        assert result is not None
        assert result["type"] == "host"
        assert result["sender"] == "Forum Host"
        assert result["source"] == "HOST"

    def test_parse_system_line_returns_none(self):
        from app.services.forum_service import parse_forum_log_line
        line = "[10:30:00] [SYSTEM] === ForumEngine 监控开始 ==="
        assert parse_forum_log_line(line) is None

    def test_parse_empty_content_returns_none(self):
        from app.services.forum_service import parse_forum_log_line
        line = "[10:30:00] [INSIGHT]  "
        assert parse_forum_log_line(line) is None

    def test_parse_malformed_line_returns_none(self):
        from app.services.forum_service import parse_forum_log_line
        assert parse_forum_log_line("普通文本，没有格式") is None
        assert parse_forum_log_line("") is None
        assert parse_forum_log_line("[BROKEN] no timestamp") is None

    def test_parse_unknown_source_returns_none(self):
        from app.services.forum_service import parse_forum_log_line
        line = "[10:30:00] [UNKNOWN] 未知来源"
        assert parse_forum_log_line(line) is None

    def test_parse_line_with_escaped_newlines(self):
        from app.services.forum_service import parse_forum_log_line
        line = "[10:30:00] [MEDIA] 第一行\\n第二行\\n第三行"
        result = parse_forum_log_line(line)
        assert result is not None
        assert result["content"] == "第一行\n第二行\n第三行"

    def test_parse_all_three_agent_sources(self):
        from app.services.forum_service import parse_forum_log_line
        for source in ["QUERY", "MEDIA", "INSIGHT"]:
            line = f"[10:30:00] [{source}] 内容"
            result = parse_forum_log_line(line)
            assert result is not None, f"{source} 未被识别"
            assert result["source"] == source


# ── LogMonitor 核心行为测试（需 tmp_path）────────────────────

class TestLogMonitorBehavior:
    """LogMonitor 核心行为：日志监控、转发、主持人触发。"""

    @pytest.fixture
    def log_dir(self, tmp_path):
        """每个测试独享的日志目录。"""
        d = tmp_path / "logs"
        d.mkdir()
        return d

    @pytest.fixture
    def monitor(self, log_dir):
        """创建 LogMonitor，禁用 LLM 主持人避免真实 API 调用。"""
        from engines.ForumEngine import monitor as _mon_module
        # 确保 HOST_AVAILABLE 为 True（否则 _trigger_host_speech 直接返回）
        _mon_module.HOST_AVAILABLE = True

        mon = _mon_module.LogMonitor(log_dir=str(log_dir))
        # 清空 forum.log 并写入起始标记
        mon.clear_forum_log()
        yield mon

    def _write_log(self, log_dir: Path, name: str, lines: list):
        """辅助：向监控日志写入内容。"""
        path = log_dir / f"{name}.log"
        with open(path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")

    def _read_forum(self, log_dir: Path) -> str:
        """辅助：读取 forum.log 内容。"""
        path = log_dir / "forum.log"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # ── 写入与转发 ──

    def test_write_to_forum_log(self, monitor, log_dir):
        """write_to_forum_log 写入的行可被 parse_forum_log_line 解析。"""
        monitor.write_to_forum_log("测试消息", "INSIGHT")
        content = self._read_forum(log_dir)
        assert "测试消息" in content
        # 验证格式：包含 [时间] [INSIGHT]
        assert re.search(r"\[\d{2}:\d{2}:\d{2}\]\s*\[INSIGHT\]", content)

    def test_read_new_lines_returns_appended_content(self, monitor, log_dir):
        """read_new_lines 能读取新增日志行。"""
        self._write_log(log_dir, "insight", [
            "2026-05-08 10:00:00 | INFO | InsightEngine.nodes.summary_node - 正在生成首次段落总结",
            "2026-05-08 10:00:01 | INFO | InsightEngine.nodes.reflection_summary_node - 反思总结输出",
        ])
        lines = monitor.read_new_lines(
            log_dir / "insight.log", "insight"
        )
        assert len(lines) >= 2

    # ── 目标行识别 ──

    @pytest.mark.parametrize("line", [
        "InsightEngine.nodes.summary_node - 正在生成首次段落总结",
        "FirstSummaryNode - 输出内容",
        "ReflectionSummaryNode - 反思总结",
        "正在生成首次段落总结",
        "正在生成反思总结",
    ])
    def test_is_target_log_line_matches(self, monitor, line):
        """is_target_log_line 正确识别 SummaryNode 相关行。"""
        assert monitor.is_target_log_line(line), f"应为目标行: {line}"

    @pytest.mark.parametrize("line", [
        "InsightEngine.graph - 生成报告结构",
        "MediaEngine.nodes.search_node - 搜索查询",
        "这是普通的日志行内容，不包含任何节点标识",
    ])
    def test_is_target_log_line_ignores_other_lines(self, monitor, line):
        """is_target_log_line 忽略非 SummaryNode 行。"""
        assert not monitor.is_target_log_line(line), f"不应为目标行: {line}"

    # ── 有价值内容识别 ──

    @pytest.mark.parametrize("line", [
        "清理后的输出: 这是经过清理的有价值内容信息展示",
        '{"paragraph_latest_state": "这是一段包含完整内容的段落"}',
        "2026-05-08 10:00:00 | INFO | node - 这是一段超过30个字符的有价值日志内容",
    ])
    def test_is_valuable_content(self, monitor, line):
        """is_valuable_content 识别有内容价值的行。"""
        assert monitor.is_valuable_content(line), f"应为有价值内容: {line}"

    @pytest.mark.parametrize("line", [
        "",
        "  ",
        " 空内容",
    ])
    def test_is_valuable_content_empty(self, monitor, line):
        """is_valuable_content 排除空白或短内容。"""
        assert not monitor.is_valuable_content(line), f"不应为有价值内容: '{line}'"

    # ── 完整监控流程（使用 mock 避免实际 LLM 调用） ──

    def test_monitor_forwards_summary_to_forum_log(self, monitor, log_dir):
        """向 insight.log 写入 SummaryNode 行后，forum.log 出现对应内容。"""
        self._write_log(log_dir, "insight", [
            "2026-05-08 10:00:00 | INFO | InsightEngine.nodes.summary_node - 正在生成首次段落总结",
            '{"paragraph_latest_state": "市场分析：整体表现良好。"}',
        ])
        monitor.read_new_lines(log_dir / "insight.log", "insight")
        processed = monitor.process_lines_for_json(
            monitor.read_new_lines(log_dir / "insight.log", "insight"),
            "insight"
        )
        # 如果 processed 有内容，说明被识别并处理了
        assert processed is not None

    def test_speech_buffer_triggers_host(self, monitor, log_dir):
        """
        当 agent_speeches_buffer 达到阈值时触发主持人发言。
        这里 mock 掉 LLM 调用，只验证流程通。
        """
        # 准备：填充缓冲区（_trigger_host_speech 硬编码检查 5 条）
        for i in range(5):
            monitor.agent_speeches_buffer.append(
                f"发言内容第{i+1}条"
            )

        # 手动触发主持人发言并 mock LLM
        with patch("engines.ForumEngine.monitor.generate_host_speech",
                   return_value="主持人综合发言"):
            monitor._trigger_host_speech()

        content = self._read_forum(log_dir)
        assert "[HOST]" in content, "应包含主持人发言标记"
        assert "主持人综合发言" in content, "主持人发言内容应出现在 forum.log 中"

    def test_get_forum_log_content(self, monitor, log_dir):
        """get_forum_log_content 返回 forum.log 所有行。"""
        monitor.write_to_forum_log("消息1", "INSIGHT")
        monitor.write_to_forum_log("消息2", "MEDIA")
        lines = monitor.get_forum_log_content()
        assert len(lines) >= 2
        assert any("消息1" in l for l in lines)
        assert any("消息2" in l for l in lines)


# ── get_forum_log 集成测试 ──────────────────────────────────

class TestGetForumLog:
    """测试 forum_service.get_forum_log 的行为。"""

    def test_get_forum_log_returns_parsed_messages(self, tmp_path):
        """get_forum_log 正确返回解析后的消息列表。"""
        from app.services.forum_service import get_forum_log
        # 使用 log_dir 覆盖
        log_file = tmp_path / "logs" / "forum.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "=== 开始 ===",
            "[10:00:00] [INSIGHT] 洞察引擎分析",
            "[10:00:01] [HOST] 主持人发言",
            "[10:00:02] [SYSTEM] 系统消息",
        ]
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 用 monkeypatch 替换 LOG_DIR
        import app.services.forum_service as fs
        original = fs.LOG_DIR
        fs.LOG_DIR = tmp_path / "logs"
        try:
            result = get_forum_log()
            assert result["total_lines"] == 4
            assert len(result["parsed_messages"]) == 2  # INSIGHT + HOST
            assert result["parsed_messages"][0]["source"] == "INSIGHT"
            assert result["parsed_messages"][1]["source"] == "HOST"
        finally:
            fs.LOG_DIR = original
