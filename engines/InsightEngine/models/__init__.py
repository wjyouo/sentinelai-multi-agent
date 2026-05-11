"""
Data model classes for serialization and interop.

- State: full report state dataclass
- Paragraph: single report paragraph
- Research: paragraph-level research tracking
- Search: individual search result
"""

from .state import State, Paragraph, Research, Search

__all__ = ["State", "Paragraph", "Research", "Search"]
