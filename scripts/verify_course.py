from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.browser import DEFAULT_CDP_URL, CdpConnectionError, connect_cdp, find_target_page
from core.downloader import validate_magic_bytes
from core.models import ItemStatus
from core.state import ManifestStore, StateStore


AUDITED_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
        ".doc",
        ".xls",
        ".ppt",
        ".zip",
        ".rar",
        ".7z",
        ".webm",
        ".mkv",
        ".mp4",
        ".mov",
        ".avi",
        ".png",
        ".jpg",
        ".jpeg",
    }
)

JUNK_SUFFIXES = (".part", ".crdownload", ".tmp", ".tmp.png", ".tmp.pdf")


def audit_directory_offline(
    course_dir: Path,
    *,
    clean: bool = False,
) -> tuple[int, list[tuple[Path, str]], list[Path], list[str]]:
    """
    Оффлайн-аудит целостности файлов (только стандартная библиотека Python, SPEC §1.1 и §5):
    - проверяет magic bytes для всех поддерживаемых бинарных файлов;
    - выявляет временный мусор (.part, .crdownload, .tmp);
    - сверяет state.json (файлы со статусом DONE, пропавшие с диска, переводятся в FAILED при --clean).
    """
    checked = 0
    broken: list[tuple[Path, str]] = []
    junk: list[Path] = []
    state_issues: list[str] = []

    for path in sorted(course_dir.rglob("*")):
        if not path.is_file():
            continue
        if "_tmp_downloads" in path.parts:
            junk.append(path)
            continue
        if any(path.name.lower().endswith(sfx) for sfx in JUNK_SUFFIXES):
            junk.append(path)
            continue
        if path.name in ("manifest.json", "state.json", "external_links.txt"):
            continue
        if path.suffix.lower() not in AUDITED_EXTENSIONS:
            continue

        checked += 1
        res = validate_magic_bytes(path)
        if not res.valid:
            broken.append((path, f"{res.detected_type}: {res.reason}"))

    state_store = StateStore(course_dir)
    broken_set = {p.resolve() for p, _ in broken}
    state_dirty = False

    for item_id, rec in state_store.all_records().items():
        if rec.status != ItemStatus.DONE:
            continue
        if not rec.local_path:
            state_issues.append(f"{item_id}: статус DONE без local_path")
            continue
        target = Path(rec.local_path)
        if not target.is_absolute():
            target = course_dir / target
        if not target.exists():
            state_issues.append(f"{item_id}: отсутствует на диске ({rec.local_path})")
            if clean:
                state_store.set_status(
                    item_id,
                    ItemStatus.FAILED,
                    local_path=rec.local_path,
                    error="Отсутствует файл при verify_course --clean",
                    auto_save=False,
                )
                state_dirty = True
        elif target.is_file() and target.resolve() in broken_set:
            state_issues.append(f"{item_id}: файл повреждён ({rec.local_path})")
            if clean:
                state_store.set_status(
                    item_id,
                    ItemStatus.FAILED,
                    local_path="",
                    error="Повреждённый файл удалён через verify_course --clean",
                    auto_save=False,
                )
                state_dirty = True

    if clean:
        for path, _ in broken:
            path.unlink(missing_ok=True)
        for path in junk:
            path.unlink(missing_ok=True)
        if state_dirty:
            state_store.save()

    return checked, broken, junk, state_issues


def verify_online_against_course(course_dir: Path, cdp_url: str, adapter_name: str) -> list[str]:
    """Онлайн-сверка локального manifest/state со списком элементов на живой странице курса."""
    from adapters import load_adapter

    adapter = load_adapter(adapter_name)
    state_store = StateStore(course_dir)
    missing: list[str] = []

    with connect_cdp(cdp_url, run_preflight=True) as (_pw, _browser, context, _info):
        page = find_target_page(context, matcher=adapter.match_page)
        if page is None:
            raise CdpConnectionError(f"Вкладка для адаптера '{adapter_name}' не найдена в браузере.")

        live_course = adapter.discover_course(page)
        ManifestStore(course_dir).save(live_course)
        for mod in live_course.modules:
            for item in mod.items:
                rec = state_store.get(item.item_id)
                if rec is None or rec.status not in (ItemStatus.DONE, ItemStatus.EXTERNAL_LINK):
                    status_label = rec.status.value if rec else "MISSING"
                    missing.append(f"M{mod.order:02d}/{item.item_id} ({item.title}) -> {status_label}")
    return missing


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Онлайн/оффлайн аудит целостности скачанного курса (magic bytes + state.json + --clean)."
    )
    parser.add_argument("--dir", type=Path, default=Path("downloaded_course"), help="Папка курса")
    parser.add_argument("--cdp", default=DEFAULT_CDP_URL, help="Адрес CDP для онлайн-сверки")
    parser.add_argument("--adapter", default="", help="Имя адаптера для онлайн-сверки со страницей курса")
    parser.add_argument("--offline", action="store_true", help="Только оффлайн-аудит (без подключения к браузеру)")
    parser.add_argument("--clean", action="store_true", help="Удалить битые файлы и .part/.crdownload/.tmp")
    parser.add_argument("--force-recheck", action="store_true", help="Принудительно перепроверить все файлы")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    course_dir = args.dir.resolve()
    if not course_dir.exists() or not course_dir.is_dir():
        print(f"ОШИБКА: папка курса не найдена: {course_dir}", file=sys.stderr)
        return 1

    checked, broken, junk, state_issues = audit_directory_offline(course_dir, clean=args.clean)

    print(f"Папка курса:        {course_dir}")
    print(f"Проверено файлов:   {checked}")
    print(f"Битых файлов:       {len(broken)}")
    print(f"Временного мусора:  {len(junk)}")
    print(f"Расхождений state:  {len(state_issues)}")

    for path, reason in broken:
        rel = path.relative_to(course_dir)
        action = " [УДАЛЁН]" if args.clean else ""
        print(f"  BROKEN{action}: {rel} -> {reason}")

    for path in junk:
        rel = path.relative_to(course_dir)
        action = " [УДАЛЁН]" if args.clean else ""
        print(f"  JUNK{action}:   {rel}")

    for issue in state_issues:
        print(f"  STATE:  {issue}")

    online_missing: list[str] = []
    if not args.offline and args.adapter:
        try:
            online_missing = verify_online_against_course(course_dir, args.cdp, args.adapter)
            print(f"Не выгружено по онлайн-сверке: {len(online_missing)}")
            for line in online_missing:
                print(f"  MISSING: {line}")
        except (CdpConnectionError, ValueError) as exc:
            print(f"ОШИБКА онлайн-сверки: {exc}", file=sys.stderr)
            return 1

    has_issues = bool(broken or junk or state_issues or online_missing)
    return 2 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
