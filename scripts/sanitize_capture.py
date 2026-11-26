from __future__ import annotations

"""
Локальный санитайзер HAR 1.2 и HTML-дампов перед сохранением или передачей в LLM.
Зависимости: ТОЛЬКО стандартная библиотека Python (SPEC §1.1, §5, §8).
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth",
        "auth_token",
        "apikey",
        "api_key",
        "key",
        "secret",
        "sessid",
        "session",
        "session_id",
        "sid",
        "phpsessid",
        "jsessionid",
        "csrf",
        "xsrf",
        "csrf_token",
        "signature",
        "sig",
        "code",
    }
)

COOKIE_HEADERS = frozenset({"cookie", "set-cookie"})
AUTH_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "x-access-token"}
)
CSRF_HEADERS = frozenset({"x-csrf-token", "x-xsrf-token", "csrf-token", "x-csrftoken", "anti-csrf-token"})

BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b")
INLINE_COOKIE_RE = re.compile(r"((?:^|\n)\s*(?:Set-)?Cookie\s*:\s*)([^\r\n]+)", re.IGNORECASE)
DOC_COOKIE_RE = re.compile(r"""(document\.cookie\s*=\s*["'])([^"']+)(["'])""", re.IGNORECASE)
INLINE_AUTH_RE = re.compile(r"((?:^|\n)\s*(?:Authorization|X-API-Key|X-Auth-Token)\s*:\s*)([^\r\n]+)", re.IGNORECASE)
META_CSRF_1_RE = re.compile(
    r"""(<meta\b[^>]*name=["'][^"']*(?:csrf|xsrf)[^"']*["'][^>]*content=["'])([^"']+)(["'][^>]*>)""",
    re.IGNORECASE,
)
META_CSRF_2_RE = re.compile(
    r"""(<meta\b[^>]*content=["'])([^"']+)(["'][^>]*name=["'][^"']*(?:csrf|xsrf)[^"']*["'][^>]*>)""",
    re.IGNORECASE,
)
INPUT_CSRF_1_RE = re.compile(
    r"""(<input\b[^>]*name=["'][^"']*(?:csrf|xsrf|__RequestVerificationToken)[^"']*["'][^>]*value=["'])([^"']+)(["'][^>]*>)""",
    re.IGNORECASE,
)
INPUT_CSRF_2_RE = re.compile(
    r"""(<input\b[^>]*value=["'])([^"']+)(["'][^>]*name=["'][^"']*(?:csrf|xsrf|__RequestVerificationToken)[^"']*["'][^>]*>)""",
    re.IGNORECASE,
)
JS_TOKEN_RE = re.compile(
    r"""((?:csrfToken|xsrfToken|csrf_token|accessToken|authToken)\s*[:=]\s*["'])([^"']{6,})(["'])""",
    re.IGNORECASE,
)
URL_RE = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+7|8|\+\d{1,3})[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\w)")


@dataclass
class SanitizeStats:
    cookies: int = 0
    auth_headers: int = 0
    csrf_tokens: int = 0
    jwt_bearer: int = 0
    query_secrets: int = 0
    emails: int = 0
    phones: int = 0

    @property
    def total(self) -> int:
        return (
            self.cookies
            + self.auth_headers
            + self.csrf_tokens
            + self.jwt_bearer
            + self.query_secrets
            + self.emails
            + self.phones
        )


def _scrub_query_url(raw_url: str, stats: SanitizeStats) -> str:
    if "?" not in raw_url:
        return raw_url
    before_hash, *hash_parts = raw_url.split("#")
    base, query_str = before_hash.split("?", 1)
    if not query_str:
        return raw_url

    changed = False
    updated: list[str] = []
    for pair in query_str.split("&"):
        if "=" not in pair:
            updated.append(pair)
            continue
        k, v = pair.split("=", 1)
        if unquote(k).lower() in SENSITIVE_QUERY_KEYS and v and v != "REDACTED":
            stats.query_secrets += 1
            changed = True
            updated.append(f"{k}=REDACTED")
        else:
            updated.append(pair)

    if not changed:
        return raw_url
    rebuilt = f"{base}?{'&'.join(updated)}"
    return f"{rebuilt}#{'#'.join(hash_parts)}" if hash_parts else rebuilt


