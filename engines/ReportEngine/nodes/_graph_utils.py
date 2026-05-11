"""Conditional edge functions for ReportEngine graph."""

from ..state import ReportGraphState


def should_build_graphrag(state: ReportGraphState) -> str:
    return "build_graphrag" if state.get("graphrag_enabled", False) else "skip_graphrag"
