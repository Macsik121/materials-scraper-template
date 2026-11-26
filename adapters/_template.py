from __future__ import annotations

"""
Шаблон (болванка) адаптера для новой LMS-площадки.

Как начать работу с новым курсом:
1. Скопируйте этот файл в adapters/<platform_name>.py (например, adapters/moodle.py).
2. Сохраните HTML/HAR срезы в web-page-reverse-engineering/ и очистите их через
   scripts/sanitize_capture.py перед анализом или передачей в LLM.
3. Реализуйте методы ниже, не добавляя сюда логику скачивания файлов —
   возвращайте только модели Course/Module/Item и payload-структуры.

ВАЖНО (Read-Only инвариант):
- Никогда не кликайте по кнопкам «Начать тест», «Сдать работу», «Сбросить попытку».
- Для свёрнутых модулей (display: none) читайте textContent вместо innerText.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page

from adapters.base import BaseAdapter, scroll_to_bottom
from core.handlers.assignment import AssignmentPayload
from core.handlers.composite_html import CompositePagePayload
from core.handlers.test_readonly import TestQuestion, TestReviewPayload
from core.models import Course, Item, ItemType, Module


class TemplateLmsAdapter(BaseAdapter):
    platform_name = "template"
    # Селекторы виджетов чата, боковых панелей и шапок, которые нужно скрыть на visual_report.png
    extra_hide_selectors = (".course-sidebar", ".support-chat-widget")

    def match_page(self, page: Page) -> bool:
        """TODO: замените проверку домена/пути на URL вашей учебной площадки."""
        url = (page.url or "").lower()
        return "lms.example.edu" in url

    def get_content_frame(self, page: Page) -> Page | Frame:
        """
        Если контент курса находится во вложенном iframe/frameset, найдите его здесь
        через core.browser.find_target_frame(page, name_or_id=...).
        """
        return page

    def discover_course(self, page: Page) -> Course:
        """
        TODO: реализуйте парсинг оглавления курса на основе HTML/HAR срезов.
        Если список модулей подгружается при прокрутке — вызовите scroll_to_bottom(page).
        """
        scroll_to_bottom(page, idle_sec=1.5, max_scrolls=20)
        title = (page.title() or "Untitled Course").strip()

        # Пример структуры возвращаемого дерева Course -> Module -> Item:
        sample_module = Module(
            module_id="m01",
            order=1,
            title="Модуль 1 (замените селекторы в адаптере)",
            items=[
                Item(
                    item_id="m01_i01",
                    order=1,
                    title="Пример конспекта",
                    item_type=ItemType.DOCUMENT,
                    source_url="",
                    lesson_url=page.url or "",
                    extension=".pdf",
                )
            ],
        )
        return Course(
            course_id="template_course",
            title=title,
            platform=self.platform_name,
            source_url=page.url or "",
            modules=[sample_module],
        )

    def resolve_item_url(self, page: Page, item: Item) -> str:
        """
        TODO: для LMS с краткоживущими Signed URLs (S3/CloudFront/Kinescope) откройте
        item.lesson_url и извлеките свежий URL непосредственно перед скачиванием.
        """
        return item.source_url

    def extract_html_content(self, page: Page, item: Item) -> CompositePagePayload:
        """TODO: извлеките очищенный текст урока, ссылки на <img> и встроенные <video>."""
        if item.lesson_url and page.url != item.lesson_url:
            page.goto(item.lesson_url, wait_until="domcontentloaded")
        text = page.locator("main, article, body").first.inner_text().strip()
        return CompositePagePayload(
            markdown=f"# {item.title}\n\n{text}\n",
            asset_urls=[],
            video_urls=[],
            extra_hide_selectors=list(self.extra_hide_selectors),
        )

    def extract_assignment(self, page: Page, item: Item) -> AssignmentPayload:
        """TODO: извлеките текст задания и список вложений (без кликов по кнопке сдачи)."""
        if item.lesson_url and page.url != item.lesson_url:
            page.goto(item.lesson_url, wait_until="domcontentloaded")
        text = page.locator("main, article, body").first.inner_text().strip()
        return AssignmentPayload(
            markdown=f"# Задание: {item.title}\n\n{text}\n",
            attachments=[],
            extra_hide_selectors=list(self.extra_hide_selectors),
        )

    def extract_test_review(self, page: Page, item: Item) -> TestReviewPayload:
        """
        TODO: извлеките вопросы/ответы ТОЛЬКО на странице разбора завершённой попытки.
        Никогда не нажимайте кнопки старта или отправки теста!
        """
        if item.lesson_url and page.url != item.lesson_url:
            page.goto(item.lesson_url, wait_until="domcontentloaded")
        return TestReviewPayload(
            title=item.title,
            expected_question_count=0,
            questions=[
                TestQuestion(
                    number=1,
                    prompt="TODO: реализуйте парсинг вопросов в вашем адаптере",
                )
            ],
            extra_hide_selectors=list(self.extra_hide_selectors),
        )
