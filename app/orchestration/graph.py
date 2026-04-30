"""
顶层协调 LangGraph 图定义。

通过 build_orchestration_graph() 构建 StateGraph，
将"输入检查 → 文件加载 → 报告生成 → 结果提取"串联为
可追踪的有向图，替代 flask_interface.run_report_generation()
中的顺序逻辑。
"""

from __future__ import annotations

from typing import Any, Dict

from langgraph.graph import END, START, StateGraph
from loguru import logger

from .graph_state import OrchestrationState


# ────────────────────────────────────────────────────────────────
# 条件边
# ────────────────────────────────────────────────────────────────

def _is_input_ready(state: OrchestrationState) -> str:
    """根据 check_result 判断是否继续生成。"""
    if state.get("input_ready", False):
        return "ready"
    return "not_ready"


# ────────────────────────────────────────────────────────────────
# 工厂函数
# ────────────────────────────────────────────────────────────────

def build_orchestration_graph(report_agent, check_engines_fn) -> Any:
    """
    构建顶层协调图。

    Args:
        report_agent: 已初始化的 ReportAgent 实例。
        check_engines_fn: 检查三引擎就绪状态的回调（即 check_engines_ready）。

    Returns:
        编译后的 CompiledStateGraph。
    """

    # ── 辅助：事件分发 ──────────────────────────────────────────
    def _make_emit(state: OrchestrationState):
        handler = state.get("stream_handler")

        def emit(event_type: str, payload: Dict[str, Any]):
            if not handler:
                return
            try:
                handler(event_type, payload)
            except Exception as e:
                logger.warning(f"协调层流式回调失败: {e}")

        return emit

    # ── 节点适配器 ──────────────────────────────────────────────

    def node_check_readiness(state: OrchestrationState) -> dict:
        """检查三引擎输出与论坛日志是否就绪。"""
        emit = _make_emit(state)
        emit("stage", {"message": "正在检查输入文件", "stage": "check_readiness"})

        check_result = check_engines_fn()
        ready = check_result.get("ready", False)

        if not ready:
            missing = check_result.get("missing_files", [])
            logger.warning(f"输入文件未就绪: {missing}")
            emit("stage", {"stage": "input_not_ready", "missing": missing})
        else:
            emit("stage", {
                "stage": "input_ready",
                "files": check_result.get("latest_files", {}),
            })

        return {
            "check_result": check_result,
            "input_ready": ready,
            "status": "running" if ready else "error",
            "error": None if ready else f"输入文件未准备就绪: {check_result.get('missing_files', [])}",
        }

    def node_input_not_ready(state: OrchestrationState) -> dict:
        """输入未就绪时的终止节点。"""
        return {}

    def node_load_inputs(state: OrchestrationState) -> dict:
        """加载三引擎报告与论坛日志。"""
        emit = _make_emit(state)
        emit("stage", {"message": "输入文件检查通过，准备载入内容", "stage": "io_ready"})

        check_result = state["check_result"]
        latest_files = check_result.get("latest_files", {})
        content = report_agent.load_input_files(latest_files)

        emit("stage", {"message": "源数据加载完成，启动生成流程", "stage": "data_loaded"})
        return {"content": content}

    def node_generate_report(state: OrchestrationState) -> dict:
        """调用 ReportAgent.generate_report() 生成最终报告。"""
        emit = _make_emit(state)
        query = state.get("query", "智能舆情分析报告")
        custom_template = state.get("custom_template", "")
        content = state["content"]
        task_id = state.get("task_id", "")
        stream_handler = state.get("stream_handler")

        emit("stage", {
            "message": "正在调用ReportAgent生成报告",
            "stage": "agent_running",
        })

        generation_result = report_agent.generate_report(
            query=query,
            reports=content.get("reports", []),
            forum_logs=content.get("forum_logs", ""),
            custom_template=custom_template,
            save_report=True,
            stream_handler=stream_handler,
            report_id=task_id,
        )

        return {"generation_result": generation_result}

    def node_extract_results(state: OrchestrationState) -> dict:
        """从生成结果中提取文件路径与 HTML 内容。"""
        result = state.get("generation_result", {})
        if not isinstance(result, dict):
            return {"html_content": str(result), "status": "completed"}

        return {
            "html_content": result.get("html_content", ""),
            "report_file_path": result.get("report_filepath", ""),
            "report_file_relative_path": result.get("report_relative_path", ""),
            "report_file_name": result.get("report_filename", ""),
            "state_file_path": result.get("state_filepath", ""),
            "ir_file_path": result.get("ir_filepath", ""),
            "status": "completed",
        }

    # ── 构建 StateGraph ────────────────────────────────────────

    graph = StateGraph(OrchestrationState)

    graph.add_node("check_readiness", node_check_readiness)
    graph.add_node("input_not_ready", node_input_not_ready)
    graph.add_node("load_inputs", node_load_inputs)
    graph.add_node("generate_report", node_generate_report)
    graph.add_node("extract_results", node_extract_results)

    # 入口
    graph.add_edge(START, "check_readiness")

    # 条件分支：就绪 → 加载，否则 → 终止
    graph.add_conditional_edges(
        "check_readiness",
        _is_input_ready,
        {
            "ready": "load_inputs",
            "not_ready": "input_not_ready",
        },
    )

    graph.add_edge("input_not_ready", END)
    graph.add_edge("load_inputs", "generate_report")
    graph.add_edge("generate_report", "extract_results")
    graph.add_edge("extract_results", END)

    return graph.compile()
