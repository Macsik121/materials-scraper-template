from __future__ import annotations

"""
Read-only разделение материалов курса по категориям (documents/, videos/, pages/).
Зависимости: ТОЛЬКО стандартная библиотека Python (SPEC §1.1, §5).
"""

import argparse
import shutil
import sys
from pathlib import Path

DOC_EXTS = frozenset({".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".zip", ".rar", ".7z", ".epub", ".txt"})
VIDEO_EXTS = frozenset({".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"})
PAGE_EXTS = frozenset({".md", ".html", ".htm", ".json", ".png", ".jpg", ".jpeg", ".webp", ".svg"})
IGNORED_FILES = frozenset({"manifest.json", "state.json"})


def _is_subpath(child: Path, parent: Path) -> bool:
    c = child.resolve()
    p = parent.resolve()
    return c == p or p in c.parents


def classify_file(rel_path: Path) -> str | None:
    ext = rel_path.suffix.lower()
    if ext in DOC_EXTS:
        return "documents"
    if ext in VIDEO_EXTS:
        return "videos"
    if ext in PAGE_EXTS:
        return "pages"
    return None


def split_course_materials(
    src_dir: Path,
    out_dir: Path,
    *,
    docs_only: bool = False,
    flat: bool = False,
) -> dict[str, int]:
    src = src_dir.resolve()
    out = out_dir.resolve()

    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Исходная папка не найдена: {src}")
    if _is_subpath(out, src):
        raise ValueError(
            f"Запрещено указывать --out ({out}) внутри --dir ({src}) во избежание рекурсивного копирования."
        )

    counts = {"documents": 0, "videos": 0, "pages": 0, "skipped": 0}

    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if "_tmp_downloads" in path.parts or path.name in IGNORED_FILES:
            continue
        if path.name.endswith((".tmp", ".part", ".crdownload")):
            continue

        rel = path.relative_to(src)
        category = classify_file(rel)
        if category is None:
            counts["skipped"] += 1
            continue
        if docs_only and category != "documents":
            counts["skipped"] += 1
            continue

        if flat:
            flat_name = "__".join(rel.parts)
            dest = out / category / flat_name
        else:
            dest = out / category / rel

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        counts[category] += 1

    return counts


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only копирование материалов курса в раздельные папки (documents/, videos/, pages/)."
    )
    parser.add_argument("--dir", type=Path, default=Path("downloaded_course"), help="Исходная папка курса")
    parser.add_argument("--out", type=Path, default=Path("sorted_course"), help="Целевая папка (вне --dir)")
    parser.add_argument("--docs-only", action="store_true", help="Копировать только документы (PDF, Office, архивы)")
    parser.add_argument("--flat", action="store_true", help="Плоская выкладка с префиксом модуля в имени файла")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        counts = split_course_materials(args.dir, args.out, docs_only=args.docs_only, flat=args.flat)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1

    print(f"Источник:   {args.dir.resolve()}")
    print(f"Назначение: {args.out.resolve()}")
    print(
        f"Скопировано -> documents: {counts['documents']}, videos: {counts['videos']}, "
        f"pages: {counts['pages']} (пропущено: {counts['skipped']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
