from __future__ import annotations

import argparse
import io
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import img2pdf
    from PIL import Image
except ImportError:
    print("Для работы pack_to_pdf.py требуются Pillow и img2pdf.")
    print("Установи их командой: pip install pillow img2pdf")
    sys.exit(1)

# Отключаем предупреждение Pillow для сверхдлинных full-page скриншотов
Image.MAX_IMAGE_PIXELS = None

DEFAULT_DIR = Path("downloaded_course")
A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
SERIES_RE = re.compile(r"^(?P<base>.+?)_(?P<idx>\d{2,3})$")


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Убирает альфа-канал (img2pdf не принимает RGBA) с заливкой белым фоном."""
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[3])
        return canvas
    return img.convert("RGB")


def compute_row_darkness(img_rgb: Image.Image, dark_threshold: int = 242) -> list[int]:
    """
    Для каждой строки пикселей считает число «небелых» пикселей
    на уменьшенной по ширине копии через LUT Pillow + быстрый sum(bytes) без NumPy.
    """
    w, h = img_rgb.size
    sample_w = min(w, 420)
    gray = img_rgb.convert("L")
    if sample_w < w:
        gray = gray.resize((sample_w, h), Image.Resampling.BILINEAR)

    lut = [1 if px < dark_threshold else 0 for px in range(256)]
    raw = gray.point(lut).tobytes()
    return [sum(raw[y * sample_w : (y + 1) * sample_w]) for y in range(h)]


def find_smart_cut_y(
    row_darkness: list[int],
    start_y: int,
    target_y: int,
    total_h: int,
    search_ratio: float = 0.18,
) -> int:
    """
    Ищет оптимальную линию разреза в нижних `search_ratio` страницы,
    предпочитая середину самой широкой чистой (белой) полосы.
    """
    if target_y >= total_h:
        return total_h

    page_h = target_y - start_y
    band = max(24, int(page_h * search_ratio))
    low = max(start_y + int(page_h * 0.5), target_y - band)
    high = min(total_h - 1, target_y)

    # 1. Ищем полностью чистые белые полосы ближе к низу страницы
    best_gap_center: int | None = None
    y = high
    while y >= low:
        if row_darkness[y] == 0:
            gap_end = y
            while y >= low and row_darkness[y] == 0:
                y -= 1
            gap_start = y + 1
            # Пропускаем одиночный 1px шум, но если полоса >= 3px — режем по её центру
            if (gap_end - gap_start + 1) >= 3:
                best_gap_center = (gap_start + gap_end) // 2
                break
        y -= 1

    if best_gap_center is not None:
        return best_gap_center

    # 2. Если полностью белой строки нет — берём строку с минимумом тёмных пикселей ближе к target_y
    best_y = high
    best_score = row_darkness[high]
    for cand_y in range(high - 1, low - 1, -1):
        sc = row_darkness[cand_y]
        if sc < best_score:
            best_score = sc
            best_y = cand_y
            if sc == 0:
                break
    return best_y


def image_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def slice_image_to_a4_pages(img_rgb: Image.Image, margin_mm: float) -> list[bytes]:
    """Режет длинный скриншот на страницы с пропорциями печатной области A4."""
    w, h = img_rgb.size
    printable_w = A4_WIDTH_MM - 2 * margin_mm
    printable_h = A4_HEIGHT_MM - 2 * margin_mm
    if printable_w <= 20 or printable_h <= 20:
        raise ValueError("Слишком большие поля (--margin-mm) для формата A4")

    target_page_h = max(100, int(round(w * (printable_h / printable_w))))
    row_darkness = compute_row_darkness(img_rgb)

    pages: list[bytes] = []
    y = 0
    while y < h:
        cut_y = find_smart_cut_y(row_darkness, start_y=y, target_y=y + target_page_h, total_h=h)
        if cut_y <= y:
            cut_y = min(h, y + target_page_h)

        crop = img_rgb.crop((0, y, w, cut_y))
        slice_h = cut_y - y

        # Дополняем белым полем снизу, чтобы последняя/неполная страница выравнивалась по верху A4
        if slice_h < target_page_h:
            padded = Image.new("RGB", (w, target_page_h), (255, 255, 255))
            padded.paste(crop, (0, 0))
            crop = padded

        pages.append(image_to_png_bytes(crop))
        y = cut_y

    return pages


def validate_pdf(path: Path) -> None:
    size = path.stat().st_size
    if size < 64:
        raise RuntimeError("Сгенерированный PDF слишком мал")
    with open(path, "rb") as f:
        head = f.read(16)
        f.seek(max(0, size - 2048))
        tail = f.read(2048)
    if b"%PDF-" not in head or b"%%EOF" not in tail:
        raise RuntimeError("Сгенерированный PDF не прошёл проверку сигнатуры (%PDF- / %%EOF)")


def _chunk_sort_key(path: Path) -> tuple[int, int, str]:
    """Числовая сортировка серий кадров: visual_report_01.._99.._100."""
    m = SERIES_RE.match(path.stem)
    if m:
        return (1, int(m.group("idx")), path.name)
    return (0, 0, path.name)


def discover_report_groups(root: Path, name_prefix: str) -> list[tuple[Path, list[Path]]]:
    """
    Группирует PNG-отчёты:
      - visual_report.png -> visual_report.pdf
      - visual_report_01.png, visual_report_02.png -> visual_report.pdf
    """
    grouped: dict[tuple[Path, str], list[Path]] = defaultdict(list)

    for png in sorted(root.rglob("*.png")):
        if "_tmp_downloads" in png.parts or png.name.endswith(".tmp.png"):
            continue
        stem = png.stem
        if name_prefix and not stem.startswith(name_prefix):
            continue

        m = SERIES_RE.match(stem)
        base = m.group("base") if m else stem
        grouped[(png.parent, base)].append(png)

    result: list[tuple[Path, list[Path]]] = []
    for (folder, base), files in sorted(grouped.items()):
        result.append((folder / f"{base}.pdf", sorted(files, key=_chunk_sort_key)))
    return result


def pack_group_to_pdf(
    pdf_path: Path,
    png_files: list[Path],
    page_size: str,
    margin_mm: float,
    overwrite: bool,
    delete_png: bool,
) -> tuple[str, int, int]:
    if pdf_path.exists() and pdf_path.stat().st_size > 0 and not overwrite:
        return "skipped", 0, pdf_path.stat().st_size

    page_blobs: list[bytes] = []
    for png_path in png_files:
        with Image.open(png_path) as raw_img:
            rgb_img = flatten_to_rgb(raw_img)
            if page_size == "a4":
                page_blobs.extend(slice_image_to_a4_pages(rgb_img, margin_mm=margin_mm))
            else:
                page_blobs.append(image_to_png_bytes(rgb_img))

    if not page_blobs:
        raise RuntimeError("Нет изображений для упаковки")

    if page_size == "a4":
        a4_pt = (img2pdf.mm_to_pt(A4_WIDTH_MM), img2pdf.mm_to_pt(A4_HEIGHT_MM))
        border_pt = img2pdf.mm_to_pt(margin_mm)
        layout_fun = img2pdf.get_layout_fun(pagesize=a4_pt, border=(border_pt, border_pt))
        pdf_bytes = img2pdf.convert(page_blobs, layout_fun=layout_fun)
    else:
        pdf_bytes = img2pdf.convert(page_blobs)

    tmp_pdf = pdf_path.with_suffix(".tmp.pdf")
    try:
        tmp_pdf.write_bytes(pdf_bytes)
        validate_pdf(tmp_pdf)
        os.replace(tmp_pdf, pdf_path)
    except Exception:
        tmp_pdf.unlink(missing_ok=True)
        raise

    if delete_png:
        for png_path in png_files:
            png_path.unlink(missing_ok=True)

    return "ok", len(page_blobs), pdf_path.stat().st_size


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Офлайн-конвертация PNG-отчётов (visual_report.png) в многостраничные PDF."
    )
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="Папка курса для поиска PNG-отчётов")
    parser.add_argument(
        "--prefix",
        default="visual_report",
        help="Префикс имён PNG-отчётов (по умолчанию: visual_report; пустая строка = все PNG)",
    )
    parser.add_argument(
        "--page-size",
        choices=("a4", "single"),
        default="a4",
        help="a4 — умная нарезка на страницы A4; single — один бесшовный лист по размеру PNG",
    )
    parser.add_argument("--margin-mm", type=float, default=8.0, help="Поля страницы A4 в мм (по умолчанию 8)")
    parser.add_argument("--delete-png", action="store_true", help="Удалить исходные PNG после проверки PDF")
    parser.add_argument("--overwrite", action="store_true", help="Перезаписывать существующие PDF")
    args = parser.parse_args()

    root = args.dir.resolve()
    if not root.exists():
        print(f"Папка не найдена: {root}")
        sys.exit(1)

    groups = discover_report_groups(root, name_prefix=args.prefix)
    print(f"Папка:            {root}")
    print(f"Найдено отчётов:  {len(groups)} (режим: {args.page_size})\n")

    if not groups:
        return

    created = 0
    skipped = 0
    failed: list[tuple[Path, str]] = []

    for idx, (pdf_path, png_files) in enumerate(groups, start=1):
        rel_pdf = pdf_path.relative_to(root)
        print(f"[{idx}/{len(groups)}] {rel_pdf} ({len(png_files)} PNG)...")
        try:
            status, pages, size = pack_group_to_pdf(
                pdf_path=pdf_path,
                png_files=png_files,
                page_size=args.page_size,
                margin_mm=args.margin_mm,
                overwrite=args.overwrite,
                delete_png=args.delete_png,
            )
            if status == "ok":
                created += 1
                print(f"    OK -> {pdf_path.name} ({pages} стр., {format_size(size)})")
            else:
                skipped += 1
                print(f"    пропуск (уже есть; используй --overwrite для перезаписи)")
        except Exception as exc:
            print(f"    ОШИБКА: {exc}")
            failed.append((rel_pdf, str(exc)))

    print("\n" + "=" * 60)
    print(f"Создано PDF:  {created}")
    if skipped:
        print(f"Пропущено:    {skipped}")
    if failed:
        print(f"Ошибки:       {len(failed)}")
        for rel, err in failed:
            print(f"  - {rel}: {err}")
    print("=" * 60)

    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()