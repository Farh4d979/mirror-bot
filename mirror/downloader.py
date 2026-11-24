"""Download engines: aria2c for HTTP/torrent, yt-dlp for video sites,
gallery-dl for image galleries, megadl for MEGA links."""

from __future__ import annotations

import importlib.util
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

from .config import Settings
from .errors import (
    DownloadError,
    FileTooLarge,
    JobCancelled,
    JobTimeout,
    MirrorError,
)
from .resolver import ResolvedLink, USER_AGENT
from .utils import (
    CancelToken,
    exceeds_limit,
    filename_from_headers,
    filename_from_url,
    format_progress,
    human_size,
    parse_aria_size,
    sanitize_filename,
    SpeedMeter,
)

logger = logging.getLogger(__name__)

ProgressCB = Callable[[Optional[float], str], None]

READOUT_RE = re.compile(
    r"\[#\w+\s+([\d.]+\s*[KMGT]?i?B)/([\d.]+\s*[KMGT]?i?B)"
    r"(?:\((\d+)%\))?(?:.*?\bDL:([\d.]+\s*[KMGT]?i?B))?"
    r"(?:.*?\bETA:(\S+))?",
    re.IGNORECASE,
)

RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})

# How often a long-running helper tool is checked for /cancel and JOB_TIMEOUT.
TOOL_POLL_SECONDS = 2.0

# aria2 1.37 (the version shipped on GitHub's Ubuntu runners) does not support
# --retry-on-http-error, so a 429 has to be retried by this outer loop.
GOFILE_RETRY_DELAYS = (30, 60, 120, 240)

_RATE_LIMIT_RE = re.compile(
    r"(?:status|http(?: error)?)\s*[=:]?\s*429\b|too many requests|rate[ -]?limit",
    re.IGNORECASE,
)

# Gofile rate-limits direct downloads per IP. Jobs run in parallel, so serialize
# Gofile transfers instead of letting them trip 429 on each other.
_GOFILE_DOWNLOAD_LOCK = threading.Semaphore(1)


def _is_gofile_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "gofile.io" or host.endswith(".gofile.io")


def _retry_delay(response: requests.Response, default: float) -> float:
    """Honour Retry-After when the server sends one (seconds only)."""
    value = response.headers.get("retry-after")
    if value:
        try:
            return max(1.0, min(300.0, float(value)))
        except (TypeError, ValueError):
            pass
    return default


def _looks_like_rate_limit(message: str) -> bool:
    return bool(_RATE_LIMIT_RE.search(message))


def _wait(cancel: CancelToken, seconds: float) -> None:
    """Sleep while remaining responsive to cancellation."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        cancel.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _parse_eta_text(value: str) -> Optional[float]:
    """Parse aria2 ETA strings like 45s, 8m2s or 1h3m4s."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", (value or "").strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


@dataclass(slots=True)
class DownloadResult:
    path: Path
    size: int
    extras: int = 0


