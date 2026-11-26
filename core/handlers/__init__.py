"""Обработчики форматов учебных материалов (бинарные файлы, видео, HTML, задания, тесты)."""

from core.handlers.base_report import ReportResult, clean_print_view, save_hybrid_report

__all__ = [
    "ReportResult",
    "clean_print_view",
    "save_hybrid_report",
]
