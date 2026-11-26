from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator
from urllib.parse import urlparse

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright


DEFAULT_CDP_URL = "http://127.0.0.1:9222"

LOGIN_URL_RE = re.compile(
    r"(?:/login|/signin|/sign-in|/auth(?:/|$)|/sso|/oauth|/accounts/login|/passport)",
    re.IGNORECASE,
)


class CdpConnectionError(RuntimeError):
    """Критическая ошибка подключения к отладочному браузеру (код возврата 1)."""


class SessionExpiredError(RuntimeError):
    """Сработал Session Expiration Circuit Breaker (сессия LMS истекла)."""


@dataclass
class CdpTabInfo:
    title: str
    url: str
    tab_type: str = "page"


@dataclass
class CdpPreflightInfo:
    cdp_url: str
    browser_name: str
    protocol_version: str
    websocket_url: str
    tabs: list[CdpTabInfo] = field(default_factory=list)


def normalize_cdp_url(cdp_url: str) -> str:
    url = (cdp_url or DEFAULT_CDP_URL).strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url


def _fetch_json(url: str, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def preflight_cdp(cdp_url: str = DEFAULT_CDP_URL, timeout: float = 3.0) -> CdpPreflightInfo:
    """
    Выполняет префлайт-проверку GET /json/version и /json/list (SPEC §2.2).
    Не требует Playwright — использует только стандартную библиотеку.
    """
    base = normalize_cdp_url(cdp_url)
    try:
        version_data = _fetch_json(f"{base}/json/version", timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise CdpConnectionError(
            f"Не удалось подключиться к CDP по адресу {base} ({exc}).\n"
            + format_port_conflict_runbook(base)
        ) from exc

    tabs: list[CdpTabInfo] = []
    try:
        raw_tabs = _fetch_json(f"{base}/json/list", timeout=timeout)
        if isinstance(raw_tabs, list):
            for entry in raw_tabs:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "page":
                    tabs.append(
                        CdpTabInfo(
                            title=str(entry.get("title", "")),
                            url=str(entry.get("url", "")),
                            tab_type=str(entry.get("type", "page")),
                        )
                    )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        pass

    return CdpPreflightInfo(
        cdp_url=base,
        browser_name=str(version_data.get("Browser", "Unknown Chromium")),
        protocol_version=str(version_data.get("Protocol-Version", "")),
        websocket_url=str(version_data.get("webSocketDebuggerUrl", "")),
        tabs=tabs,
    )


def format_port_conflict_runbook(cdp_url: str = DEFAULT_CDP_URL) -> str:
    parsed = urlparse(normalize_cdp_url(cdp_url))
    port = parsed.port or 9222
    return (
        f"Проверьте, что браузер запущен с --remote-debugging-port={port} "
        f"--remote-debugging-address=127.0.0.1 и ASCII-профилем (--user-data-dir=\"C:\\Users\\Public\\chrome-debug\").\n"
        f"Если порт {port} занят зависшим процессом (Windows PowerShell):\n"
        f"  Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue |\n"
        f"      Select-Object -ExpandProperty OwningProcess -Unique |\n"
        f"      ForEach-Object {{ Stop-Process -Id $_ -Force }}\n"
        f"  Stop-Process -Name chrome, browser -Force -ErrorAction SilentlyContinue"
    )


def is_login_redirect(url: str, status_code: int | None = None) -> bool:
    """Определяет признаки истечения сессии: HTTP 401/403 или редирект на страницу логина."""
    if status_code in (401, 403):
        return True
    if not url:
        return False
    return bool(LOGIN_URL_RE.search(url))


class SessionCircuitBreaker:
    """
    Контроллер истечения сессии (SPEC §2.3).
    После `threshold` подряд ошибок авторизации сохраняет состояние и переводит процесс
    в интерактивную паузу (или выбрасывает SessionExpiredError в non-interactive режиме).
    """

    def __init__(
        self,
        threshold: int = 3,
        non_interactive: bool = False,
        on_save_state: Callable[[], None] | None = None,
        prompt_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.threshold = max(1, threshold)
        self.non_interactive = non_interactive
        self.on_save_state = on_save_state
        self.prompt_fn = prompt_fn or input
        self.consecutive_failures = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_auth_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures < self.threshold:
            return

        if self.on_save_state is not None:
            self.on_save_state()

        if self.non_interactive or not sys.stdin.isatty():
            raise SessionExpiredError(
                f"Session Circuit Breaker: {self.consecutive_failures} ошибок авторизации подряд ({reason}). "
                "state.json сохранён. Обновите вход в браузере и перезапустите скрипт."
            )

        print("\n" + "!" * 64)
        print(f"ВНИМАНИЕ: похоже, истекла сессия LMS ({reason}).")
        print("Текущий state.json сохранён. Обновите авторизацию в окне браузера.")
        print("!" * 64)
        answer = self.prompt_fn("Нажмите Enter после повторного входа (или введите 'q' для выхода): ").strip().lower()
        if answer in ("q", "quit", "exit"):
            raise SessionExpiredError("Выполнение остановлено пользователем после истечения сессии.")
        self.consecutive_failures = 0


@contextmanager
def connect_cdp(
    cdp_url: str = DEFAULT_CDP_URL,
    run_preflight: bool = True,
) -> Iterator[tuple[Playwright, Browser, BrowserContext, CdpPreflightInfo | None]]:
    """
    Подключается к живому браузеру через Playwright CDP.
    Никогда не закрывает пользовательский браузер при выходе (только отключает Playwright).
    """
    info = preflight_cdp(cdp_url) if run_preflight else None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CdpConnectionError(
            "Пакет playwright не установлен. Выполните: pip install -r requirements.txt "
            "(команду playwright install выполнять не нужно)."
        ) from exc

    base = normalize_cdp_url(cdp_url)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(base)
        except Exception as exc:
            raise CdpConnectionError(f"Ошибка connect_over_cdp({base}): {exc}") from exc

        contexts = browser.contexts
        context = contexts[0] if contexts else browser.new_context()
        try:
            yield pw, browser, context, info
        finally:
            try:
                browser.close()
            except Exception:
                pass


def find_target_page(
    context: BrowserContext,
    matcher: Callable[[Page], bool] | None = None,
    url_substring: str | None = None,
) -> Page | None:
    """Ищет открытую вкладку курса среди вкладок контекста."""
    pages = list(context.pages)
    for page in pages:
        try:
            if matcher is not None and matcher(page):
                return page
            if url_substring and url_substring.lower() in (page.url or "").lower():
                return page
        except Exception:
            continue
    return pages[0] if (matcher is None and not url_substring and pages) else None


def find_target_frame(
    page: Page,
    *,
    name_or_id: str | None = None,
    url_substring: str | None = None,
    css_selector: str | None = None,
) -> Frame:
    """
    Ищет целевой фрейм (актуально для iframe/frameset СДО, SPEC §3 и §7).
    Если подходящий вложенный фрейм не найден — возвращает page.main_frame.
    """
    for frame in page.frames:
        try:
            if name_or_id and frame.name == name_or_id:
                return frame
            if url_substring and url_substring.lower() in (frame.url or "").lower():
                return frame
            if css_selector and frame.locator(css_selector).count() > 0:
                return frame
        except Exception:
            continue
    return page.main_frame
