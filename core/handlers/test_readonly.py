from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page

from core.handlers.base_report import save_hybrid_report, validate_png
from core.models import Item, ItemStatus


DESTRUCTIVE_ACTION_RE = re.compile(
    r"(?:начать\s+тест|пройти\s+тест|сдать\s+(?:тест|работу)|отправить\s+на\s+проверку|"
    r"завершить\s+попытку|сбросить\s+попытку|start\s+(?:quiz|test|attempt)|submit\s+(?:quiz|attempt)|retake)",
    re.IGNORECASE,
)

FORMULA_LIGHT_STYLE_MARKER = "data-scraper-formula-light"
FORMULA_LIGHT_CSS = """
math, mjx-container, .MathJax, .katex, svg.formula {
    background: #ffffff !important;
    color: #000000 !important;
    padding: 10px !important;
}
math svg, mjx-container svg, .MathJax svg, .katex svg {
    fill: #000000 !important;
    stroke: #000000 !important;
}
"""


@dataclass
class TestQuestion:
    number: int
    prompt: str
    options: list[str] = field(default_factory=list)
    selected_answers: list[str] = field(default_factory=list)
    correct_answers: list[str] = field(default_factory=list)
    explanation: str = ""
    formula_selector: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "prompt": self.prompt,
            "options": list(self.options),
            "selected_answers": list(self.selected_answers),
            "correct_answers": list(self.correct_answers),
            "explanation": self.explanation,
        }


@dataclass
class TestReviewPayload:
    """Данные завершённого теста / разбора попытки (строго read-only, SPEC §4.2)."""

    title: str
    expected_question_count: int
    score_summary: str = ""
    questions: list[TestQuestion] = field(default_factory=list)
    extra_hide_selectors: list[str] = field(default_factory=list)


def is_destructive_control_text(text: str) -> bool:
    """Защитная проверка: запрещено кликать по контролам, меняющим состояние попытки."""
    return bool(DESTRUCTIVE_ACTION_RE.search(text or ""))


def render_test_markdown(payload: TestReviewPayload, formula_images: dict[int, str] | None = None) -> str:
    lines = [f"# Тест (разбор): {payload.title}", ""]
    if payload.score_summary:
        lines.extend([f"**Результат:** {payload.score_summary}", ""])
    lines.extend([f"**Вопросов:** {len(payload.questions)} / {payload.expected_question_count}", ""])

    images = formula_images or {}
    for q in payload.questions:
        lines.append(f"## Вопрос {q.number}")
        lines.append("")
        lines.append(q.prompt.strip() or "_Текст вопроса не извлечён_")
        lines.append("")
        if q.number in images:
            lines.append(f"![Формула вопроса {q.number}]({images[q.number]})")
            lines.append("")
        if q.options:
            lines.append("**Варианты:**")
            for opt in q.options:
                lines.append(f"- {opt}")
            lines.append("")
        if q.selected_answers:
            lines.append(f"**Выбрано:** {', '.join(q.selected_answers)}")
        if q.correct_answers:
            lines.append(f"**Верный ответ:** {', '.join(q.correct_answers)}")
        if q.explanation:
            lines.append(f"**Пояснение:** {q.explanation}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _capture_formula_screenshots(
    target_frame: Page | Frame,
    questions: list[TestQuestion],
    images_dir: Path,
) -> dict[int, str]:
    """Снимает MathML/LaTeX/SVG узлы с временным светлым CSS и гарантированным откатом (SPEC §4.2)."""
    selectors = [(q.number, q.formula_selector) for q in questions if q.formula_selector]
    if not selectors:
        return {}

    images_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[int, str] = {}

    try:
        target_frame.evaluate(
            """([marker, css]) => {
                const st = document.createElement("style");
                st.setAttribute(marker, "1");
                st.textContent = css;
                (document.head || document.documentElement).appendChild(st);
            }""",
            [FORMULA_LIGHT_STYLE_MARKER, FORMULA_LIGHT_CSS],
        )

        for q_num, selector in selectors:
            loc = target_frame.locator(selector).first
            if loc.count() == 0:
                continue
            out_path = images_dir / f"q{q_num:02d}_formula.png"
            tmp_path = out_path.with_suffix(".tmp.png")
            try:
                loc.screenshot(path=str(tmp_path), animations="disabled")
                validate_png(tmp_path)
                os.replace(tmp_path, out_path)
                saved[q_num] = f"images/{out_path.name}"
            except Exception:
                tmp_path.unlink(missing_ok=True)
    finally:
        try:
            target_frame.evaluate(
                "(marker) => document.querySelectorAll(`style[${marker}=\"1\"]`).forEach(el => el.remove())",
                FORMULA_LIGHT_STYLE_MARKER,
            )
        except Exception:
            pass

    return saved


def handle_test_readonly(
    item: Item,
    target_dir: Path,
    target_frame: Page | Frame,
    payload: TestReviewPayload,
    *,
    extra_hide_selectors: Sequence[str] | None = None,
) -> tuple[ItemStatus, Path, int, str]:
    """
    Строго read-only обработчик завершённых тестов и разборов попыток (SPEC §4.2).
    Сохраняет <stem>.json, <stem>.md, images/ (для формул) и visual_report.png.
    Проверяет совпадение числа извлечённых вопросов со счётчиком теста.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = target_dir.name

    formula_images = _capture_formula_screenshots(target_frame, payload.questions, target_dir / "images")
    markdown_text = render_test_markdown(payload, formula_images=formula_images)

    json_data: dict[str, Any] = {
        "item_id": item.item_id,
        "title": payload.title,
        "score_summary": payload.score_summary,
        "expected_question_count": payload.expected_question_count,
        "extracted_question_count": len(payload.questions),
        "questions": [q.to_dict() for q in payload.questions],
    }

    hide_selectors = list(payload.extra_hide_selectors)
    if extra_hide_selectors:
        hide_selectors.extend(extra_hide_selectors)

    report = save_hybrid_report(
        target=target_frame,
        target_dir=target_dir,
        markdown_content=markdown_text,
        markdown_filename=f"{stem}.md",
        json_data=json_data,
        json_filename=f"{stem}.json",
        extra_hide_selectors=hide_selectors,
    )

    if payload.expected_question_count > 0 and len(payload.questions) != payload.expected_question_count:
        return (
            ItemStatus.FAILED,
            target_dir,
            report.total_bytes,
            f"Несовпадение числа вопросов: извлечено {len(payload.questions)}, ожидалось {payload.expected_question_count}",
        )

    return ItemStatus.DONE, target_dir, report.total_bytes, ""
