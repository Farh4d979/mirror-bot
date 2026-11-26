"""Create maximum-compression 7z archives for the /compress-* commands."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .config import Settings
from .dumper import find_7zip
from .errors import CompressionError, JobCancelled, JobTimeout
from .utils import CancelToken, sanitize_filename

logger = logging.getLogger(__name__)

ProgressCB = Callable[[Optional[float], str], None]
_HASH_CHUNK = 1024 * 1024
_PERCENT_RE = re.compile(rb"(\d{1,3})%")


@dataclass(frozen=True)
class CompressionResult:
    path: Path
    size: int
    sha256: str


def build_archive_name(source_name: str, partitions: Sequence[str] = ()) -> str:
    """Pick a safe .7z name based on the source and requested partitions."""
    stem = Path(source_name).stem or "archive"
    if partitions:
        stem = f"{stem}-{'-'.join(partitions)}"
    return sanitize_filename(f"{stem}.7z", "archive.7z")


def build_7z_command(
    seven_zip: str, archive: Path, members: Sequence[str]
) -> list[str]:
    """Build the 7-Zip command used for a maximum-compression archive."""
    return [
        seven_zip,
        "a",
        "-t7z",
        "-mx=9",
        "-m0=lzma2",
        "-ms=on",
        "-sccUTF-8",
        "-y",
        "-bsp1",
        "--",
        str(archive),
        *members,
    ]


def sha256_file(
    path: Path,
    *,
    cancel: Optional[CancelToken] = None,
    progress: Optional[ProgressCB] = None,
) -> str:
    """Hash a file in chunks so large archives do not need to fit in RAM."""
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    last_report = 0.0
    with path.open("rb") as handle:
        while True:
            if cancel is not None:
                cancel.check()
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if progress is not None and (now - last_report >= 0.5 or done == total):
                percent = (done / total * 100.0) if total else 100.0
                progress(None, f"Computing SHA-256… {percent:.0f}%")
                last_report = now
    return digest.hexdigest()


def compress_to_7z(
    files: Sequence[Path],
    archive: Path,
    *,
    settings: Settings,
    progress: ProgressCB,
    cancel: CancelToken,
    seven_zip: Optional[str] = None,
) -> CompressionResult:
    """Pack ``files`` into one 7z archive and return its size and SHA-256."""
    paths = [Path(path) for path in files]
    if not paths:
        raise CompressionError("There are no files to compress.")
    for path in paths:
        if not path.is_file():
            raise CompressionError(f"{path.name} disappeared before compression.")
        if path.stat().st_size == 0:
            raise CompressionError(f"{path.name} is empty and cannot be compressed.")

    executable = seven_zip or find_7zip(settings)
    if not executable:
        raise CompressionError(
            "7-Zip is required to create the archive. Install it and retry, "
            "or set SEVEN_ZIP_PATH to the 7z executable."
        )

    parents = [path.parent.resolve() for path in paths]
    try:
        root = Path(os.path.commonpath([str(parent) for parent in parents]))
    except ValueError:
        root = parents[0]
    members: list[str] = []
    for path in paths:
        resolved = path.resolve()
        try:
            members.append(str(resolved.relative_to(root)))
        except ValueError:
            members.append(str(resolved))

    archive = archive.resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.unlink(missing_ok=True)

    command = build_7z_command(executable, archive, members)
    logger.info(
        "Creating %s with 7-Zip (%d file(s), maximum compression)",
        archive.name,
        len(paths),
    )

    process = subprocess.Popen(
        command,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    errors: list[str] = []
    last_percent = -1

    def read_stdout() -> None:
        nonlocal last_percent
        if process.stdout is None:
            return
        tail = b""
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            data = tail + chunk
            matches = _PERCENT_RE.findall(data)
            if matches:
                value = min(100, int(matches[-1]))
                if value != last_percent:
                    last_percent = value
                    progress(
                        float(value),
                        f"Compressing with 7-Zip · {value}%",
                    )
            tail = data[-8:]

    def read_stderr() -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                errors.append(line)
                del errors[:-20]

    stdout_thread = threading.Thread(
        target=read_stdout, name="7z-stdout", daemon=True
    )
    stderr_thread = threading.Thread(
        target=read_stderr, name="7z-stderr", daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        while process.poll() is None:
            cancel.check()
            time.sleep(0.2)
    except (JobCancelled, JobTimeout):
        _terminate(process)
        raise
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if process.poll() is None:
            _terminate(process)

    code = process.returncode
    if code is None:
        try:
            code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate(process)
            code = process.returncode

    details = " | ".join(errors[-4:]) or "no details from 7-Zip"
    if not archive.is_file() or archive.stat().st_size == 0:
        raise CompressionError(
            f"7-Zip could not create the archive (exit code {code}): {details}"
        )
    if code not in (0, 1):
        archive.unlink(missing_ok=True)
        raise CompressionError(
            f"7-Zip failed with exit code {code}: {details}"
        )
    if code == 1:
        logger.warning("7-Zip reported a warning for %s: %s", archive.name, details)

    size = archive.stat().st_size
    progress(None, "Computing the archive SHA-256…")
    digest = sha256_file(archive, cancel=cancel, progress=progress)
    return CompressionResult(path=archive, size=size, sha256=digest)


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
