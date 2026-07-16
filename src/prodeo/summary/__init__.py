"""Daily summaries: a scheduled digest of fleet activity (Phase 3)."""

from prodeo.summary.interface import Summarizer
from prodeo.summary.service import SummaryService

__all__ = ["Summarizer", "SummaryService"]
