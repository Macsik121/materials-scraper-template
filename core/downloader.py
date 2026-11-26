from __future__ import annotations

import os
import random
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs, unquote, unquote_to_bytes, urljoin, urlparse

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

from core.browser import SessionCircuitBreaker, is_login_redirect


HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body")

SIGNED_URL_PARAMS = frozenset(
    {
        "expires",
        "x-amz-expires",
        "x-amz-signature",
        "signature",
        "sig",
        "exp",
        "token",
        "x-goog-expires",
    }
)


@dataclass
class ValidationResult:
    valid: bool
    detected_type: str
    reason: str = ""


def validate_magic_bytes(path: Path, expected_ext: str = "") -> ValidationResult:
    """
    Мгновенная проверка бинарных заголовков (SPEC §4.1, §4.2, §5).
    Проверяет:
    - ненулевой размер;
    - отсутствие HTML-страницы логина/редиректа внутри бинарного файла;
    - сигнатуры PDF (%PDF- + %%EOF), Office/ZIP (PK\\x03\\x04), PNG, JPEG, WebM/MKV (EBML), MP4/MOV (ftyp), AVI (RIFF..AVI ).
    """
    if not path.exists() or not path.is_file():
        return ValidationResult(False, "missing", "Файл не существует")

    size = path.stat().st_size
    if size == 0:
        return ValidationResult(False, "empty", "Размер файла 0 байт")

    ext = (expected_ext or path.suffix).lower().lstrip(".")
    with open(path, "rb") as f:
        head = f.read(512)
        tail_offset = max(0, size - 4096)
        f.seek(tail_offset)
        tail = f.read(4096)

    head_stripped = head.lstrip().lower()
    is_html_ext = ext in ("html", "htm", "mhtml")
    if not is_html_ext and any(head_stripped.startswith(m) for m in HTML_MARKERS):
        return ValidationResult(
            False,
            "html_masquerade",
            f"Внутри .{ext or 'bin'} обнаружен HTML (вероятно, страница логина или редирект)",
        )

    if ext == "pdf":
        if b"%PDF-" not in head[:32]:
            return ValidationResult(False, "invalid_pdf", "Отсутствует заголовок %PDF-")
        if b"%%EOF" not in tail:
            return ValidationResult(False, "truncated_pdf", "Отсутствует маркер %%EOF в конце PDF (файл обрезан)")
        return ValidationResult(True, "pdf")

    if ext in ("docx", "xlsx", "pptx", "zip", "odt", "odp", "ods", "epub"):
        if not head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            return ValidationResult(False, "invalid_zip_office", f"Отсутствует сигнатура PK\\x03\\x04 для .{ext}")
        return ValidationResult(True, "zip_office")

    if ext in ("doc", "xls", "ppt"):
        if not head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return ValidationResult(False, "invalid_ole", f"Отсутствует OLE-сигнатура для .{ext}")
        return ValidationResult(True, "ole_office")

    if ext == "png":
        if not head.startswith(b"\x89PNG\r\n\x1a\n"):
            return ValidationResult(False, "invalid_png", "Отсутствует сигнатура PNG")
        return ValidationResult(True, "png")

    if ext in ("jpg", "jpeg"):
        if not head.startswith(b"\xff\xd8\xff"):
            return ValidationResult(False, "invalid_jpeg", "Отсутствует сигнатура JPEG")
        return ValidationResult(True, "jpeg")

    if ext in ("webm", "mkv"):
        if not head.startswith(b"\x1a\x45\xdf\xa3"):
            return ValidationResult(False, "invalid_ebml", f"Отсутствует сигнатура EBML (\\x1a\\x45\\xdf\\xa3) для .{ext}")
        return ValidationResult(True, "ebml_video")

    if ext in ("mp4", "m4v", "mov", "m4a"):
        if len(head) < 12 or head[4:8] != b"ftyp":
            return ValidationResult(False, "invalid_mp4", f"Отсутствует бокс ftyp для .{ext}")
        return ValidationResult(True, "mp4_video")

    if ext == "avi":
        if not (head.startswith(b"RIFF") and head[8:12] == b"AVI "):
            return ValidationResult(False, "invalid_avi", "Отсутствует сигнатура RIFF/AVI")
        return ValidationResult(True, "avi_video")

    if ext in ("rar",):
        if not head.startswith(b"Rar!"):
            return ValidationResult(False, "invalid_rar", "Отсутствует сигнатура Rar!")
        return ValidationResult(True, "rar")

    if ext in ("7z",):
        if not head.startswith(b"7z\xbc\xaf\x27\x1c"):
            return ValidationResult(False, "invalid_7z", "Отсутствует сигнатура 7z")
        return ValidationResult(True, "7z")

    return ValidationResult(True, ext or "binary")