def scrub_text(text: str, stats: SanitizeStats) -> str:
    out = text

    def _sub_bearer(_m: re.Match[str]) -> str:
        stats.jwt_bearer += 1
        return "Bearer [REDACTED_JWT]"

    def _sub_jwt(_m: re.Match[str]) -> str:
        stats.jwt_bearer += 1
        return "[REDACTED_JWT]"

    out = BEARER_RE.sub(_sub_bearer, out)
    out = JWT_RE.sub(_sub_jwt, out)

    def _sub_cookie_line(m: re.Match[str]) -> str:
        if "[REDACTED" in m.group(2):
            return m.group(0)
        stats.cookies += 1
        return f"{m.group(1)}[REDACTED_COOKIE]"

    def _sub_doc_cookie(m: re.Match[str]) -> str:
        if "[REDACTED" in m.group(2):
            return m.group(0)
        stats.cookies += 1
        return f"{m.group(1)}[REDACTED_COOKIE]{m.group(3)}"

    out = INLINE_COOKIE_RE.sub(_sub_cookie_line, out)
    out = DOC_COOKIE_RE.sub(_sub_doc_cookie, out)

    def _sub_auth_line(m: re.Match[str]) -> str:
        if "[REDACTED" in m.group(2):
            return m.group(0)
        stats.auth_headers += 1
        return f"{m.group(1)}[REDACTED_AUTH]"

    out = INLINE_AUTH_RE.sub(_sub_auth_line, out)

    def _sub_csrf(m: re.Match[str]) -> str:
        if "[REDACTED" in m.group(2):
            return m.group(0)
        stats.csrf_tokens += 1
        return f"{m.group(1)}[REDACTED_CSRF]{m.group(3)}"

    for rx in (META_CSRF_1_RE, META_CSRF_2_RE, INPUT_CSRF_1_RE, INPUT_CSRF_2_RE):
        out = rx.sub(_sub_csrf, out)

    def _sub_js_token(m: re.Match[str]) -> str:
        if "[REDACTED" in m.group(2):
            return m.group(0)
        stats.csrf_tokens += 1
        return f"{m.group(1)}[REDACTED_TOKEN]{m.group(3)}"

    out = JS_TOKEN_RE.sub(_sub_js_token, out)
    out = URL_RE.sub(lambda m: _scrub_query_url(m.group(0), stats), out)

    def _sub_email(m: re.Match[str]) -> str:
        if "redacted.local" in m.group(0):
            return m.group(0)
        stats.emails += 1
        return "student@redacted.local"

    out = EMAIL_RE.sub(_sub_email, out)

    def _sub_phone(_m: re.Match[str]) -> str:
        stats.phones += 1
        return "[REDACTED_PHONE]"

    out = PHONE_RE.sub(_sub_phone, out)
    return out


def _sanitize_headers(headers: Any, stats: SanitizeStats) -> None:
    if not isinstance(headers, list):
        return
    for h in headers:
        if not isinstance(h, dict) or not isinstance(h.get("name"), str):
            continue
        lower = h["name"].lower()
        val = h.get("value")
        if not isinstance(val, str) or not val:
            continue
        if lower in COOKIE_HEADERS:
            if val != "[REDACTED_COOKIE]":
                h["value"] = "[REDACTED_COOKIE]"
                stats.cookies += 1
        elif lower in AUTH_HEADERS:
            if val != "[REDACTED_AUTH]":
                h["value"] = "[REDACTED_AUTH]"
                stats.auth_headers += 1
        elif lower in CSRF_HEADERS:
            if val != "[REDACTED_CSRF]":
                h["value"] = "[REDACTED_CSRF]"
                stats.csrf_tokens += 1
        else:
            h["value"] = scrub_text(val, stats)


def _sanitize_cookies(cookies: Any, stats: SanitizeStats) -> None:
    if not isinstance(cookies, list):
        return
    for c in cookies:
        if isinstance(c, dict) and isinstance(c.get("value"), str) and c["value"] != "[REDACTED_COOKIE]":
            c["value"] = "[REDACTED_COOKIE]"
            stats.cookies += 1


def _sanitize_query_list(params: Any, stats: SanitizeStats) -> None:
    if not isinstance(params, list):
        return
    for p in params:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        if p["name"].lower() in SENSITIVE_QUERY_KEYS and p.get("value") and p["value"] != "REDACTED":
            p["value"] = "REDACTED"
            stats.query_secrets += 1


