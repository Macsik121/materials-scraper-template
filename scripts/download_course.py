from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from adapters import list_adapters, load_adapter
from core.browser import (
    DEFAULT_CDP_URL,
    CdpConnectionError,
    SessionCircuitBreaker,
    SessionExpiredError,
    connect_cdp,
    find_target_page,
)
from core.downloader import CdpDownloader, WafJitter
from core.fs_utils import PathTooLongError, atomic_write_text, build_item_path, format_size
from core.handlers.assignment import handle_assignment
from core.handlers.binary_file import handle_binary_file
from core.handlers.composite_html import handle_composite_html
from core.handlers.test_readonly import handle_test_readonly
from core.handlers.video_stream import handle_video_item
from core.models import Course, Item, ItemStatus, ItemType, Module
from core.state import ManifestStore, StateStore


def _append_external_link(course_dir: Path, module: Module, item: Item) -> Path:
    links_file = course_dir / "external_links.txt"
    existing = links_file.read_text(encoding="utf-8") if links_file.exists() else ""
    line = f"[M{module.order:02d} {module.title}] {item.title}: {item.source_url or item.lesson_url}"
    if line not in existing.splitlines():
        updated = (existing.rstrip("\n") + "\n" + line + "\n").lstrip("\n")
        atomic_write_text(links_file, updated)
    return links_file


