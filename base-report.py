from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PRINT_STYLE_MARKER = "data-scraper-print"
DEFAULT_MAX_FULLPAGE_HEIGHT_PX = 15000

DEFAULT_HIDE_SELECTORS: tuple[str, ...] = (
    "header",
    "nav",
    "footer",
    ".sticky",
    ".fixed-top",
    ".fixed-bottom",
    "[role='banner']",
    "[role='navigation']",
    "#jivo-chat",
    ".jivo-btn",
    ".cookie-notice",
    ".cookie-banner",
    ".intercom-lightweight-app",
)

BASE_PRINT_CSS = """
html, body, #root, #app, main, article, .content-container, .workingplace {
    background-color: #ffffff !important;
    background-image: none !important;
    color: #000000 !important;
    overflow: visible !important;
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
}

pre, code, .scrollable-box, .code-block, [class*="scroll"] {
    overflow: visible !important;
    height: auto !important;
    max-height: none !important;
    white-space: pre-wrap !important;
    word-break: break-word !important;
}

/* Контраст для формул MathML / LaTeX / MathJax / KaTeX в тёмной теме */
math, mjx-container, .MathJax, .katex {
    background-color: #ffffff !important;
    color: #000000 !important;
    padding: 4px 6px !important;
    border-radius: 2px !important;
}
math svg, mjx-container svg, .MathJax svg, .katex svg {
    color: #000000 !important;
    fill: currentColor !important;
    stroke: currentColor !important;
}
"""


@dataclass
class ReportResult:
    target_dir: Path
    markdown_path: Path | None = None
    json_path: Path | None = None
    screenshots: list[Path] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        paths = [p for p in (self.markdown_path, self.json_path, *self.screenshots) if p and p.exists()]
        return sum(p.stat().st_size for p in paths)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding=encoding)
        os.replace(tmp_path, path)
        return path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def validate_png(path: Path) -> None:
    if not path.exists() or path.stat().st_size < len(PNG_SIGNATURE):
        raise RuntimeError(f"Скриншот пуст или не создан: {path.name}")
    with open(path, "rb") as f:
        if f.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            raise RuntimeError(f"Файл не имеет сигнатуры PNG: {path.name}")


def _build_print_css(extra_hide_selectors: Sequence[str] | None = None) -> str:
    hide_list = list(DEFAULT_HIDE_SELECTORS)
    if extra_hide_selectors:
        hide_list.extend(sel.strip() for sel in extra_hide_selectors if sel and sel.strip())

    hide_block = ",\n".join(hide_list) + " {\n    display: none !important;\n}\n"
    return hide_block + BASE_PRINT_CSS


def _resolve_page(target: Page | Frame) -> Page:
    return target if isinstance(target, Page) else target.page


def _is_nested_frame(target: Page | Frame) -> bool:
    return isinstance(target, Frame) and target.parent_frame is not None


@contextmanager
def clean_print_view(
    target: Page | Frame,
    extra_hide_selectors: Sequence[str] | None = None,
) -> Iterator[None]:
    """
    Временно инжектит CSS для печатного/светлого вида в целевой Page или Frame
    и гарантированно удаляет его в finally. Для вложенного iframe также временно
    разворачивает его высоту в родительском документе.
    """
    css_text = _build_print_css(extra_hide_selectors)
    prev_iframe_style: str | None = None
    iframe_el = None

    try:
        target.evaluate(
            """([marker, css]) => {
                document.querySelectorAll(`style[${marker}="1"]`).forEach(el => el.remove());
                const style = document.createElement("style");
                style.setAttribute(marker, "1");
                style.textContent = css;
                (document.head || document.documentElement).appendChild(style);
            }""",
            [PRINT_STYLE_MARKER, css_text],
        )

        # Если контент внутри iframe — разворачиваем высоту самого тега <iframe>, чтобы не обрезался
        if _is_nested_frame(target):
            try:
                iframe_el = target.frame_element()
                prev_iframe_style = iframe_el.get_attribute("style")
                content_height = target.evaluate(
                    "() => Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0)"
                )
                if content_height and content_height > 0:
                    iframe_el.evaluate(
                        "(el, h) => { el.style.height = h + 'px'; el.style.maxHeight = 'none'; }",
                        int(content_height) + 40,
                    )
            except PlaywrightError:
                iframe_el = None

        yield
    finally:
        if iframe_el is not None:
            try:
                if prev_iframe_style is None:
                    iframe_el.evaluate("el => el.removeAttribute('style')")
                else:
                    iframe_el.evaluate("(el, st) => el.setAttribute('style', st)", prev_iframe_style)
            except PlaywrightError:
                pass
        try:
            target.evaluate(
                """(marker) => {
                    document.querySelectorAll(`style[${marker}="1"]`).forEach(el => el.remove());
                }""",
                PRINT_STYLE_MARKER,
            )
        except PlaywrightError:
            pass


