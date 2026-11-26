from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

from core.downloader import CdpDownloader
from core.models import Item, ItemStatus


DRM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("HLS SAMPLE-AES", re.compile(r"METHOD\s*=\s*SAMPLE-AES(?:-CTR|-CENC)?", re.IGNORECASE)),
    ("Widevine UUID", re.compile(r"edef8ba9-79d6-4ace-a3c8-27dcd51d21ed", re.IGNORECASE)),
    ("PlayReady UUID", re.compile(r"9a04f079-9840-4286-ab92-e65be0885f95", re.IGNORECASE)),
    ("FairPlay Streaming", re.compile(r"com\.apple\.streamingkeydelivery|skd://", re.IGNORECASE)),
    ("DASH ContentProtection", re.compile(r"<ContentProtection\b", re.IGNORECASE)),
    ("DASH cenc:pssh", re.compile(r"<(?:cenc:)?pssh\b", re.IGNORECASE)),
]

AES128_RE = re.compile(r"METHOD\s*=\s*AES-128\b", re.IGNORECASE)


@dataclass
class DrmCheckResult:
    is_drm: bool
    is_aes128: bool
    indicators: list[str] = field(default_factory=list)


def inspect_stream_manifest(manifest_text: str) -> DrmCheckResult:
    """
    Предварительная проверка HLS (.m3u8) / DASH (.mpd) манифеста на DRM в памяти (SPEC §4.2).
    Обычный HLS AES-128 НЕ считается DRM и скачивается штатно через yt-dlp / ffmpeg.
    """
    indicators = [label for label, pattern in DRM_PATTERNS if pattern.search(manifest_text or "")]
    is_aes128 = bool(AES128_RE.search(manifest_text or "")) and not indicators
    return DrmCheckResult(is_drm=bool(indicators), is_aes128=is_aes128, indicators=indicators)


def is_stream_url(url: str) -> bool:
    lower = (url or "").lower().split("?", 1)[0]
    return lower.endswith((".m3u8", ".mpd")) or ".m3u8" in url.lower() or ".mpd" in url.lower()


def probe_video_ffprobe(path: Path) -> tuple[bool, str]:
    """Проверяет видео через ffprobe: длительность > 0 и наличие видео/аудиодорожек."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True, "ffprobe не найден в PATH — выполнена только проверка размера файла"

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return False, f"ffprobe вернул ошибку: {proc.stderr.strip()}"

    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return False, f"Не удалось разобрать вывод ffprobe: {exc}"

    duration = float(info.get("format", {}).get("duration") or 0.0)
    streams = info.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    if duration <= 0 or not has_video:
        return False, f"Битый видеопоток (duration={duration}, video_streams={has_video})"
    return True, ""


def _build_cookie_header(context: BrowserContext, url: str) -> str:
    try:
        cookies = context.cookies([url])
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))
    except Exception:
        return ""


def download_hls_dash_stream(
    manifest_url: str,
    target_path: Path,
    *,
    context: BrowserContext,
    referer: str = "",
    user_agent: str = "",
) -> tuple[ItemStatus, Path, int, str]:
    """
    Проверяет манифест на DRM и скачивает незащищённый поток через yt-dlp или ffmpeg
    с пробросом Cookie, Referer и User-Agent из живой сессии браузера.
    """
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    if user_agent:
        headers["User-Agent"] = user_agent

    resp = context.request.get(manifest_url, headers=headers, timeout=30_000)
    if resp.status >= 400:
        return ItemStatus.FAILED, target_path, 0, f"HTTP {resp.status} при чтении манифеста потока"

    manifest_text = resp.text()
    drm = inspect_stream_manifest(manifest_text)
    if drm.is_drm:
        return (
            ItemStatus.DRM_LOCKED,
            target_path,
            0,
            f"Поток защищён DRM ({', '.join(drm.indicators)}) — скачивание пропущено",
        )

    cookie_header = _build_cookie_header(context, manifest_url)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f"{target_path.stem}.tmp.mp4")

    yt_dlp = shutil.which("yt-dlp")
    ffmpeg = shutil.which("ffmpeg")
    if not yt_dlp and not ffmpeg:
        return ItemStatus.FAILED, target_path, 0, "Для скачивания HLS/DASH требуется yt-dlp или ffmpeg в PATH"

    try:
        if yt_dlp:
            cmd = [yt_dlp, "--no-part", "--merge-output-format", "mp4", "-o", str(tmp_path)]
            if referer:
                cmd.extend(["--add-header", f"Referer:{referer}"])
            if user_agent:
                cmd.extend(["--add-header", f"User-Agent:{user_agent}"])
            if cookie_header:
                cmd.extend(["--add-header", f"Cookie:{cookie_header}"])
            cmd.append(manifest_url)
        else:
            assert ffmpeg is not None
            raw_headers = []
            if referer:
                raw_headers.append(f"Referer: {referer}")
            if user_agent:
                raw_headers.append(f"User-Agent: {user_agent}")
            if cookie_header:
                raw_headers.append(f"Cookie: {cookie_header}")
            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
            if raw_headers:
                cmd.extend(["-headers", "\r\n".join(raw_headers) + "\r\n"])
            cmd.extend(["-i", manifest_url, "-c", "copy", "-movflags", "+faststart", str(tmp_path)])

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            tmp_path.unlink(missing_ok=True)
            err = (proc.stderr or proc.stdout or "неизвестная ошибка").strip().splitlines()[-1:]
            return ItemStatus.FAILED, target_path, 0, f"Ошибка скачивания потока: {' '.join(err)}"

        ok, probe_err = probe_video_ffprobe(tmp_path)
        if not ok:
            tmp_path.unlink(missing_ok=True)
            return ItemStatus.FAILED, target_path, 0, probe_err

        os.replace(tmp_path, target_path)
        return ItemStatus.DONE, target_path, target_path.stat().st_size, ""
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return ItemStatus.FAILED, target_path, 0, str(exc)


def handle_video_item(
    item: Item,
    target_path: Path,
    downloader: CdpDownloader,
    *,
    context: BrowserContext,
    url_resolver: Callable[[], str] | None = None,
    referer: str = "",
    user_agent: str = "",
) -> tuple[ItemStatus, Path, int, str]:
    """
    Унифицированный обработчик прямых видео (.webm/.mp4/.mkv) и потоков HLS/DASH (SPEC §4.2).
    """
    video_url = item.source_url
    if not video_url and url_resolver is not None:
        video_url = url_resolver()

    if not video_url:
        return ItemStatus.FAILED, target_path, 0, "Не удалось определить URL видео"

    if is_stream_url(video_url):
        mp4_target = target_path.with_suffix(".mp4")
        return download_hls_dash_stream(
            video_url,
            mp4_target,
            context=context,
            referer=referer or item.lesson_url,
            user_agent=user_agent,
        )

    saved = downloader.download_file(
        video_url,
        target_path,
        expected_ext=item.extension or target_path.suffix,
        referer=referer or item.lesson_url,
        url_resolver=url_resolver,
    )
    return ItemStatus.DONE, saved, saved.stat().st_size, ""
