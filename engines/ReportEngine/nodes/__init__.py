"""
LangGraph node classes for ReportEngine.
Business-logic nodes (TemplateSelectionNode, etc.) remain in their original modules.
"""

from .normalize_reports import NormalizeReportsNode
from .select_template import SelectTemplateNode
from .slice_template import SliceTemplateNode
from .design_layout import DesignLayoutNode
from .plan_budget import PlanBudgetNode
from .build_context import BuildContextNode
from .build_graphrag import BuildGraphRagNode
from .generate_chapters import GenerateChaptersNode
from .compose_document import ComposeDocumentNode
from .render_html import RenderHtmlNode
from .save_report import SaveReportNode

# Re-export business-logic node classes for backward compatibility
from .template_selection_node import TemplateSelectionNode
from .chapter_generation_node import ChapterGenerationNode, ChapterJsonParseError, ChapterContentError, ChapterValidationError
from .document_layout_node import DocumentLayoutNode
from .word_budget_node import WordBudgetNode
from .graphrag_query_node import GraphRAGQueryNode

__all__ = [
    "NormalizeReportsNode", "SelectTemplateNode", "SliceTemplateNode",
    "DesignLayoutNode", "PlanBudgetNode", "BuildContextNode",
    "BuildGraphRagNode", "GenerateChaptersNode", "ComposeDocumentNode",
    "RenderHtmlNode", "SaveReportNode",
    "TemplateSelectionNode", "ChapterGenerationNode", "ChapterJsonParseError",
    "ChapterContentError", "ChapterValidationError", "DocumentLayoutNode",
    "WordBudgetNode", "GraphRAGQueryNode",
]
