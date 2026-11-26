from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.downloader import validate_magic_bytes
from core.fs_utils import format_size

SOURCE_VIDEO_EXTS = frozenset({".webm", ".mkv", ".avi", ".flv", ".wmv"})


def convert_file_to_mp4(
    ffmpeg_bin: str,
    src_path: Path,
    *,
    crf: int = 23,
    preset: str = "fast",
    delete_source: bool = False,
) -> tuple[str, Path, int]:
    """
    Конвертирует видео в универсальный .mp4 (H.264 yuv420p + AAC 128k + faststart + чётные размеры кадра)
    по контракту SPEC §5.
    """
    dst_path = src_path.with_suffix(".mp4")
    if dst_path.exists() and dst_path.stat().st_size > 0:
        check = validate_magic_bytes(dst_path, expected_ext=".mp4")
        if check.valid:
            return "skipped", dst_path, dst_path.stat().st_size

    tmp_path = dst_path.with_name(f"{dst_path.stem}.tmp.mp4")
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src_path),
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(tmp_path),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(proc.stderr.strip() or f"FFmpeg exited with code {proc.returncode}")

        check = validate_magic_bytes(tmp_path, expected_ext=".mp4")
        if not check.valid:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Результат конвертации не прошёл проверку MP4: {check.reason}")

        os.replace(tmp_path, dst_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    if delete_source:
        src_path.unlink(missing_ok=True)

    return "ok", dst_path, dst_path.stat().st_size


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Пакетная конвертация .webm/.mkv видео в универсальный .mp4 через FFmpeg."
    )
    parser.add_argument("--dir", type=Path, default=Path("downloaded_course"), help="Папка курса")
    parser.add_argument("--delete-webm", action="store_true", help="Удалить исходный файл после проверки .mp4")
    parser.add_argument("--crf", type=int, default=23, help="Качество H.264 CRF (по умолчанию 23)")
    parser.add_argument("--preset", default="fast", help="Пресет кодирования FFmpeg (по умолчанию fast)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    course_dir = args.dir.resolve()
    if not course_dir.exists() or not course_dir.is_dir():
        print(f"ОШИБКА: папка курса не найдена: {course_dir}", file=sys.stderr)
        return 1

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        print(
            "ОШИБКА: ffmpeg не найден в PATH. Установите FFmpeg (например, winget install Gyan.FFmpeg) "
            "и перезапустите терминал.",
            file=sys.stderr,
        )
        return 1

    candidates = [
        p
        for p in sorted(course_dir.rglob("*"))
        if p.is_file() and "_tmp_downloads" not in p.parts and p.suffix.lower() in SOURCE_VIDEO_EXTS
    ]
    print(f"Папка: {course_dir}")
    print(f"Найдено видео для конвертации: {len(candidates)}")

    converted = 0
    skipped = 0
    failed: list[tuple[Path, str]] = []

    for idx, src in enumerate(candidates, start=1):
        rel = src.relative_to(course_dir)
        print(f"[{idx}/{len(candidates)}] {rel}...")
        try:
            status, dst, size = convert_file_to_mp4(
                ffmpeg_bin,
                src,
                crf=args.crf,
                preset=args.preset,
                delete_source=args.delete_webm,
            )
            if status == "ok":
                converted += 1
                print(f"    OK -> {dst.name} ({format_size(size)})")
            else:
                skipped += 1
                print(f"    SKIP -> {dst.name} уже существует ({format_size(size)})")
        except Exception as exc:
            failed.append((rel, str(exc)))
            print(f"    ОШИБКА: {exc}")

    print("\n" + "=" * 60)
    print(f"Сконвертировано: {converted}, пропущено: {skipped}, ошибок: {len(failed)}")
    print("=" * 60)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