def _capture_chunked_page(
    page: Page,
    output_dir: Path,
    base_name: str,
    total_height: int,
    viewport_height: int,
) -> list[Path]:
    """Снимает сверхдлинную страницу (>15 000 px) поэкранными кадрами без обрезки GPU."""
    saved: list[Path] = []
    step = max(viewport_height - 40, 200)
    y = 0
    idx = 1

    try:
        while y < total_height:
            page.evaluate("(pos) => window.scrollTo(0, pos)", y)
            page.wait_for_timeout(120)

            out_path = output_dir / f"{base_name}_{idx:02d}.png"
            tmp_path = out_path.with_suffix(".tmp.png")
            try:
                page.screenshot(path=str(tmp_path), full_page=False, animations="disabled")
                validate_png(tmp_path)
                os.replace(tmp_path, out_path)
                saved.append(out_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

            if y + viewport_height >= total_height:
                break
            y += step
            idx += 1
    finally:
        try:
            page.evaluate("() => window.scrollTo(0, 0)")
        except PlaywrightError:
            pass

    return saved


def capture_visual_report(
    target: Page | Frame,
    output_dir: Path,
    base_name: str = "visual_report",
    max_height_px: int = DEFAULT_MAX_FULLPAGE_HEIGHT_PX,
) -> list[Path]:
    """
    Делает full-page скриншот (или серию кадров для сверхдлинных страниц)
    и проверяет сигнатуру PNG.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    page = _resolve_page(target)

    metrics = target.evaluate(
        """() => ({
            scrollHeight: Math.max(
                document.body ? document.body.scrollHeight : 0,
                document.documentElement ? document.documentElement.scrollHeight : 0
            ),
            viewportHeight: window.innerHeight || 900
        })"""
    )
    scroll_height = int(metrics["scrollHeight"] or 0)
    viewport_height = int(metrics["viewportHeight"] or 900)

    # Сверхдлинная страница: обходим лимит высоты Skia (~16 384 px)
    if not _is_nested_frame(target) and scroll_height > max_height_px:
        return _capture_chunked_page(page, output_dir, base_name, scroll_height, viewport_height)

    out_path = output_dir / f"{base_name}.png"
    tmp_path = out_path.with_suffix(".tmp.png")

    try:
        if _is_nested_frame(target):
            # Снимаем развёрнутое содержимое вложенного фрейма
            target.locator("body").screenshot(path=str(tmp_path), animations="disabled")
        else:
            page.screenshot(path=str(tmp_path), full_page=True, animations="disabled")

        validate_png(tmp_path)
        os.replace(tmp_path, out_path)
        return [out_path]
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def save_hybrid_report(
    target: Page | Frame,
    target_dir: Path,
    markdown_content: str,
    markdown_filename: str = "lesson.md",
    json_data: dict[str, Any] | list[Any] | None = None,
    json_filename: str | None = None,
    extra_hide_selectors: Sequence[str] | None = None,
    screenshot_base_name: str = "visual_report",
) -> ReportResult:
    """
    Унифицированный пайплайн гибридного отчёта:
      1. DOM-санитизация (clean_print_view)
      2. Сохранение Markdown и опционального JSON
      3. Full-page скриншот(ы) с валидацией PNG
      4. Гарантированный откат CSS
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    result = ReportResult(target_dir=target_dir)

    if markdown_content.strip():
        result.markdown_path = atomic_write_text(target_dir / markdown_filename, markdown_content)

    if json_data is not None and json_filename:
        serialized = json.dumps(json_data, ensure_ascii=False, indent=2)
        result.json_path = atomic_write_text(target_dir / json_filename, serialized)

    with clean_print_view(target, extra_hide_selectors=extra_hide_selectors):
        _resolve_page(target).wait_for_timeout(150)
        result.screenshots = capture_visual_report(
            target=target,
            output_dir=target_dir,
            base_name=screenshot_base_name,
        )

    return result

"""
Пример вызова в core/handlers/assignment.py

```python
from pathlib import Path
from core.handlers.base_report import save_hybrid_report

report = save_hybrid_report(
    target=course_frame,
    target_dir=Path("downloaded_course/M01_Введение/04_Task_СУБД"),
    markdown_content="# Задание: Разработка СУБД\n\n...",
    markdown_filename="task_info.md",
    extra_hide_selectors=[".course-sidebar", ".chat-widget"],
)
```
"""
