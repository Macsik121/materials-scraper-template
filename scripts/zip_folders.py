from __future__ import annotations

"""
Автономный Zip64-архиватор любых директорий с умным выбором сжатия.
Зависимости: ТОЛЬКО стандартная библиотека Python (SPEC §1.1, §5).
"""

import argparse
import os
import sys
import zipfile
from pathlib import Path

ALREADY_COMPRESSED_EXTS = frozenset(
    {
        ".mp4",
        ".webm",
        ".mkv",
        ".mov",
        ".avi",
        ".m4v",
        ".mp3",
        ".aac",
        ".m4a",
        ".ogg",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".zip",
        ".rar",
        ".7z",
        ".gz",
        ".xz",
        ".bz2",
        ".docx",
        ".xlsx",
        ".pptx",
        ".epub",
    }
)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def zip_single_folder(
    folder: Path,
    out_dir: Path,
    *,
    no_root_folder: bool = False,
    store_only: bool = False,
    level: int = 6,
    overwrite: bool = False,
) -> tuple[str, Path, int, int]:
    src = folder.resolve()
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Папка не найдена: {src}")

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{src.name}.zip"
    if zip_path.exists() and not overwrite:
        return "skipped", zip_path, 0, zip_path.stat().st_size

    tmp_zip = zip_path.with_suffix(".tmp.zip")
    files_added = 0

    try:
        with zipfile.ZipFile(tmp_zip, mode="w", allowZip64=True) as zf:
            for item in sorted(src.rglob("*")):
                if not item.is_file():
                    continue
                if "_tmp_downloads" in item.parts or item.name.endswith((".tmp", ".part", ".crdownload")):
                    continue

                rel = item.relative_to(src)
                arcname = str(rel) if no_root_folder else str(Path(src.name) / rel)

                if store_only or item.suffix.lower() in ALREADY_COMPRESSED_EXTS:
                    zf.write(item, arcname=arcname, compress_type=zipfile.ZIP_STORED)
                else:
                    zf.write(item, arcname=arcname, compress_type=zipfile.ZIP_DEFLATED, compresslevel=level)
                files_added += 1

        os.replace(tmp_zip, zip_path)
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        raise

    return "ok", zip_path, files_added, zip_path.stat().st_size


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Автономный Zip64-архиватор папок с умным сжатием (только стандартная библиотека Python)."
    )
    parser.add_argument("--folders", nargs="*", type=Path, default=[], help="Список папок для упаковки")
    parser.add_argument("--subdirs-of", type=Path, default=None, help="Упаковать каждую подпапку указанной директории")
    parser.add_argument("--out", type=Path, default=Path("archives"), help="Папка назначения для .zip архивов")
    parser.add_argument("--no-root-folder", action="store_true", help="Не добавлять имя корневой папки внутри архива")
    parser.add_argument("--store-only", action="store_true", help="Упаковать без сжатия (ZIP_STORED для всех файлов)")
    parser.add_argument("--level", type=int, default=6, choices=range(1, 10), help="Уровень сжатия DEFLATE (1–9)")
    parser.add_argument("--overwrite", action="store_true", help="Перезаписывать существующие .zip архивы")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    targets: list[Path] = [p for p in args.folders]

    if args.subdirs_of is not None:
        parent = args.subdirs_of.resolve()
        if not parent.exists() or not parent.is_dir():
            print(f"ОШИБКА: папка --subdirs-of не найдена: {parent}", file=sys.stderr)
            return 1
        targets.extend(sorted(p for p in parent.iterdir() if p.is_dir() and p.name != "_tmp_downloads"))

    if not targets:
        print("ОШИБКА: укажите хотя бы одну папку через --folders или --subdirs-of.", file=sys.stderr)
        return 1

    out_dir = args.out.resolve()
    created = 0
    skipped = 0
    failed: list[tuple[Path, str]] = []

    for folder in targets:
        try:
            status, zip_path, count, size = zip_single_folder(
                folder,
                out_dir,
                no_root_folder=args.no_root_folder,
                store_only=args.store_only,
                level=args.level,
                overwrite=args.overwrite,
            )
            if status == "ok":
                created += 1
                print(f"OK -> {zip_path.name} ({count} файлов, {_format_size(size)})")
            else:
                skipped += 1
                print(f"SKIP -> {zip_path.name} уже существует (используйте --overwrite)")
        except Exception as exc:
            failed.append((folder, str(exc)))
            print(f"ОШИБКА ({folder}): {exc}", file=sys.stderr)

    print(f"Итого архивов: создано={created}, пропущено={skipped}, ошибок={len(failed)}")
    if failed and created == 0:
        return 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