def decode_redirect_url(raw_url: str, param_names: tuple[str, ...] = ("url", "file", "target", "redirect", "href")) -> tuple[str, str]:
    """
    Извлекает и декодирует целевой URL из прокладок redirect.asp?url=... или viewer?file=...
    Поддерживает fallback UTF-8 -> Windows-1251 (Postmortem §7).
    Возвращает кортеж (decoded_url, encoding_used).
    """
    parsed = urlparse(raw_url)
    if not parsed.query:
        return raw_url, "direct"

    raw_pairs = parsed.query.split("&")
    for pair in raw_pairs:
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k.lower() not in param_names or not v:
            continue

        raw_bytes = unquote_to_bytes(v.replace("+", " "))
        try:
            decoded = raw_bytes.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            decoded = raw_bytes.decode("cp1251", errors="replace")
            encoding = "windows-1251"

        # Двойное процентное кодирование (например %252F)
        if "%3A%2F%2F" in decoded.upper() or decoded.lower().startswith(("http%3a", "https%3a")):
            decoded = unquote(decoded)

        if decoded.startswith(("http://", "https://", "/")):
            if decoded.startswith("/"):
                decoded = urljoin(f"{parsed.scheme}://{parsed.netloc}", decoded)
            return decoded, encoding

    return raw_url, "direct"


def extract_pdfjs_file_url(viewer_url: str) -> str:
    """Извлекает реальный PDF URL из параметра ?file= встроенного просмотрщика PDF.js."""
    decoded, _ = decode_redirect_url(viewer_url, param_names=("file", "src", "url"))
    return decoded


