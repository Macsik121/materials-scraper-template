from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page

from core.handlers.assignment import AssignmentPayload
from core.handlers.composite_html import CompositePagePayload
from core.handlers.test_readonly import TestReviewPayload
from core.models import Course, Item


def scroll_to_bottom(
    page: Page,
    container_selector: str | None = None,
    idle_sec: float = 2.0,
    max_scrolls: int = 100,
) -> int:
    """
    Общая утилита прокрутки для SPA с бесконечным скроллом / ленивой подгрузкой (SPEC §3).
    Останавливается, когда высота контейнера перестаёт расти в течение idle_sec
    или достигнут лимит max_scrolls. Возвращает число выполненных шагов прокрутки.
    """
    get_height_js = """(sel) => {
        const el = sel ? document.querySelector(sel) : (document.scrollingElement || document.documentElement);
        return el ? el.scrollHeight : 0;
    }"""
    scroll_js = """(sel) => {
        const el = sel ? document.querySelector(sel) : (document.scrollingElement || document.documentElement);
        if (!el) return;
        el.scrollTop = el.scrollHeight;
        if (!sel) window.scrollTo(0, document.body.scrollHeight);
    }"""

    last_height = int(page.evaluate(get_height_js, container_selector) or 0)
    last_growth_ts = time.monotonic()
    steps = 0

    while steps < max_scrolls:
        page.evaluate(scroll_js, container_selector)
        steps += 1
        page.wait_for_timeout(350)
        new_height = int(page.evaluate(get_height_js, container_selector) or 0)
        if new_height > last_height:
            last_height = new_height
            last_growth_ts = time.monotonic()
        elif time.monotonic() - last_growth_ts >= idle_sec:
            break

    return steps


class BaseAdapter(ABC):
    """
    Абстрактный контракт LMS-адаптера (SPEC §1, §3, §8).
    Адаптер отвечает ТОЛЬКО за разбор DOM/сети и возврат нормализованных моделей;
    загрузка, проверка magic bytes и сохранение файлов выполняются в core/.
    """

    platform_name: str = "generic"
    extra_hide_selectors: Sequence[str] = ()

    @abstractmethod
    def match_page(self, page: Page) -> bool:
        """Возвращает True, если открытая вкладка соответствует целевой LMS-площадке."""

    @abstractmethod
    def discover_course(self, page: Page) -> Course:
        """
        Разбирает оглавление курса и возвращает нормализованное дерево Course -> Module -> Item.
        Для свёрнутых блоков (display: none) рекомендуется читать textContent, а не innerText.
        """

    def resolve_item_url(self, page: Page, item: Item) -> str:
        """
        Отложенное разрешение прямого URL файла/видео (Lazy URL Resolution, SPEC §4.1).
        Переопределяется для LMS с краткоживущими Signed URLs (S3/CloudFront/Kinescope)
        или прокладками redirect.asp / popup.
        """
        return item.source_url

    def get_content_frame(self, page: Page) -> Page | Frame:
        """Возвращает целевой Page или вложенный Frame с контентом урока."""
        return page

    def extract_html_content(self, page: Page, item: Item) -> CompositePagePayload:
        """Извлекает Markdown, списки локальных картинок и встроенных видео со страницы урока."""
        raise NotImplementedError(f"Адаптер '{self.platform_name}' не реализует extract_html_content()")

    def extract_assignment(self, page: Page, item: Item) -> AssignmentPayload:
        """Извлекает условие практического задания и список вложений (строго read-only)."""
        raise NotImplementedError(f"Адаптер '{self.platform_name}' не реализует extract_assignment()")

    def extract_test_review(self, page: Page, item: Item) -> TestReviewPayload:
        """Извлекает разбор ЗАВЕРШЁННОГО теста (строго read-only, без кликов по деструктивным кнопкам)."""
        raise NotImplementedError(f"Адаптер '{self.platform_name}' не реализует extract_test_review()")
