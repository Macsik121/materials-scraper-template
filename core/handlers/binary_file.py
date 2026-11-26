from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.downloader import CdpDownloader, extract_pdfjs_file_url
from core.models import Item, ItemStatus


def handle_binary_file(
    item: Item,
    target_path: Path,
    downloader: CdpDownloader,
    *,
    url_resolver: Callable[[], str] | None = None,
    referer: str = "",
) -> tuple[ItemStatus, Path, int, str]:
    """
    Обработчик прямых документов и архивов (PDF, Office, ZIP/RAR/7z) — SPEC §4.2.
    Поддерживает извлечение URL из ?file= просмотрщика PDF.js и проверку magic bytes.
    """
    raw_url = item.source_url
    if not raw_url and url_resolver is not None:
        raw_url = url_resolver()

    if not raw_url:
        return ItemStatus.FAILED, target_path, 0, "Не удалось получить URL для скачивания документа"

    download_url = extract_pdfjs_file_url(raw_url)
    expected_ext = item.extension or target_path.suffix

    saved = downloader.download_file(
        download_url,
        target_path,
        expected_ext=expected_ext,
        referer=referer or item.lesson_url,
        url_resolver=url_resolver,
    )
    return ItemStatus.DONE, saved, saved.stat().st_size, ""