def _sanitize_form_params(params: Any, stats: SanitizeStats) -> None:
    if not isinstance(params, list):
        return
    for p in params:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not isinstance(p.get("value"), str):
            continue
        val = p["value"]
        if not val or "[REDACTED" in val or val == "student@redacted.local":
            continue
        lower = p["name"].lower()
        if re.search(r"(?:csrf|xsrf|verificationtoken)", lower):
            p["value"] = "[REDACTED_CSRF]"
            stats.csrf_tokens += 1
        elif re.search(r"(?:pass|pwd|secret|auth|token|key|session|sid|cookie)", lower):
            p["value"] = "[REDACTED_AUTH]"
            stats.auth_headers += 1
        elif re.search(r"(?:email|mail)", lower):
            p["value"] = "student@redacted.local"
            stats.emails += 1
        elif re.search(r"(?:phone|tel|mobile)", lower):
            p["value"] = "[REDACTED_PHONE]"
            stats.phones += 1
        else:
            p["value"] = scrub_text(val, stats)


def sanitize_content(raw_text: str, fmt: str = "auto") -> tuple[str, str, SanitizeStats]:
    """
    Очищает HAR 1.2 (JSON) или HTML/текст от секретов и ПДн.
    Возвращает кортеж (cleaned_text, detected_format, stats).
    """
    stats = SanitizeStats()
    stripped = raw_text.lstrip()
    try_har = fmt == "har" or (fmt == "auto" and stripped.startswith("{"))

    if try_har:
        try:
            obj = json.loads(raw_text)
            if isinstance(obj, dict) and isinstance(obj.get("log"), dict):
                entries = obj["log"].get("entries")
                if isinstance(entries, list):
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        req = entry.get("request")
                        if isinstance(req, dict):
                            if isinstance(req.get("url"), str):
                                req["url"] = _scrub_query_url(req["url"], stats)
                            _sanitize_headers(req.get("headers"), stats)
                            _sanitize_cookies(req.get("cookies"), stats)
                            _sanitize_query_list(req.get("queryString"), stats)
                            post_data = req.get("postData")
                            if isinstance(post_data, dict):
                                if isinstance(post_data.get("text"), str):
                                    post_data["text"] = scrub_text(post_data["text"], stats)
                                _sanitize_form_params(post_data.get("params"), stats)
                        res = entry.get("response")
                        if isinstance(res, dict):
                            if isinstance(res.get("redirectURL"), str):
                                res["redirectURL"] = _scrub_query_url(res["redirectURL"], stats)
                            _sanitize_headers(res.get("headers"), stats)
                            _sanitize_cookies(res.get("cookies"), stats)
                            content = res.get("content")
                            if isinstance(content, dict) and isinstance(content.get("text"), str):
                                content["text"] = scrub_text(content["text"], stats)
                return json.dumps(obj, ensure_ascii=False, indent=2) + "\n", "har", stats
        except json.JSONDecodeError:
            pass

    return scrub_text(raw_text, stats), "html", stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Локальная очистка HAR/HTML-дампов от Cookie, Authorization, CSRF, JWT и ПДн."
    )
    parser.add_argument("--input", "-i", type=Path, required=True, help="Исходный .har или .html файл")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Файл для сохранения очищенного результата")
    parser.add_argument("--format", choices=("auto", "har", "html"), default="auto", help="Формат входного файла")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    src = args.input.resolve()
    if not src.exists() or not src.is_file():
        print(f"ОШИБКА: входной файл не найден: {src}", file=sys.stderr)
        return 1

    raw = src.read_text(encoding="utf-8", errors="replace")
    cleaned, detected, stats = sanitize_content(raw, fmt=args.format)

    dst = args.output.resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.tmp")
    try:
        tmp.write_text(cleaned, encoding="utf-8")
        os.replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    print(f"Формат: {detected.upper()} | Удалено фрагментов: {stats.total}")
    print(
        f"  Cookie: {stats.cookies}, Auth: {stats.auth_headers}, CSRF: {stats.csrf_tokens}, "
        f"JWT: {stats.jwt_bearer}, Query: {stats.query_secrets}, Email: {stats.emails}, Тел: {stats.phones}"
    )
    print(f"Сохранено: {dst}")
    print("Напоминание: перед передачей в LLM вручную проверьте свободный текст страницы на ФИО и логины.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
