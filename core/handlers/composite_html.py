from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Frame, Page

from core.downloader import CdpDownloader
from core.fs_utils import sanitize_name
from core.handlers.base_report import save_hybrid_report
from core.handlers.video_stream import handle_video_item
from core.models import Item, ItemStatus, ItemType


@dataclass
class CompositePagePayload:
    """Нормализованные данные составной HTML-страницы, извлечённые адаптером."""

    markdown: str
    asset_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    extra_hide_selectors: list[str] = field(default_factory=list)


def _guess_ext_from_url(url: str, default_ext: str = ".bin") -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix and 2 <= len(suffix) <= 6:
        return suffix
    return default_ext


def handle_composite_html(
    item: Item,
    target_dir: Path,
    target_frame: Page | Frame,
    payload: CompositePagePayload,
    downloader: CdpDownloader,
    *,
    context: BrowserContext,
    extra_hide_selectors: Sequence[str] | None = None,
) -> tuple[ItemStatus, Path, int, str]:
    """
    Обработчик составной HTML-страницы (SPEC §4.2):
    1) скачивает изображения/вложения в assets/ и переписывает ссылки на локальные;
    2) скачивает встроенные видео в ту же папку урока через video_stream.py;
    3) сохраняет lesson.md + visual_report.png через base_report.py.
    """
    if not payload.markdown.strip():
        return ItemStatus.FAILED, target_dir, 0, "Пустой текст страницы (возможно, открылась страница логина)"

    target_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = target_dir / "assets"
    markdown_text = payload.markdown
    total_bytes = 0
    errors: list[str] = []

    for idx, asset_url in enumerate(payload.asset_urls, start=1):
        ext = _guess_ext_from_url(asset_url, ".png")
        filename = sanitize_name(f"asset_{idx:02d}{ext}")
        asset_path = assets_dir / filename
        try:
            saved = downloader.download_file(asset_url, asset_path, expected_ext=ext, referer=item.lesson_url)
            total_bytes += saved.stat().st_size
            markdown_text = markdown_text.replace(asset_url, f"assets/{saved.name}")
        except Exception as exc:
            errors.append(f"asset #{idx}: {exc}")

    for idx, video_url in enumerate(payload.video_urls, start=1):
        ext = _guess_ext_from_url(video_url, ".mp4")
        if ext in (".m3u8", ".mpd"):
            ext = ".mp4"
        vid_path = target_dir / f"video_{idx:02d}{ext}"
        sub_item = Item(
            item_id=f"{item.item_id}_vid_{idx}",
            order=idx,
            title=f"video_{idx:02d}",
            item_type=ItemType.VIDEO,
            source_url=video_url,
            lesson_url=item.lesson_url,
            extension=ext,
        )
        v_status, v_path, v_size, v_err = handle_video_item(
            sub_item,
            vid_path,
            downloader,
            context=context,
            referer=item.lesson_url,
        )
        if v_status == ItemStatus.DONE:
            total_bytes += v_size
            markdown_text += f"\n\n- Локальное видео {idx}: `{v_path.name}`\n"
        else:
            errors.append(f"video #{idx} ({v_status.value}): {v_err}")

    hide_selectors = list(payload.extra_hide_selectors)
    if extra_hide_selectors:
        hide_selectors.extend(extra_hide_selectors)

    report = save_hybrid_report(
        target=target_frame,
        target_dir=target_dir,
        markdown_content=markdown_text,
        markdown_filename="lesson.md",
        extra_hide_selectors=hide_selectors,
    )
    total_bytes += report.total_bytes

    if errors:
        return ItemStatus.FAILED, target_dir, total_bytes, "; ".join(errors)
    return ItemStatus.DONE, target_dir, total_bytes, ""
