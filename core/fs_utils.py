from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from core.models import ItemType

WINDOWS_MAX_PATH = 260
FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")

RESERVED_WINDOWS_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

ITEM_TYPE_PREFIX: dict[ItemType, str] = {
    ItemType.DOCUMENT: "Doc",
    ItemType.VIDEO: "Vid",
    ItemType.VIDEO_STREAM: "Vid",
    ItemType.HTML_PAGE: "Page",
    ItemType.ASSIGNMENT: "Task",
    ItemType.TEST_READONLY: "Test",
    ItemType.EXTERNAL_LINK: "Link",
}


class PathTooLongError(ValueError):
    """Базовый путь настолько длинный, что невозможно уложить имя файла в MAX_PATH=260."""


def sanitize_name(raw: str, fallback: str = "untitled", max_len: int | None = None) -> str:
    """
    Санитизирует имя файла или папки для полной совместимости с Windows/Linux:
    - удаляет запрещённые символы (<>:"/\\|?*) и управляющие символы;
    - схлопывает пробелы и удаляет завершающие точки/пробелы;
    - защищает от зарезервированных имён Windows (CON, PRN, AUX, NUL, COM1-9, LPT1-9).
    """
    cleaned = FORBIDDEN_CHARS_RE.sub("_", raw or "")
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback

    stem_upper = cleaned.split(".")[0].upper()
    if stem_upper in RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"

    if max_len is not None and max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
        if not cleaned:
            cleaned = fallback[:max_len]

    return cleaned


def zero_pad(index: int, total: int) -> str:
    """Возвращает индекс с ведущими нулями: 02d до 99 элементов, 03d при >= 100."""
    width = 3 if total >= 100 else 2
    return f"{index:0{width}d}"


def format_module_dirname(order: int, title: str, total_modules: int = 10, max_title_len: int | None = None) -> str:
    prefix = f"M{zero_pad(order, total_modules)}_"
    safe = sanitize_name(title, fallback=f"Module_{order}", max_len=max_title_len)
    return f"{prefix}{safe}"


def format_item_name(
    order: int,
    item_type: ItemType,
    title: str,
    extension: str = "",
    total_items: int = 10,
    max_title_len: int | None = None,
) -> str:
    prefix = f"{zero_pad(order, total_items)}_{ITEM_TYPE_PREFIX[item_type]}_"
    ext = extension.strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    ext = FORBIDDEN_CHARS_RE.sub("", ext)
    safe = sanitize_name(title, fallback=f"Item_{order}", max_len=max_title_len)
    return f"{prefix}{safe}{ext}"


def build_item_path(
    base_dir: Path,
    module_order: int,
    module_title: str,
    item_order: int,
    item_type: ItemType,
    item_title: str,
    extension: str = "",
    total_modules: int = 10,
    total_items: int = 10,
    max_path: int = WINDOWS_MAX_PATH,
    nested_artifact_reserve: int = 0,
) -> Path:
    """
    Строит детерминированный путь <base_dir>/M01_<Module>/<01>_<Type>_<Item><ext>,
    динамически укорачивая названия модуля и элемента, чтобы гарантированно
    уложиться в лимит Windows MAX_PATH (260).
    """
    mod_prefix = f"M{zero_pad(module_order, total_modules)}_"
    item_prefix = f"{zero_pad(item_order, total_items)}_{ITEM_TYPE_PREFIX[item_type]}_"
    ext = extension.strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    ext = FORBIDDEN_CHARS_RE.sub("", ext)

    base_str = str(base_dir).rstrip("/\\")
    # Учитываем разделители + префиксы + расширение + резерв для вложенных файлов (например, /visual_report.png)
    fixed_len = len(base_str) + 1 + len(mod_prefix) + 1 + len(item_prefix) + len(ext) + nested_artifact_reserve
    available_for_titles = max_path - fixed_len

    if available_for_titles < 2:
        raise PathTooLongError(
            f"Базовая папка и системные префиксы уже занимают {fixed_len} симв. "
            f"(лимит MAX_PATH={max_path}). Перенесите --dir ближе к корню диска."
        )

    safe_mod = sanitize_name(module_title, fallback=f"Module_{module_order}")
    safe_item = sanitize_name(item_title, fallback=f"Item_{item_order}")

    if len(safe_mod) + len(safe_item) > available_for_titles:
        mod_budget = max(1, min(len(safe_mod), available_for_titles // 2))
        item_budget = max(1, available_for_titles - mod_budget)
        if len(safe_item) < item_budget:
            mod_budget = min(len(safe_mod), available_for_titles - len(safe_item))
            item_budget = len(safe_item)
        safe_mod = sanitize_name(safe_mod, fallback="M", max_len=mod_budget)
        safe_item = sanitize_name(safe_item, fallback="I", max_len=item_budget)

    return base_dir / f"{mod_prefix}{safe_mod}" / f"{item_prefix}{safe_item}{ext}"


def is_subpath(child: Path, parent: Path) -> bool:
    """Возвращает True, если child совпадает с parent или лежит внутри него (с разрешением .. и symlink)."""
    try:
        child_resolved = child.resolve()
        parent_resolved = parent.resolve()
        return child_resolved == parent_resolved or parent_resolved in child_resolved.parents
    except OSError:
        return False


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
        return path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, content.encode(encoding))


def atomic_write_json(path: Path, payload: Any) -> Path:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, serialized, encoding="utf-8")


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"