class Downloader:
    def __init__(
        self, settings: Settings, cancel: CancelToken, *, keep_all: bool = False
    ) -> None:
        self.settings = settings
        self.cancel = cancel
        self.max_size = settings.max_file_size
        # Partition dumping needs every downloaded file, not just the largest.
        self.keep_all = keep_all

    # ------------------------------------------------------------------ public
    def download(
        self,
        *,
        original_url: str,
        link: ResolvedLink,
        dest: Path,
        progress: ProgressCB,
    ) -> DownloadResult:
        dest.mkdir(parents=True, exist_ok=True)

        if link.kind == "magnet":
            return self._torrent(link.url, dest, progress)
        if link.kind == "torrent":
            return self._torrent(link.url, dest, progress, referer=link.referer)
        if link.kind == "ytdlp":
            try:
                return self._ytdlp(original_url, dest, progress)
            except (JobCancelled, JobTimeout):
                raise
            except DownloadError as exc:
                if not link.allow_gallery_fallback or not _tool_command(
                    "gallery-dl", "gallery_dl"
                ):
                    raise
                progress(None, "Video extractor failed, trying the gallery engine…")
                try:
                    gallery_dest = dest / "gallery"
                    gallery_dest.mkdir(parents=True, exist_ok=True)
                    return self._gallery(link.url, gallery_dest, progress)
                except (JobCancelled, JobTimeout):
                    raise
                except MirrorError:
                    raise exc
        if link.kind == "gallery":
            return self._gallery(link.url, dest, progress)
        if link.kind == "mega":
            return self._mega(link.url, dest, progress)

        if link.parts:
            return self._download_parts(link, dest, progress)

        if link.size and exceeds_limit(link.size, self.max_size):
            raise FileTooLarge(
                f"The file is {human_size(link.size)}, above the {human_size(self.max_size)} limit."
            )

        try:
            return self._http(link, dest, progress)
        except (JobCancelled, JobTimeout, FileTooLarge):
            raise
        except DownloadError as exc:
            if not link.allow_ytdlp_fallback or not self._ytdlp_available():
                raise
            progress(None, "Direct download failed, trying video extractor…")
            try:
                return self._ytdlp(original_url, dest, progress)
            except (JobCancelled, JobTimeout):
                raise
            except MirrorError:
                raise exc

    # -------------------------------------------------------------- HTTP/aria2
    def _http(self, link: ResolvedLink, dest: Path, progress: ProgressCB) -> DownloadResult:
        if not _is_gofile_url(link.url):
            return self._http_unlocked(link, dest, progress)

        self._acquire_gofile_slot(progress)
        try:
            return self._http_unlocked(link, dest, progress)
        finally:
            _GOFILE_DOWNLOAD_LOCK.release()

    def _acquire_gofile_slot(self, progress: ProgressCB) -> None:
        if _GOFILE_DOWNLOAD_LOCK.acquire(blocking=False):
            return
        progress(None, "Waiting for another Gofile download to finish…")
        while not _GOFILE_DOWNLOAD_LOCK.acquire(timeout=1):
            self.cancel.check()

    def _http_unlocked(self, link: ResolvedLink, dest: Path, progress: ProgressCB) -> DownloadResult:
        if shutil.which(self.settings.aria2c_path):
            return self._aria2_http(link, dest, progress)
        logger.warning("%s not found; falling back to the Python downloader", self.settings.aria2c_path)
        return self._requests_download(link, dest, progress)

    def _download_parts(
        self, link: ResolvedLink, dest: Path, progress: ProgressCB
    ) -> DownloadResult:
        parts = list(link.parts)
        skipped = 0
        if self.settings.multi_file_mode == "largest" and not self.keep_all:
            parts.sort(key=lambda item: item.size or 0, reverse=True)
            skipped = max(0, len(parts) - 1)
            parts = parts[:1]

        if len(parts) == 1:
            result = self._http(parts[0], dest, progress)
            if skipped:
                result.extras += skipped
            return result

        total = len(parts)
        for index, part in enumerate(parts, 1):
            sub = dest / f"part-{index:03d}"
            sub.mkdir(parents=True, exist_ok=True)

            def part_progress(
                percent: Optional[float],
                detail: str,
                *,
                index: int = index,
                total: int = total,
            ) -> None:
                progress(percent, f"File {index}/{total} · {detail}")

            self._http(part, sub, part_progress)

        result = self._collect(dest)
        if exceeds_limit(result.size, self.max_size):
            raise FileTooLarge(
                f"The file is {human_size(result.size)}, above the {human_size(self.max_size)} limit."
            )
        return result

    def _aria2_http(self, link: ResolvedLink, dest: Path, progress: ProgressCB) -> DownloadResult:
        out_name = sanitize_filename(link.filename or filename_from_url(link.url), "download.bin")
        gofile = _is_gofile_url(link.url)
        # Gofile dislikes many parallel connections from one IP. Be gentle there,
        # but keep full speed for other hosts.
        connections = 4 if gofile else 16
        args = [
            "--dir", str(dest),
            "--out", out_name,
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            f"--max-connection-per-server={connections}",
            f"--split={connections}",
            "--min-split-size=1M",
            f"--max-tries={10 if gofile else 5}",
            f"--retry-wait={15 if gofile else 5}",
            "--connect-timeout=30",
            "--timeout=60",
            "--summary-interval=3",
            "--console-log-level=warn",
            "--download-result=hide",
            f"--user-agent={USER_AGENT}",
        ]
        if link.referer:
            args.append(f"--referer={link.referer}")
        if link.headers:
            for key, value in link.headers.items():
                args.append(f"--header={key}: {value}")
        args.append(link.url)

        attempts = len(GOFILE_RETRY_DELAYS) + 1 if gofile else 1
        for attempt in range(1, attempts + 1):
            try:
                self._run_aria2(args, dest, progress, label="download")
                break
            except DownloadError as exc:
                if not gofile or not _looks_like_rate_limit(str(exc)):
                    raise
                logger.warning(
                    "Gofile download attempt %d/%d failed: %s",
                    attempt,
                    attempts,
                    exc,
                )
                if attempt == attempts:
                    raise DownloadError(
                        "Gofile kept rate-limiting downloads after several retries "
                        f"({attempts} attempts). Wait a few minutes and try again, "
                        "or use Pixeldrain with /px."
                    ) from exc
                delay = GOFILE_RETRY_DELAYS[attempt - 1]
                progress(None, f"Gofile is rate-limiting downloads. Retrying in {delay}s…")
                _wait(self.cancel, delay)

        result = self._collect(dest)
        if exceeds_limit(result.size, self.max_size):
            raise FileTooLarge(
                f"The file is {human_size(result.size)}, above the {human_size(self.max_size)} limit."
            )
        return result

    def _torrent(
        self,
        url: str,
        dest: Path,
        progress: ProgressCB,
        *,
        referer: Optional[str] = None,
    ) -> DownloadResult:
        if not shutil.which(self.settings.aria2c_path):
            raise DownloadError(
                "Torrent and magnet links need aria2c. Install aria2 (it is preinstalled in the "
                "Docker image and GitHub Actions workflow)."
            )

        progress(None, "Fetching torrent metadata…")
        args = [
            "--dir", str(dest),
            "--seed-time=0",
            "--bt-stop-timeout=300",
            "--bt-save-metadata=false",
            "--follow-torrent=mem",
            "--bt-tracker-connect-timeout=30",
            "--bt-tracker-timeout=30",
            "--max-concurrent-downloads=1",
            "--file-allocation=none",
            "--summary-interval=3",
            "--console-log-level=warn",
            "--download-result=hide",
            "--max-tries=5",
            "--retry-wait=5",
            "--connect-timeout=30",
            "--timeout=60",
            "--user-agent=" + USER_AGENT,
        ]
        if referer:
            args.append(f"--referer={referer}")
        args.append(url)
        self._run_aria2(args, dest, progress, label="torrent")
        result = self._collect(dest)
        if exceeds_limit(result.size, self.max_size):
            raise FileTooLarge(
                f"The torrent's largest file is {human_size(result.size)}, above the "
                f"{human_size(self.max_size)} limit."
            )
        return result

    def _run_aria2(
        self,
        args: list[str],
        dest: Path,
        progress: ProgressCB,
        *,
        label: str,
    ) -> None:
        command = [self.settings.aria2c_path, *args]
        logger.info("Starting aria2 %s", label)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(dest),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise DownloadError(f"Could not start aria2c: {exc}") from exc

        tail: deque[str] = deque(maxlen=25)
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                tail.append(line)
                self.cancel.check()
                match = READOUT_RE.search(line)
                if not match:
                    continue
                downloaded_text = match.group(1)
                total_text = match.group(2)
                percent = float(match.group(3)) if match.group(3) else None
                speed_text = match.group(4) or ""
                eta_text = match.group(5) or ""

                total_bytes = parse_aria_size(total_text)
                if total_bytes and exceeds_limit(total_bytes, self.max_size):
                    process.terminate()
                    raise FileTooLarge(
                        f"The file is {human_size(total_bytes)}, above the "
                        f"{human_size(self.max_size)} limit."
                    )

                if total_bytes == 0:
                    progress(
                        None,
                        "Fetching torrent metadata…" if label == "torrent" else "Waiting for server…",
                    )
                    continue

                downloaded_bytes = parse_aria_size(downloaded_text)
                speed_bps = parse_aria_size(speed_text) if speed_text else None
                eta_seconds = _parse_eta_text(eta_text) if eta_text else None
                if downloaded_bytes is not None:
                    detail = format_progress(
                        downloaded_bytes, total_bytes, speed_bps, eta_seconds
                    )
                else:
                    detail = f"{downloaded_text} / {total_text}"
                    stats = format_progress(None, None, speed_bps, eta_seconds)
                    if stats:
                        detail += f" · {stats}"
                progress(percent, detail)

            code = process.wait(timeout=60)
        except (JobCancelled, JobTimeout, FileTooLarge):
            _terminate(process)
            raise
        except subprocess.TimeoutExpired:
            _terminate(process)
            raise DownloadError("aria2c did not exit after finishing the transfer.")
        finally:
            if process.poll() is None:
                _terminate(process)

        if code != 0:
            if self.cancel.cancelled:
                raise JobCancelled("Cancelled.")
            details = [line for line in tail if not line.startswith("[#")]
            message = " | ".join(details[-4:]) or "no details from aria2c"
            raise DownloadError(f"aria2c failed with exit code {code}: {message}")

    def _open_download(
        self, link: ResolvedLink, headers: dict[str, str], progress: ProgressCB
    ) -> requests.Response:
        attempts = 5
        for attempt in range(1, attempts + 1):
            self.cancel.check()
            try:
                response = requests.get(
                    link.url,
                    headers=headers,
                    stream=True,
                    allow_redirects=True,
                    timeout=(30, 60),
                )
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise DownloadError(f"Download failed: {exc}") from exc
                delay = min(60, 5 * attempt)
                progress(None, f"Network error; retrying in {delay}s…")
                _wait(self.cancel, delay)
                continue

            if response.status_code not in RETRYABLE_HTTP_STATUS:
                return response

            if attempt == attempts:
                response.close()
                if response.status_code == 429:
                    raise DownloadError(
                        "Gofile is rate-limiting downloads right now. "
                        "Wait a minute and try again."
                        if _is_gofile_url(link.url)
                        else "The server is rate-limiting downloads (HTTP 429). "
                        "Wait a minute and try again."
                    )
                raise DownloadError(
                    f"The server kept returning HTTP {response.status_code}. Try again later."
                )

            delay = _retry_delay(response, min(60, 5 * (2 ** (attempt - 1))))
            response.close()
            if response.status_code == 429 and _is_gofile_url(link.url):
                message = "Gofile is rate-limiting downloads"
            else:
                message = f"The server returned HTTP {response.status_code}"
            progress(None, f"{message}; retrying in {delay}s…")
            _wait(self.cancel, delay)

        raise DownloadError("Download failed after several attempts.")

    def _requests_download(self, link: ResolvedLink, dest: Path, progress: ProgressCB) -> DownloadResult:
        name = sanitize_filename(link.filename or filename_from_url(link.url), "download.bin")
        target = dest / name
        partial = target.with_suffix(target.suffix + ".part")
        headers = {"User-Agent": USER_AGENT}
        if link.referer:
            headers["Referer"] = link.referer
        if link.headers:
            headers.update(link.headers)

        response = self._open_download(link, headers, progress)

        try:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/html" in content_type and not link.force_download:
                raise DownloadError("The server returned a web page instead of a file.")
            remote_name = filename_from_headers(response.headers)
            if remote_name and (link.filename is None or link.filename == "download.bin"):
                target = dest / remote_name
                partial = target.with_suffix(target.suffix + ".part")

            total = None
            try:
                total = int(response.headers.get("content-length", ""))
            except (TypeError, ValueError):
                pass
            if total and exceeds_limit(total, self.max_size):
                raise FileTooLarge(
                    f"The file is {human_size(total)}, above the {human_size(self.max_size)} limit."
                )

            downloaded = 0
            meter = SpeedMeter()
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    self.cancel.check()
                    downloaded += len(chunk)
                    if exceeds_limit(downloaded, self.max_size):
                        raise FileTooLarge(
                            f"The file grew past the {human_size(self.max_size)} limit."
                        )
                    handle.write(chunk)
                    percent = downloaded / total * 100 if total else None
                    speed, eta = meter.update(downloaded, total)
                    progress(
                        percent,
                        format_progress(downloaded, total, speed, eta),
                    )
        except (JobCancelled, JobTimeout, FileTooLarge, DownloadError):
            partial.unlink(missing_ok=True)
            raise
        except requests.RequestException as exc:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"Download failed: {exc}") from exc
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise DownloadError(f"Could not write the download to disk: {exc}") from exc
        finally:
            response.close()

        if not partial.exists() or partial.stat().st_size == 0:
            raise DownloadError("The download was empty.")
        partial.replace(target)
        return DownloadResult(path=target, size=target.stat().st_size)

    # ------------------------------------------------------- gallery-dl / MEGA
    def _gallery(self, url: str, dest: Path, progress: ProgressCB) -> DownloadResult:
        command = _tool_command("gallery-dl", "gallery_dl")
        if not command:
            raise DownloadError(
                "Image gallery links need gallery-dl. Install it with "
                "`pip install gallery-dl` (it is in requirements.txt) and restart the bot."
            )

        progress(None, "Downloading gallery…")
        args = [
            *command,
            "--config-ignore",
            "-d", str(dest),
            "--no-part",
            "--no-mtime",
            "--no-colors",
            "--retries", "5",
            "--http-timeout", "30",
            f"--user-agent={USER_AGENT}",
        ]
        if self.max_size > 0:
            args += ["--filesize-max", str(self.max_size)]
        if self.settings.cookies_file and self.settings.cookies_file.exists():
            args += ["--cookies", str(self.settings.cookies_file)]
        args.append(url)

        def on_line(line: str) -> None:
            if line.startswith("[download]") or "Downloading" in line:
                progress(None, line[:200])

        self._run_tool(args, dest, progress, label="gallery-dl", on_line=on_line)
        result = self._collect(dest)
        if exceeds_limit(result.size, self.max_size):
            raise FileTooLarge(
                f"The largest gallery file is {human_size(result.size)}, above the "
                f"{human_size(self.max_size)} limit."
            )
        return result

    def _mega(self, url: str, dest: Path, progress: ProgressCB) -> DownloadResult:
        command = _tool_command("megadl")
        if not command:
            # Newer Windows megatools builds ship one multi-call executable
            # instead of a separate megadl.exe.
            megatools = shutil.which("megatools")
            if megatools:
                command = [megatools, "dl"]
        if not command:
            raise DownloadError(
                "MEGA links need megatools. Install megatools (with megadl or "
                "megatools.exe on PATH) and restart the bot."
            )

        progress(None, "Downloading from MEGA…")
        args = [
            *command,
            "--path", str(dest),
            "--print-names",
            "--no-progress",
            url,
        ]

        def on_line(line: str) -> None:
            progress(None, f"Downloaded {line}"[:200])

        self._run_tool(args, dest, progress, label="megadl", on_line=on_line)
        result = self._collect(dest)
        if exceeds_limit(result.size, self.max_size):
            raise FileTooLarge(
                f"The largest MEGA file is {human_size(result.size)}, above the "
                f"{human_size(self.max_size)} limit."
            )
        return result

    def _run_tool(
        self,
        command: list[str],
        dest: Path,
        progress: ProgressCB,
        *,
        label: str,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> None:
        logger.info("Starting %s", label)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(dest),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise DownloadError(f"Could not start {label}: {exc}") from exc

        failure: list[MirrorError] = []
        stop = threading.Event()

        def watchdog() -> None:
            # Tools like megadl --no-progress can be silent for many minutes, so
            # check cancellation/deadline on a timer instead of only on output.
            while not stop.wait(TOOL_POLL_SECONDS):
                try:
                    self.cancel.check()
                except MirrorError as exc:
                    failure.append(exc)
                    _terminate(process)
                    return

        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()

        tail: deque[str] = deque(maxlen=25)
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                tail.append(line)
                self.cancel.check()
                if on_line:
                    on_line(line)
            code = process.wait(timeout=60)
        except (JobCancelled, JobTimeout):
            _terminate(process)
            raise
        except subprocess.TimeoutExpired:
            _terminate(process)
            raise DownloadError(f"{label} did not exit after finishing the transfer.")
        finally:
            stop.set()
            watcher.join(timeout=5)
            if process.poll() is None:
                _terminate(process)

        if failure:
            raise failure[0]
        if code != 0:
            if self.cancel.cancelled:
                raise JobCancelled("Cancelled.")
            details = [line for line in tail if line]
            message = " | ".join(details[-4:]) or f"no details from {label}"
            raise DownloadError(f"{label} failed with exit code {code}: {message}")

    # ------------------------------------------------------------------- yt-dlp
    def _ytdlp_available(self) -> bool:
        return importlib.util.find_spec("yt_dlp") is not None

    def _ytdlp(self, url: str, dest: Path, progress: ProgressCB) -> DownloadResult:
        if not self._ytdlp_available():
            raise DownloadError("yt-dlp is not installed, so this video site cannot be downloaded.")
        import yt_dlp  # imported lazily; the package is optional at runtime

        def hook(data: dict) -> None:
            self.cancel.check()
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes") or 0
                percent = downloaded / total * 100 if total else None
                speed = data.get("speed")
                eta = data.get("eta")
                progress(percent, format_progress(downloaded, total, speed, eta))
            elif status == "finished":
                progress(100, "Processing video…")

        options = {
            "paths": {"home": str(dest), "temp": str(dest)},
            "outtmpl": "%(title).150B [%(id)s].%(ext)s",
            "format": self.settings.ytdlp_format,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "overwrites": True,
            "progress_hooks": [hook],
        }
        if self.max_size > 0:
            options["max_filesize"] = self.max_size
        if self.settings.cookies_file and self.settings.cookies_file.exists():
            options["cookiefile"] = str(self.settings.cookies_file)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(url, download=True)
        except (JobCancelled, JobTimeout):
            raise
        except Exception as exc:  # yt-dlp raises many error classes
            message = str(exc).strip() or exc.__class__.__name__
            if "max-filesize" in message.lower() or "larger than max" in message.lower():
                raise FileTooLarge(
                    f"The video is larger than the {human_size(self.max_size)} limit."
                ) from exc
            raise DownloadError(f"Video extractor failed: {message[:400]}") from exc

        result = self._collect(dest)
        if exceeds_limit(result.size, self.max_size):
            raise FileTooLarge(
                f"The video is {human_size(result.size)}, above the {human_size(self.max_size)} limit."
            )
        return result

    # ------------------------------------------------------------------ output
    def _collect(self, dest: Path) -> DownloadResult:
        ignored = {".aria2", ".part", ".ytdl", ".tmp"}
        files = [
            path
            for path in dest.rglob("*")
            if path.is_file() and path.suffix.lower() not in ignored and path.stat().st_size > 0
        ]
        if not files:
            raise DownloadError("The download finished but produced no file.")

        files.sort(key=lambda path: path.stat().st_size, reverse=True)
        largest = files[0]
        extras = len(files) - 1

        if extras and self.settings.multi_file_mode == "zip" and not self.keep_all:
            archive_base = dest.parent / sanitize_filename(dest.name, "download")
            archive = Path(shutil.make_archive(str(archive_base), "zip", root_dir=dest))
            shutil.rmtree(dest, ignore_errors=True)
            return DownloadResult(path=archive, size=archive.stat().st_size)

        if extras:
            logger.info("Picked the largest of %d downloaded files", len(files))
        return DownloadResult(path=largest, size=largest.stat().st_size, extras=extras)


def _tool_command(executable: str, module: Optional[str] = None) -> Optional[list[str]]:
    """Find a CLI tool, falling back to `python -m <module>` when needed."""
    found = shutil.which(executable)
    if found:
        return [found]
    if module:
        try:
            if importlib.util.find_spec(module) is not None:
                return [sys.executable, "-m", module]
        except (ImportError, ValueError):
            pass
    return None


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
        except OSError:
            pass