def _process_item(
    *,
    adapter,
    page,
    context,
    downloader: CdpDownloader,
    course_dir: Path,
    course: Course,
    module: Module,
    item: Item,
) -> tuple[ItemStatus, Path | str, int, str]:
    total_modules = len(course.modules)
    total_items = len(module.items)

    if item.item_type == ItemType.EXTERNAL_LINK:
        links_path = _append_external_link(course_dir, module, item)
        return ItemStatus.EXTERNAL_LINK, links_path, links_path.stat().st_size, ""

    is_dir_item = item.item_type in (ItemType.HTML_PAGE, ItemType.ASSIGNMENT, ItemType.TEST_READONLY)
    ext = "" if is_dir_item else (item.extension or Path(item.expected_filename).suffix or ".bin")
    nested_reserve = 28 if is_dir_item else 0

    target = build_item_path(
        base_dir=course_dir,
        module_order=module.order,
        module_title=module.title,
        item_order=item.order,
        item_type=item.item_type,
        item_title=item.title,
        extension=ext,
        total_modules=total_modules,
        total_items=total_items,
        nested_artifact_reserve=nested_reserve,
    )

    resolver = lambda: adapter.resolve_item_url(page, item)

    if item.item_type == ItemType.DOCUMENT:
        return handle_binary_file(item, target, downloader, url_resolver=resolver)

    if item.item_type in (ItemType.VIDEO, ItemType.VIDEO_STREAM):
        user_agent = ""
        try:
            user_agent = str(page.evaluate("() => navigator.userAgent") or "")
        except Exception:
            pass
        return handle_video_item(
            item,
            target,
            downloader,
            context=context,
            url_resolver=resolver,
            user_agent=user_agent,
        )

    if item.item_type == ItemType.HTML_PAGE:
        payload = adapter.extract_html_content(page, item)
        frame = adapter.get_content_frame(page)
        return handle_composite_html(
            item,
            target,
            frame,
            payload,
            downloader,
            context=context,
            extra_hide_selectors=adapter.extra_hide_selectors,
        )

    if item.item_type == ItemType.ASSIGNMENT:
        payload = adapter.extract_assignment(page, item)
        frame = adapter.get_content_frame(page)
        return handle_assignment(
            item,
            target,
            frame,
            payload,
            downloader,
            extra_hide_selectors=adapter.extra_hide_selectors,
        )

    if item.item_type == ItemType.TEST_READONLY:
        payload = adapter.extract_test_review(page, item)
        frame = adapter.get_content_frame(page)
        return handle_test_readonly(
            item,
            target,
            frame,
            payload,
            extra_hide_selectors=adapter.extra_hide_selectors,
        )

    return ItemStatus.SKIPPED, target, 0, f"Неизвестный тип элемента: {item.item_type}"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Основная точка входа: выгрузка учебного курса через живой CDP-браузер."
    )
    parser.add_argument(
        "--adapter",
        required=True,
        help=f"Имя адаптера из adapters/ (например, _template). Доступно: {', '.join(list_adapters(include_template=True))}",
    )
    parser.add_argument("--dir", type=Path, default=Path("downloaded_course"), help="Целевая папка курса")
    parser.add_argument("--cdp", default=DEFAULT_CDP_URL, help="Адрес CDP браузера (по умолчанию http://127.0.0.1:9222)")
    parser.add_argument("--delay-min", type=float, default=1.0, help="Мин. WAF-пауза между элементами (сек)")
    parser.add_argument("--delay-max", type=float, default=3.5, help="Макс. WAF-пауза между элементами (сек)")
    parser.add_argument("--force-recheck", action="store_true", help="Игнорировать статус DONE и перекачать элементы")
    parser.add_argument("--use-manifest", action="store_true", help="Использовать существующий manifest.json без повторного парсинга")
    parser.add_argument("--manifest-only", action="store_true", help="Только собрать manifest.json и выйти")
    parser.add_argument("--non-interactive", action="store_true", help="Завершаться с кодом 1 при истечении сессии вместо паузы")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        adapter = load_adapter(args.adapter)
    except ValueError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1

    course_dir = args.dir.resolve()
    course_dir.mkdir(parents=True, exist_ok=True)
    manifest_store = ManifestStore(course_dir)
    state_store = StateStore(course_dir)
    jitter = WafJitter(args.delay_min, args.delay_max)
    breaker = SessionCircuitBreaker(
        threshold=3,
        non_interactive=args.non_interactive,
        on_save_state=state_store.save,
    )

    try:
        with connect_cdp(args.cdp, run_preflight=True) as (_pw, _browser, context, info):
            if info:
                print(f"CDP подключён: {info.browser_name} ({info.cdp_url}), открыто вкладок: {len(info.tabs)}")

            page = find_target_page(context, matcher=adapter.match_page)
            if page is None:
                print(
                    f"ОШИБКА: не найдена открытая вкладка для адаптера '{adapter.platform_name}'. "
                    "Откройте страницу курса в отладочном браузере и повторите запуск.",
                    file=sys.stderr,
                )
                return 1

            if args.use_manifest and manifest_store.exists():
                course = manifest_store.load()
                print(f"Загружен manifest.json: {course.title} ({course.total_items} эл.)")
            else:
                course = adapter.discover_course(page)
                manifest_store.save(course)
                print(f"Собран manifest.json: {course.title} (модулей: {len(course.modules)}, элементов: {course.total_items})")

            if args.manifest_only:
                return 0

            downloader = CdpDownloader(
                context=context,
                work_page=page,
                tmp_dir=course_dir / "_tmp_downloads",
                circuit_breaker=breaker,
            )

            partial_errors = 0
            processed = 0
            total = course.total_items

            for module in course.modules:
                print(f"\n=== Модуль M{module.order:02d}: {module.title} ({len(module.items)} эл.) ===")
                for item in module.items:
                    processed += 1
                    if state_store.should_skip(item.item_id, force_recheck=args.force_recheck):
                        print(f"[{processed}/{total}] SKIP (уже DONE): {item.title}")
                        continue

                    print(f"[{processed}/{total}] {item.item_type.value}: {item.title}")
                    try:
                        status, saved_path, size_bytes, err = _process_item(
                            adapter=adapter,
                            page=page,
                            context=context,
                            downloader=downloader,
                            course_dir=course_dir,
                            course=course,
                            module=module,
                            item=item,
                        )
                        state_store.set_status(
                            item.item_id,
                            status,
                            local_path=saved_path,
                            size_bytes=size_bytes,
                            error=err,
                        )
                        if status == ItemStatus.DONE:
                            print(f"    OK -> {Path(saved_path).name} ({format_size(size_bytes)})")
                        elif status in (ItemStatus.DRM_LOCKED, ItemStatus.FAILED):
                            partial_errors += 1
                            print(f"    {status.value}: {err}")
                        else:
                            print(f"    {status.value}: {err or saved_path}")
                    except (PathTooLongError, SessionExpiredError):
                        raise
                    except Exception as exc:
                        partial_errors += 1
                        state_store.set_status(item.item_id, ItemStatus.FAILED, error=str(exc))
                        print(f"    FAILED: {exc}")

                    jitter.sleep()

            summary = state_store.summary()
            print("\n" + "=" * 60)
            print(f"Итог ({course.title}): {summary}")
            print("=" * 60)
            return 2 if partial_errors > 0 else 0

    except (CdpConnectionError, SessionExpiredError, PathTooLongError) as exc:
        print(f"КРИТИЧЕСКАЯ ОШИБКА: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
