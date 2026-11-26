from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page

from core.downloader import CdpDownloader
from core.fs_utils import sanitize_name
from core.handlers.base_report import save_hybrid_report
from core.models import Item, ItemStatus


@dataclass
class AssignmentPayload:
    """Данные страницы практического задания, извлечённые адаптером."""

    markdown: str
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (filename_or_title, url)
    extra_hide_selectors: list[str] = field(default_factory=list)


def handle_assignment(
    item: Item,
    target_dir: Path,
    target_frame: Page | Frame,
    payload: AssignmentPayload,
    downloader: CdpDownloader,
    *,
    extra_hide_selectors: Sequence[str] | None = None,
) -> tuple[ItemStatus, Path, int, str]:
    """
    Обработчик заданий (практика, лабораторные) поверх base_report.py (SPEC §4.2):
    сохраняет task.md, вложения в attachments/ и visual_report.png (строго read-only).
    """
    if not payload.markdown.strip():
        return ItemStatus.FAILED, target_dir, 0, "Пустое описание задания"

    target_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir = target_dir / "attachments"
    total_bytes = 0
    errors: list[str] = []

    saved_names: list[str] = []
    for idx, (label, att_url) in enumerate(payload.attachments, start=1):
        parsed_suffix = Path(urlparse(att_url).path).suffix
        base_name = sanitize_name(label or f"attachment_{idx:02d}")
        if parsed_suffix and not base_name.lower().endswith(parsed_suffix.lower()):
            base_name = f"{base_name}{parsed_suffix}"
        att_path = attachments_dir / f"{idx:02d}_{base_name}"
        try:
            saved = downloader.download_file(
                att_url,
                att_path,
                expected_ext=parsed_suffix,
                referer=item.lesson_url,
            )
            total_bytes += saved.stat().st_size
            saved_names.append(saved.name)
        except Exception as exc:
            errors.append(f"attachment #{idx}: {exc}")

    markdown_text = payload.markdown
    if saved_names:
        markdown_text += "\n\n## Вложения\n" + "\n".join(f"- `attachments/{n}`" for n in saved_names) + "\n"

    hide_selectors = list(payload.extra_hide_selectors)
    if extra_hide_selectors:
        hide_selectors.extend(extra_hide_selectors)

    report = save_hybrid_report(
        target=target_frame,
        target_dir=target_dir,
        markdown_content=markdown_text,
        markdown_filename="task.md",
        extra_hide_selectors=hide_selectors,
    )
    total_bytes += report.total_bytes

    if errors:
        return ItemStatus.FAILED, target_dir, total_bytes, "; ".join(errors)
    return ItemStatus.DONE, target_dir, total_bytes, ""