def has_signed_url_params(url: str) -> bool:
    """Проверяет, содержит ли URL признаки краткоживущей подписи (S3/CloudFront/CDN)."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return any(k.lower() in SIGNED_URL_PARAMS for k in qs)


class WafJitter:
    """Случайная пауза между логическими операциями для защиты от WAF (SPEC §4.1)."""

    def __init__(self, delay_min: float = 1.0, delay_max: float = 3.5) -> None:
        self.delay_min = max(0.0, delay_min)
        self.delay_max = max(self.delay_min, delay_max)

    def sleep(self) -> float:
        if self.delay_max <= 0:
            return 0.0
        duration = random.uniform(self.delay_min, self.delay_max)
        time.sleep(duration)
        return duration


class CdpDownloader:
    """
    Последовательный нативный загрузчик через Chrome DevTools Protocol (SPEC §4.1):
    - Browser.setDownloadBehavior(allowAndName) -> _tmp_downloads;
    - строго 1 загрузка за раз (без Race Condition по guid);
    - Popup Await (ожидание URL/старта загрузки во всплывающем окне);
    - Cross-Origin Fallback (fetch->Blob или context.request.get с куками и Referer);
    - валидация magic bytes до переноса файла из _tmp_downloads;
    - Lazy URL re-resolve (1 повторное разрешение при 403 на Signed URL).
    """

    def __init__(
        self,
        context: BrowserContext,
        work_page: Page,
        tmp_dir: Path,
        *,
        stall_timeout_sec: float = 120.0,
        popup_wait_sec: float = 5.0,
        circuit_breaker: SessionCircuitBreaker | None = None,
    ) -> None:
        self.context = context
        self.page = work_page
        self.tmp_dir = tmp_dir.resolve()
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.stall_timeout_sec = stall_timeout_sec
        self.popup_wait_sec = popup_wait_sec
        self.circuit_breaker = circuit_breaker
        self._cdp_session = self.context.new_cdp_session(self.page)
        self._configure_download_behavior()

    def _configure_download_behavior(self) -> None:
        self._cdp_session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(self.tmp_dir),
                "eventsEnabled": True,
            },
        )

    def download_file(
        self,
        url: str,
        target_path: Path,
        *,
        expected_ext: str = "",
        referer: str = "",
        url_resolver: Callable[[], str] | None = None,
    ) -> Path:
        """
        Скачивает файл по URL, проверяет magic bytes и атомарно перемещает в target_path.
        При 403 с Signed URL делает 1 попытку переразрешения через url_resolver (SPEC §4.1).
        """
        current_url, _ = decode_redirect_url(url)
        attempts = 2 if (url_resolver is not None and has_signed_url_params(current_url)) else 1

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._download_once(
                    current_url,
                    target_path,
                    expected_ext=expected_ext,
                    referer=referer,
                )
            except PermissionError as exc:
                last_error = exc
                if attempt < attempts and url_resolver is not None:
                    refreshed = url_resolver()
                    if refreshed:
                        current_url, _ = decode_redirect_url(refreshed)
                        continue
                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_auth_failure(str(exc))
                raise
            except Exception as exc:
                last_error = exc
                if attempt < attempts and url_resolver is not None and "403" in str(exc):
                    refreshed = url_resolver()
                    if refreshed:
                        current_url, _ = decode_redirect_url(refreshed)
                        continue
                raise

        assert last_error is not None
        raise last_error

    def _download_once(
        self,
        url: str,
        target_path: Path,
        *,
        expected_ext: str = "",
        referer: str = "",
    ) -> Path:
        ext = expected_ext or target_path.suffix
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Сначала пробуем нативную загрузку через CDP (<a download>),
        # а при кросс-доменном открытии без Content-Disposition переключаемся на context.request.get
        tmp_file: Path | None = None
        try:
            tmp_file = self._download_via_cdp_anchor(url)
        except TimeoutError:
            tmp_file = self._download_via_context_request(url, referer=referer)

        try:
            check = validate_magic_bytes(tmp_file, expected_ext=ext)
            if not check.valid:
                tmp_file.unlink(missing_ok=True)
                raise RuntimeError(f"Проверка magic bytes не пройдена ({check.detected_type}): {check.reason}")

            os.replace(tmp_file, target_path)
            if self.circuit_breaker is not None:
                self.circuit_breaker.record_success()
            return target_path
        except Exception:
            if tmp_file is not None:
                tmp_file.unlink(missing_ok=True)
            raise

    def _download_via_cdp_anchor(self, url: str) -> Path:
        state: dict[str, object] = {
            "guid": None,
            "done": False,
            "canceled": False,
            "last_progress": time.monotonic(),
        }

        def on_will_begin(params: dict) -> None:
            if state["guid"] is None:
                state["guid"] = params.get("guid")
                state["last_progress"] = time.monotonic()

        def on_progress(params: dict) -> None:
            if state["guid"] and params.get("guid") != state["guid"]:
                return
            state["last_progress"] = time.monotonic()
            st = params.get("state")
            if st == "completed":
                state["done"] = True
            elif st == "canceled":
                state["canceled"] = True

        self._cdp_session.on("Browser.downloadWillBegin", on_will_begin)
        self._cdp_session.on("Browser.downloadProgress", on_progress)

        try:
            self.page.evaluate(
                """(downloadUrl) => {
                    const a = document.createElement("a");
                    a.href = downloadUrl;
                    a.download = "";
                    a.rel = "noopener";
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                }""",
                url,
            )

            # Ждём старта загрузки (короткий таймаут — если cross-origin без attachment, быстро уйдём в fallback)
            start_deadline = time.monotonic() + 8.0
            while state["guid"] is None and time.monotonic() < start_deadline:
                self.page.wait_for_timeout(100)

            if state["guid"] is None:
                raise TimeoutError("CDP downloadWillBegin не получен (вероятно, cross-origin без Content-Disposition)")

            while not state["done"] and not state["canceled"]:
                if time.monotonic() - float(state["last_progress"]) > self.stall_timeout_sec:
                    raise RuntimeError(f"Загрузка зависла более чем на {self.stall_timeout_sec:.0f} сек")
                self.page.wait_for_timeout(150)

            if state["canceled"]:
                raise RuntimeError("Загрузка отменена браузером (state=canceled)")

            guid = str(state["guid"])
            candidate = self.tmp_dir / guid
            if not candidate.exists():
                raise RuntimeError(f"Временный файл загрузки не найден: {candidate}")
            return candidate
        finally:
            try:
                self._cdp_session.remove_listener("Browser.downloadWillBegin", on_will_begin)
                self._cdp_session.remove_listener("Browser.downloadProgress", on_progress)
            except Exception:
                pass

    def _download_via_context_request(self, url: str, *, referer: str = "") -> Path:
        """
        Cross-Origin Fallback (SPEC §4.1):
        использует context.request.get() с куками и Referer живой сессии браузера.
        """
        headers = {}
        if referer:
            headers["Referer"] = referer
        elif self.page.url and self.page.url.startswith("http"):
            headers["Referer"] = self.page.url

        resp = self.context.request.get(url, headers=headers, timeout=int(self.stall_timeout_sec * 1000))
        if resp.status in (401, 403):
            raise PermissionError(f"HTTP {resp.status} при скачивании {url}")
        if is_login_redirect(resp.url, resp.status):
            raise PermissionError(f"Редирект на страницу входа ({resp.url})")
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} при скачивании {url}")

        body = resp.body()
        tmp_file = self.tmp_dir / f"req_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.tmp"
        tmp_file.write_bytes(body)
        return tmp_file

    def capture_popup_url(self, trigger_action: Callable[[], None]) -> str | None:
        """
        Умное ожидание всплывающих окон (Popup Await, SPEC §4.1):
        не закрывает popup мгновенно, ждёт появления целевого URL до popup_wait_sec,
        затем аккуратно закрывает вкладку.
        """
        captured: dict[str, str | None] = {"url": None}
        opened_pages: list[Page] = []

        def on_page(popup: Page) -> None:
            opened_pages.append(popup)

        self.context.on("page", on_page)
        try:
            trigger_action()
            deadline = time.monotonic() + self.popup_wait_sec
            while time.monotonic() < deadline:
                if opened_pages:
                    popup = opened_pages[-1]
                    cur_url = popup.url or ""
                    if cur_url and cur_url != "about:blank":
                        captured["url"] = cur_url
                        break
                self.page.wait_for_timeout(120)
        finally:
            try:
                self.context.remove_listener("page", on_page)
            except Exception:
                pass
            for popup in opened_pages:
                try:
                    popup.close()
                except Exception:
                    pass

        if captured["url"]:
            decoded, _ = decode_redirect_url(captured["url"])
            return decoded
        return None

    def cleanup_tmp(self) -> None:
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
