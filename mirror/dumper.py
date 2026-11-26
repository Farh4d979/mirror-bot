"""Extract Android partition images from OTA payloads and super images.

The download itself is handled by :mod:`mirror.downloader`.  This module takes
whatever was downloaded (a directory, a raw image, an OTA zip, a fastboot ROM
tar, …) and produces only the partition images the user asked for.

Two extraction paths are supported:

* ``payload.bin`` / OTA zips: handled by ``payload-dumper-go``.  It understands
  compressed and delta payloads and can read a payload directly from a zip.
* ``super.img``: Android's logical-partition metadata is parsed here, so a
  sparse super image can be read without ``simg2img`` and only the requested
  extents are copied.  This is important on GitHub's ~14 GB runners.

Archives may be ZIP/TAR or 7z/RAR.  ZIP and TAR are read with Python's
standard library; 7z and RAR are streamed through the ``7z`` command-line
tool so large ROMs are never unpacked in full.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
import shutil
import struct
import subprocess
import tarfile
import threading
import time
import zipfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional, Sequence

from .config import Settings
from .errors import DumpError, JobCancelled, JobTimeout, MirrorError
from .utils import CancelToken, format_progress

logger = logging.getLogger(__name__)

ProgressCB = Callable[[Optional[float], str], None]

PARTITION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IMG_SUFFIX_RE = re.compile(r"\.img$", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[\s,]+")
_DUMP_ARG_RE = re.compile(r"^--?dump(?:=(.*))?$", re.IGNORECASE)
_SPLIT_PART_RE = re.compile(r"^(?P<stem>.+)\.img\.(?P<index>\d+)$", re.IGNORECASE)

_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
)
_SEVEN_ZIP_SUFFIXES = (".7z", ".rar")

_PAYLOAD_MAGIC = b"CrAU"
_SPARSE_MAGIC = 0xED26FF3A
_GEOMETRY_MAGIC = 0x616C4467
_HEADER_MAGIC = 0x414C5030
_GEOMETRY_SIZE = 4096
_RESERVED_BYTES = 4096
_SECTOR_SIZE = 512
_SLOT_SUFFIXED = 1 << 1
_COPY_CHUNK = 8 * 1024 * 1024
_SCAN_CHUNK = 4 * 1024 * 1024


# --------------------------------------------------------------------- parsing
def parse_dump_args(args: Sequence[str]) -> tuple[list[str], list[str], bool]:
    """Split ``-dump boot,vendor`` arguments from the other command arguments.

    Returns ``(remaining_args, dump_values, dump_requested)``.  Values may be
    comma-separated or separate tokens; ``--dump=boot,vendor`` is accepted too.
    """
    remaining: list[str] = []
    values: list[str] = []
    requested = False
    collecting = False

    for raw in args:
        token = str(raw).strip()
        if not token:
            continue
        match = _DUMP_ARG_RE.match(token)
        if match:
            requested = True
            collecting = True
            if match.group(1):
                values.append(match.group(1))
            continue
        if collecting:
            if token.startswith("-"):
                collecting = False
                remaining.append(token)
            else:
                values.append(token)
        else:
            remaining.append(token)

    return remaining, values, requested


def normalize_partitions(values: Iterable[str]) -> tuple[str, ...]:
    """Normalise partition names and reject anything that is not a safe name."""
    names: list[str] = []
    for value in values:
        for piece in _SPLIT_RE.split(str(value or "")):
            piece = _IMG_SUFFIX_RE.sub("", piece.strip()).lower()
            if not piece:
                continue
            if not PARTITION_NAME_RE.fullmatch(piece):
                raise DumpError(
                    f"`{piece}` is not a valid partition name. Example: "
                    "`/gf <link> -dump boot,vendor`."
                )
            if piece not in names:
                names.append(piece)
    return tuple(names)


# ---------------------------------------------------------------------- result
@dataclass(slots=True)
class ExtractionResult:
    files: list[Path] = field(default_factory=list)
    found: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _ArchiveEntry:
    archive: Path
    name: str
    kind: str  # "zip" | "tar" | "7z" | "rar"
    size: int = 0
    encrypted: bool = False

    @property
    def basename(self) -> str:
        return PurePosixPath(self.name).name


@dataclass(slots=True)
class _ImageSource:
    path: Path
    entry: Optional[_ArchiveEntry] = None


@dataclass(slots=True)
class _SplitSuper:
    """A super image stored as ``super.img.0``, ``super.img.1``, … parts."""

    parts: list[_ImageSource]


@dataclass(slots=True)
class _SplitSegment:
    """A byte range of the merged super image backed by one part."""

    start: int
    end: int
    kind: str  # "raw" | "fill"
    part: int
    data_offset: int = 0
    pattern: bytes = b"\0\0\0\0"


@dataclass(slots=True)
class _SparsePartInfo:
    source: _ImageSource
    block_size: int
    total_blocks: int
    covered_blocks: int
    first_data_block: int
    segments: list[_SplitSegment]


@dataclass(slots=True)
class _Extent:
    sectors: int
    target_type: int
    target_data: int
    target_source: int


@dataclass(slots=True)
class _Partition:
    name: str
    extents: list[_Extent]

    @property
    def size(self) -> int:
        return sum(extent.sectors for extent in self.extents) * _SECTOR_SIZE


# ------------------------------------------------------------------ public API
def extract_partitions(
    root: Path,
    partitions: Sequence[str],
    output_dir: Path,
    *,
    settings: Settings,
    progress: ProgressCB,
    cancel: CancelToken,
) -> ExtractionResult:
    """Extract requested partitions from a downloaded file or directory."""
    requested = normalize_partitions(partitions)
    if not requested:
        raise DumpError(
            "No partitions were given. Example: `/gf <link> -dump boot,vendor`."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    files = _candidate_files(root)
    progress(None, "Scanning the download for Android images…")
    seven_zip = _find_7zip(settings)
    archives = _scan_archives(
        files, progress=progress, cancel=cancel, seven_zip=seven_zip
    )

    available: list[str] = []
    sources: list[str] = []
    found: list[str] = []
    missing = list(requested)
    outputs: list[Path] = []

    # 1. Files that are already plain partition images (inside archives too).
    direct = _direct_images(files, archives)
    if direct:
        available.extend(direct)
        for request in list(missing):
            actual = _pick_partition(request, direct)
            if not actual:
                continue
            source = _pick_image_source(direct[actual])
            destination = output_dir / f"{actual}.img"
            if not destination.exists():
                _materialize(
                    source,
                    destination,
                    label=f"Extracting {actual}.img",
                    cancel=cancel,
                    progress=progress,
                    seven_zip=seven_zip,
                )
            if destination.exists() and destination.stat().st_size > 0:
                _append_unique(outputs, destination)
                _append_unique(found, request)
                _append_unique(sources, "image")
                missing.remove(request)

    # 2. OTA payload.bin, either raw or inside an OTA zip/tar.
    payload_inputs = _find_payload_inputs(files, archives)
    payload_error = ""
    if missing and payload_inputs:
        binary = _find_payload_dumper(settings)
        if not binary:
            payload_error = (
                "payload-dumper-go is not installed. Install it from "
                "https://github.com/ssut/payload-dumper-go/releases and set "
                "PAYLOAD_DUMPER_PATH if it is not on PATH."
            )
        else:
            for index, source in enumerate(payload_inputs):
                if not missing:
                    break
                local_path = source.path
                temporary: Optional[Path] = None
                try:
                    if source.entry is not None:
                        temporary = output_dir / f"_payload_{index}.bin"
                        _materialize(
                            source,
                            temporary,
                            label="Extracting payload.bin",
                            cancel=cancel,
                            progress=progress,
                            seven_zip=seven_zip,
                        )
                        local_path = temporary

                    try:
                        parts = _payload_partitions(
                            binary, local_path, cancel=cancel, progress=progress
                        )
                    except DumpError as exc:
                        logger.warning("Could not read %s: %s", local_path, exc)
                        payload_error = str(exc)
                        continue

                    available.extend(parts)
                    matches: dict[str, str] = {}
                    for request in missing:
                        actual = _pick_partition(request, parts)
                        if actual:
                            matches[request] = actual
                    if not matches:
                        continue

                    actuals = sorted(set(matches.values()))
                    progress(None, f"Extracting {', '.join(actuals)} from payload.bin…")
                    try:
                        _extract_payload(
                            binary,
                            local_path,
                            actuals,
                            parts,
                            output_dir,
                            cancel=cancel,
                            progress=progress,
                        )
                    except DumpError as exc:
                        logger.warning("Payload extraction failed: %s", exc)
                        payload_error = str(exc)
                        continue

                    for request, actual in matches.items():
                        destination = output_dir / f"{actual}.img"
                        if destination.exists() and destination.stat().st_size > 0:
                            _append_unique(outputs, destination)
                            _append_unique(found, request)
                            _append_unique(sources, "payload.bin")
                            if request in missing:
                                missing.remove(request)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)

    # 3. super.img with dynamic/logical partitions.  ROMs sometimes ship the
    #    super image split into super.img.0, super.img.1, … parts; those are
    #    merged virtually so no 8 GB temporary file is needed.
    super_inputs = _find_super_inputs(files, archives)
    super_error = ""
    if missing and super_inputs:
        for index, source in enumerate(super_inputs):
            if not missing:
                break
            if isinstance(source, _SplitSuper):
                reader: Optional[_SplitSparseReader] = None
                try:
                    reader = _open_split_super(
                        source.parts, cancel=cancel, progress=progress
                    )
                    super_parts = _read_super_metadata(
                        reader, cancel=cancel, progress=progress
                    )
                except DumpError as exc:
                    logger.warning("Could not read the split super image: %s", exc)
                    super_error = str(exc)
                    if reader is not None:
                        reader.close()
                    continue

                try:
                    available.extend(super_parts)
                    matches: dict[str, str] = {}
                    for request in missing:
                        actual = _pick_partition(request, super_parts)
                        if actual:
                            matches[request] = actual
                    if not matches:
                        continue

                    actuals = sorted(set(matches.values()))
                    progress(
                        None,
                        f"Extracting {', '.join(actuals)} from the split super image…",
                    )
                    destinations = _extract_split_partitions(
                        reader,
                        super_parts,
                        actuals,
                        output_dir,
                        cancel=cancel,
                        progress=progress,
                    )
                    for request, actual in matches.items():
                        destination = destinations.get(actual)
                        if (
                            destination is not None
                            and destination.exists()
                            and destination.stat().st_size > 0
                        ):
                            _append_unique(outputs, destination)
                            _append_unique(found, request)
                            _append_unique(sources, "super.img")
                            if request in missing:
                                missing.remove(request)
                except DumpError as exc:
                    logger.warning("Could not extract from the split super image: %s", exc)
                    super_error = str(exc)
                finally:
                    if reader is not None:
                        reader.close()
                continue

            local_path = source.path
            temporary: Optional[Path] = None
            try:
                if source.entry is not None:
                    temporary = output_dir / f"_super_{index}.img"
                    try:
                        _materialize(
                            source,
                            temporary,
                            label="Extracting super.img",
                            cancel=cancel,
                            progress=progress,
                            seven_zip=seven_zip,
                        )
                    except DumpError as exc:
                        logger.warning("Could not extract super.img: %s", exc)
                        super_error = str(exc)
                        continue
                    local_path = temporary

                try:
                    super_parts = _read_super_partitions(
                        local_path, cancel=cancel, progress=progress
                    )
                except DumpError as exc:
                    logger.warning("Could not parse %s: %s", local_path, exc)
                    super_error = str(exc)
                    continue

                available.extend(super_parts)
                matches: dict[str, str] = {}
                for request in missing:
                    actual = _pick_partition(request, super_parts)
                    if actual:
                        matches[request] = actual

                for request, actual in matches.items():
                    destination = output_dir / f"{actual}.img"
                    if destination.exists() and destination.stat().st_size > 0:
                        _append_unique(outputs, destination)
                        _append_unique(found, request)
                        _append_unique(sources, "super.img")
                        if request in missing:
                            missing.remove(request)
                        continue
                    try:
                        _extract_super_partition(
                            local_path,
                            super_parts[actual],
                            destination,
                            label=f"Extracting {actual}.img",
                            cancel=cancel,
                            progress=progress,
                        )
                    except DumpError as exc:
                        logger.warning("Could not extract %s: %s", actual, exc)
                        super_error = str(exc)
                        continue
                    if destination.exists() and destination.stat().st_size > 0:
                        _append_unique(outputs, destination)
                        _append_unique(found, request)
                        _append_unique(sources, "super.img")
                        if request in missing:
                            missing.remove(request)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    available = sorted(set(available))
    if not outputs:
        raise DumpError(_missing_message(requested, available, payload_error, super_error))

    progress(100.0, f"Extracted {len(outputs)} partition image(s).")
    return ExtractionResult(
        files=outputs,
        found=found,
        missing=[name for name in requested if name not in found],
        available=available,
        sources=sorted(set(sources)),
    )


# --------------------------------------------------------------------- helpers
def _append_unique(items: list, value) -> None:
    if value not in items:
        items.append(value)


def _strip_slot(name: str) -> str:
    if len(name) > 2 and name[-2] == "_" and name[-1] in {"a", "b"}:
        return name[:-2]
    return name


def _pick_partition(request: str, names: Iterable[str]) -> Optional[str]:
    """Pick the best matching partition for a user-supplied name.

    Exact names win, then the usual ``_a``/``_b`` slot variants.  A different
    partition such as ``vendor_boot`` must not satisfy a request for ``vendor``;
    otherwise a missing partition silently produces the wrong image.
    """
    # Partition names become file names, so never trust metadata or manifest
    # names that could escape the output directory.
    candidates = {
        str(name).lower(): str(name)
        for name in names
        if PARTITION_NAME_RE.fullmatch(str(name).lower())
    }
    request = request.lower()

    if request in candidates:
        return candidates[request]

    base = _strip_slot(request)
    if base != request and base in candidates:
        return candidates[base]

    if base != request:
        # An explicitly requested slot only matches that slot (or the raw name).
        return None

    for suffix in ("_a", "_b"):
        candidate = request + suffix
        if candidate in candidates:
            return candidates[candidate]
    return None


def _candidate_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.exists():
        raise DumpError("The download directory disappeared.")
    files = [path for path in root.rglob("*") if path.is_file() and path.stat().st_size > 0]
    files.sort(key=lambda path: str(path).lower())
    return files


def _archive_kind(path: Path) -> Optional[str]:
    lower = path.name.lower()
    if lower.endswith((".zip", ".apk", ".jar")):
        return "zip"
    if lower.endswith(_ARCHIVE_SUFFIXES):
        return "tar"
    if lower.endswith(_SEVEN_ZIP_SUFFIXES):
        return "7z" if lower.endswith(".7z") else "rar"
    try:
        with path.open("rb") as handle:
            magic = handle.read(8)
    except OSError:
        return None
    if magic.startswith(b"PK"):
        return "zip"
    if magic.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if magic.startswith(b"Rar!\x1a\x07"):
        return "rar"
    return None


def find_7zip(settings: Settings) -> Optional[str]:
    """Return the 7-Zip executable, if one can be found."""
    return _find_7zip(settings)


def _find_7zip(settings: Settings) -> Optional[str]:
    configured = (settings.seven_zip_path or "7z").strip()
    candidates = [configured, "7z", "7zz", "7za"]
    if os.name == "nt":
        candidates.extend(["7z.exe", "7zz.exe"])
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(str(Path(base) / "7-Zip" / "7z.exe"))
    candidates.append(str(Path("tools") / ("7z.exe" if os.name == "nt" else "7z")))
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
        path = Path(candidate)
        if path.is_file():
            return str(path)
    return None


def _scan_archives(
    files: Sequence[Path],
    *,
    progress: ProgressCB,
    cancel: CancelToken,
    seven_zip: Optional[str] = None,
) -> dict[Path, list[_ArchiveEntry]]:
    archives: dict[Path, list[_ArchiveEntry]] = {}
    for path in files:
        kind = _archive_kind(path)
        if not kind:
            continue
        if kind in {"7z", "rar"} and not seven_zip:
            raise DumpError(
                "This download is a .7z or .rar archive, but 7-Zip is not "
                "installed. Install 7-Zip (the `7z` command), or set "
                "SEVEN_ZIP_PATH to its executable, then retry."
            )
        cancel.check()
        progress(None, f"Scanning {path.name}…")
        try:
            entries = _list_archive(
                path, kind, cancel=cancel, seven_zip=seven_zip
            )
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            logger.warning("Could not read archive %s: %s", path, exc)
            continue
        if entries:
            archives[path] = entries
    return archives


def _list_archive(
    path: Path,
    kind: str,
    *,
    cancel: CancelToken,
    seven_zip: Optional[str] = None,
) -> list[_ArchiveEntry]:
    entries: list[_ArchiveEntry] = []
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                cancel.check()
                if info.is_dir() or info.file_size <= 0:
                    continue
                entries.append(
                    _ArchiveEntry(path, info.filename, "zip", size=info.file_size)
                )
        return entries

    if kind == "tar":
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                cancel.check()
                if not member.isfile() or member.size <= 0:
                    continue
                entries.append(
                    _ArchiveEntry(path, member.name, "tar", size=member.size)
                )
        return entries

    if not seven_zip:
        raise DumpError(
            "7-Zip is required to read .7z and .rar archives. Install it and "
            "retry, or set SEVEN_ZIP_PATH to the 7z executable."
        )
    return _list_7z(path, kind, seven_zip, cancel=cancel)


def _list_7z(
    path: Path,
    kind: str,
    binary: str,
    *,
    cancel: CancelToken,
) -> list[_ArchiveEntry]:
    command = [binary, "l", "-slt", "-sccUTF-8", "-bd", "--", str(path)]
    try:
        lines = _run_process(
            command,
            label="7-Zip",
            cancel=cancel,
            progress=lambda percent, detail: None,
            interval=10.0,
        )
    except DumpError as exc:
        lowered = str(exc).lower()
        if "password" in lowered or "encrypt" in lowered:
            raise DumpError(
                f"{path.name} is encrypted. Password-protected 7z/RAR archives "
                "are not supported; extract it first and upload the extracted "
                "files."
            ) from exc
        raise
    entries = _parse_7z_entries(path, kind, lines)
    if not entries:
        raise DumpError(
            f"7-Zip could not list any files in {path.name}. The archive may "
            "be empty, encrypted, or damaged."
        )
    return entries


def _parse_7z_entries(
    path: Path, kind: str, lines: Sequence[str]
) -> list[_ArchiveEntry]:
    entries: list[_ArchiveEntry] = []
    started = False
    current: dict[str, str] = {}

    def flush() -> None:
        name = current.get("Path", "")
        if not name:
            return
        while name.startswith("./"):
            name = name[2:]
        attributes = current.get("Attributes", "")
        folder = current.get("Folder", "") == "+"
        try:
            size = int(current.get("Size", "0").strip() or 0)
        except ValueError:
            size = 0
        if folder or attributes.startswith("D") or size <= 0:
            return
        entries.append(
            _ArchiveEntry(
                path,
                name.replace("\\", "/"),
                kind,
                size=size,
                encrypted=current.get("Encrypted", "") == "+",
            )
        )

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("----------"):
            started = True
            current = {}
            continue
        if not started:
            continue
        if not line.strip():
            flush()
            current = {}
            continue
        key, separator, value = line.partition(" = ")
        if not separator:
            continue
        if key == "Path":
            flush()
            current = {"Path": value}
        elif current:
            current[key] = value
    flush()
    return entries


def _has_magic(path: Path, magic: bytes) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(magic)) == magic
    except OSError:
        return False


def _direct_images(
    files: Sequence[Path],
    archives: dict[Path, list[_ArchiveEntry]],
) -> dict[str, list[_ImageSource]]:
    images: dict[str, list[_ImageSource]] = {}
    for path in files:
        if path.suffix.lower() != ".img" or _archive_kind(path):
            continue
        images.setdefault(path.stem.lower(), []).append(_ImageSource(path))

    for entries in archives.values():
        for entry in entries:
            if not entry.basename.lower().endswith(".img"):
                continue
            stem = Path(entry.basename).stem.lower()
            images.setdefault(stem, []).append(_ImageSource(entry.archive, entry))
    return images


def _pick_image_source(sources: Sequence[_ImageSource]) -> _ImageSource:
    # Plain files are faster than extracting from an archive.
    for source in sources:
        if source.entry is None:
            return source
    return sources[0]


def _find_payload_inputs(
    files: Sequence[Path],
    archives: dict[Path, list[_ArchiveEntry]],
) -> list[_ImageSource]:
    inputs: list[_ImageSource] = []
    seen: set[tuple[str, str]] = set()

    def add(source: _ImageSource) -> None:
        key = (
            str(source.path),
            source.entry.name if source.entry is not None else "",
        )
        if key not in seen:
            seen.add(key)
            inputs.append(source)

    for path in files:
        if path.name.lower() == "payload.bin" or _has_magic(path, _PAYLOAD_MAGIC):
            add(_ImageSource(path))

    for path, entries in archives.items():
        for entry in entries:
            if entry.basename.lower() != "payload.bin":
                continue
            if entry.kind == "zip":
                # payload-dumper-go reads payload.bin directly from OTA zips,
                # including a deflated entry (it extracts it to a temp file).
                add(_ImageSource(path))
            else:
                add(_ImageSource(path, entry))
    return inputs


def _find_super_inputs(
    files: Sequence[Path],
    archives: dict[Path, list[_ArchiveEntry]],
) -> list[_ImageSource | _SplitSuper]:
    inputs: list[_ImageSource | _SplitSuper] = []
    for path in files:
        if _SPLIT_PART_RE.match(path.name):
            continue
        if _looks_like_super(path):
            inputs.append(_ImageSource(path))

    for entries in archives.values():
        for entry in entries:
            name = entry.basename.lower()
            if _SPLIT_PART_RE.match(entry.basename):
                continue
            if name == "super.img" or (
                name.startswith("super") and name.endswith(".img")
            ):
                inputs.append(_ImageSource(entry.archive, entry))

    # Prefer a generic super.img over slot-specific copies.
    def priority(source: _ImageSource) -> tuple[int, str]:
        name = (
            source.entry.basename.lower()
            if source.entry is not None
            else source.path.name.lower()
        )
        if name == "super.img":
            return (0, name)
        if name == "super_a.img":
            return (1, name)
        if name == "super_b.img":
            return (2, name)
        return (3, name)

    inputs.sort(key=priority)
    # A plain super.img always wins over split parts, so try it first.
    inputs.extend(_find_split_supers(files, archives))
    return inputs


def _find_split_supers(
    files: Sequence[Path],
    archives: dict[Path, list[_ArchiveEntry]],
) -> list[_SplitSuper]:
    """Group ``super.img.0``, ``super.img.1``, … parts into split images."""
    groups: dict[tuple[str, ...], dict[int, _ImageSource]] = {}

    for path in files:
        match = _SPLIT_PART_RE.match(path.name)
        if not match or match.group("stem").lower() != "super":
            continue
        key = ("file", str(path.parent))
        groups.setdefault(key, {})[int(match.group("index"))] = _ImageSource(path)

    for archive, entries in archives.items():
        for entry in entries:
            match = _SPLIT_PART_RE.match(entry.basename)
            if not match or match.group("stem").lower() != "super":
                continue
            parent = str(PurePosixPath(entry.name).parent)
            key = ("archive", str(archive), parent)
            groups.setdefault(key, {})[int(match.group("index"))] = _ImageSource(
                archive, entry
            )

    split: list[_SplitSuper] = []
    for parts in groups.values():
        indexes = sorted(parts)
        if len(indexes) < 2:
            continue
        if indexes != list(range(len(indexes))):
            logger.warning(
                "Ignoring incomplete split super image: parts %s", indexes
            )
            continue
        split.append(_SplitSuper([parts[index] for index in indexes]))
    return split


def _looks_like_super(path: Path) -> bool:
    try:
        if path.stat().st_size < _RESERVED_BYTES + _GEOMETRY_SIZE:
            return False
    except OSError:
        return False
    if path.suffix.lower() not in {".img", ".bin"} and "super" not in path.name.lower():
        return False
    try:
        reader = _open_image(path)
    except (OSError, DumpError):
        return False
    try:
        data = reader.read_at(_RESERVED_BYTES, 4)
        if len(data) < 4:
            return False
        return struct.unpack("<I", data)[0] == _GEOMETRY_MAGIC
    finally:
        reader.close()


def _materialize(
    source: _ImageSource,
    destination: Path,
    *,
    label: str,
    cancel: CancelToken,
    progress: ProgressCB,
    seven_zip: Optional[str] = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        if source.entry is None:
            _copy_path(
                source.path,
                partial,
                label=label,
                cancel=cancel,
                progress=progress,
            )
        else:
            _extract_entry(
                source.entry,
                partial,
                label=label,
                cancel=cancel,
                progress=progress,
                seven_zip=seven_zip,
            )
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _copy_path(
    source: Path,
    destination: Path,
    *,
    label: str,
    cancel: CancelToken,
    progress: ProgressCB,
) -> None:
    total = source.stat().st_size
    done = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            cancel.check()
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            done += len(chunk)
            percent = done / total * 100 if total else None
            progress(percent, f"{label} · {format_progress(done, total)}")


def _extract_entry(
    entry: _ArchiveEntry,
    destination: Path,
    *,
    label: str,
    cancel: CancelToken,
    progress: ProgressCB,
    seven_zip: Optional[str] = None,
) -> None:
    if entry.kind in {"7z", "rar"}:
        if entry.encrypted:
            raise DumpError(
                f"{entry.archive.name} is encrypted. Password-protected 7z/RAR "
                "archives are not supported; extract it first and upload the "
                "extracted files."
            )
        if not seven_zip:
            raise DumpError(
                "7-Zip is required to read .7z and .rar archives. Install it "
                "and retry, or set SEVEN_ZIP_PATH to the 7z executable."
            )
        _stream_7z_member(
            seven_zip,
            entry,
            destination,
            label=label,
            cancel=cancel,
            progress=progress,
        )
        return

    if entry.kind == "zip":
        with zipfile.ZipFile(entry.archive) as archive:
            info = archive.getinfo(entry.name)
            total = info.file_size
            done = 0
            with archive.open(info) as src, destination.open("wb") as dst:
                while True:
                    cancel.check()
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
                    done += len(chunk)
                    percent = done / total * 100 if total else None
                    progress(percent, f"{label} · {format_progress(done, total)}")
        return

    with tarfile.open(entry.archive, "r:*") as archive:
        member = archive.getmember(entry.name)
        total = member.size
        done = 0
        src = archive.extractfile(member)
        if src is None:
            raise DumpError(f"Could not read {entry.name} from {entry.archive.name}.")
        with src, destination.open("wb") as dst:
            while True:
                cancel.check()
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                percent = done / total * 100 if total else None
                progress(percent, f"{label} · {format_progress(done, total)}")


def _stream_7z_member(
    binary: str,
    entry: _ArchiveEntry,
    destination: Path,
    *,
    label: str,
    cancel: CancelToken,
    progress: ProgressCB,
) -> None:
    """Stream one archive member to disk without unpacking the whole archive."""
    command = [
        binary,
        "x",
        "-so",
        "-sccUTF-8",
        "-bd",
        "-y",
        "-spd",
        "--",
        str(entry.archive),
        entry.name,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise DumpError(f"Could not start 7-Zip: {exc}") from exc

    errors: list[str] = []
    failure: list[MirrorError] = []
    stop = threading.Event()

    def read_errors() -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            errors.append(line)
            if len(errors) > 40:
                del errors[:-40]

    def watchdog() -> None:
        while not stop.wait(2.0):
            try:
                cancel.check()
            except MirrorError as exc:
                failure.append(exc)
                _terminate(process)
                return

    reader = threading.Thread(target=read_errors, daemon=True)
    watcher = threading.Thread(target=watchdog, daemon=True)
    reader.start()
    watcher.start()
    done = 0
    try:
        assert process.stdout is not None
        with destination.open("wb") as output:
            while True:
                cancel.check()
                chunk = process.stdout.read(_COPY_CHUNK)
                if not chunk:
                    break
                output.write(chunk)
                done += len(chunk)
                percent = done / entry.size * 100 if entry.size else None
                progress(percent, f"{label} · {format_progress(done, entry.size)}")
        code = process.wait(timeout=60)
    except (JobCancelled, JobTimeout):
        _terminate(process)
        raise
    except subprocess.TimeoutExpired:
        _terminate(process)
        raise DumpError("7-Zip did not exit after finishing.")
    finally:
        stop.set()
        watcher.join(timeout=5)
        reader.join(timeout=5)
        if process.poll() is None:
            _terminate(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    if failure:
        raise failure[0]
    if code != 0:
        details = " | ".join(errors[-4:]) or "no details from 7-Zip"
        if code == 1 and entry.size and destination.stat().st_size == entry.size:
            # Exit code 1 is a warning (for example, trailing bytes after the
            # archive). The requested member was still produced in full.
            logger.warning("7-Zip warning for %s: %s", entry.name, details[:300])
            return
        lowered = details.lower()
        if "password" in lowered or "encrypt" in lowered:
            raise DumpError(
                f"{entry.archive.name} is encrypted. Password-protected 7z/RAR "
                "archives are not supported; extract it first and upload the "
                "extracted files."
            )
        raise DumpError(f"7-Zip failed (exit code {code}): {details[:600]}")


# -------------------------------------------------------------------- payloads
def _find_payload_dumper(settings: Settings) -> Optional[str]:
    configured = (settings.payload_dumper_path or "payload-dumper-go").strip()
    candidates = [configured, "payload-dumper-go"]
    if os.name == "nt":
        candidates.append("payload-dumper-go.exe")
    candidates.append(str(Path("tools") / "payload-dumper-go"))
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
        path = Path(candidate)
        if path.is_file():
            return str(path)
    return None


def _payload_partitions(
    binary: str,
    input_path: Path,
    *,
    cancel: CancelToken,
    progress: ProgressCB,
) -> dict[str, int]:
    progress(None, "Reading the OTA partition list…")
    lines = _run_process(
        [binary, "-l", "-m", str(input_path)],
        label="payload-dumper-go --list",
        cancel=cancel,
        progress=progress,
        interval=10.0,
    )
    partitions: dict[str, int] = {}
    for line in lines:
        match = re.fullmatch(r"([A-Za-z0-9._-]+):(\d+)", line.strip())
        if match:
            parts = match.groups()
            partitions[parts[0]] = int(parts[1]) * 1024
    if not partitions:
        raise DumpError(
            "payload-dumper-go could not list any partitions in this file. "
            "It may not be a valid Android OTA payload."
        )
    logger.info(
        "Payload lists %d partitions: %s",
        len(partitions),
        ", ".join(sorted(partitions)),
    )
    return partitions


def _extract_payload(
    binary: str,
    input_path: Path,
    partitions: Sequence[str],
    sizes: dict[str, int],
    output_dir: Path,
    *,
    cancel: CancelToken,
    progress: ProgressCB,
) -> None:
    workers = max(1, min(4, os.cpu_count() or 1))
    command = [
        binary,
        "-p",
        ",".join(partitions),
        "-o",
        str(output_dir),
        "-c",
        str(workers),
        "-m",
        str(input_path),
    ]

    selected = set(partitions)
    total_bytes = sum(sizes.get(name, 0) for name in partitions)
    percentages: dict[str, float] = {}

    def on_line(line: str) -> None:
        match = re.fullmatch(r"([A-Za-z0-9._-]+):(\d+)", line.strip())
        if not match or match.group(1) not in selected:
            return
        name, percent_text = match.groups()
        percent = max(0.0, min(100.0, float(percent_text)))
        percentages[name] = percent
        if total_bytes:
            done = sum(
                sizes.get(item, 0) * percentages.get(item, 0.0) / 100.0
                for item in partitions
            )
            detail = format_progress(int(done), total_bytes)
        else:
            detail = ""
        active = max(percentages, key=percentages.get)
        message = f"Extracting {active}.img"
        if detail:
            message += f" · {detail}"
        progress(
            sum(percentages.values()) / len(partitions) if partitions else None,
            message,
        )

    try:
        _run_process(
            command,
            label="payload-dumper-go",
            cancel=cancel,
            progress=progress,
            interval=10.0,
            on_line=on_line,
        )
    except DumpError:
        raise

    for name in partitions:
        path = output_dir / f"{name}.img"
        if not path.exists() or path.stat().st_size == 0:
            raise DumpError(
                f"payload-dumper-go did not produce {name}.img. The payload may "
                "be an incremental OTA that needs the previous firmware."
            )


def _run_process(
    command: Sequence[str],
    *,
    label: str,
    cancel: CancelToken,
    progress: ProgressCB,
    interval: float,
    on_line: Optional[Callable[[str], None]] = None,
) -> list[str]:
    logger.info("Running %s", label)
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise DumpError(f"Could not start {label}: {exc}") from exc

    lines: list[str] = []
    tail: list[str] = []
    failure: list[MirrorError] = []
    stop = threading.Event()
    last_update = time.monotonic()

    def watchdog() -> None:
        while not stop.wait(2.0):
            try:
                cancel.check()
            except MirrorError as exc:
                failure.append(exc)
                _terminate(process)
                return

    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()

    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            lines.append(line)
            tail.append(line)
            if len(tail) > 40:
                del tail[:-40]
            cancel.check()
            if on_line:
                on_line(line)
            now = time.monotonic()
            if now - last_update >= interval:
                last_update = now
                progress(None, f"{label} is working…")
        code = process.wait(timeout=60)
    except (JobCancelled, JobTimeout):
        _terminate(process)
        raise
    except subprocess.TimeoutExpired:
        _terminate(process)
        raise DumpError(f"{label} did not exit after finishing.")
    finally:
        stop.set()
        watcher.join(timeout=5)
        if process.poll() is None:
            _terminate(process)

    if failure:
        raise failure[0]
    if code != 0:
        details = " | ".join(tail[-4:]) or f"no details from {label}"
        raise DumpError(f"{label} failed (exit code {code}): {details[:600]}")
    # Return every line, not just the tail: partition listings can easily
    # exceed 40 entries and the old tail limit silently hid the first ones.
    return lines


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


# ---------------------------------------------------------------- super images
class _RawReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.size = path.stat().st_size
        self._handle = path.open("rb")

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size <= 0:
            return b""
        self._handle.seek(offset)
        return self._handle.read(size)

    def close(self) -> None:
        self._handle.close()


class _SparseReader:
    """Read a libsparse image as if it were a raw image."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = path.open("rb")
        header = self._handle.read(28)
        if len(header) < 28:
            self._handle.close()
            raise DumpError("The sparse image is truncated.")
        (
            magic,
            _major,
            _minor,
            file_header_size,
            chunk_header_size,
            block_size,
            total_blocks,
            total_chunks,
            _checksum,
        ) = struct.unpack("<IHHHHIIII", header)
        if magic != _SPARSE_MAGIC:
            self._handle.close()
            raise DumpError("The sparse image has an invalid header.")
        if block_size <= 0 or file_header_size < 28 or chunk_header_size < 12:
            self._handle.close()
            raise DumpError("The sparse image has invalid geometry.")

        self.block_size = block_size
        self.size = total_blocks * block_size
        self._segments: list[tuple[int, int, str, object]] = []
        self._starts: list[int] = []

        position = file_header_size
        block_index = 0
        for _ in range(total_chunks):
            self._handle.seek(position)
            chunk_header = self._handle.read(chunk_header_size)
            if len(chunk_header) < 12:
                self._handle.close()
                raise DumpError("The sparse image is truncated.")
            chunk_type, _reserved, chunk_blocks, total_size = struct.unpack(
                "<HHII", chunk_header[:12]
            )
            if total_size < chunk_header_size:
                self._handle.close()
                raise DumpError("The sparse image has a corrupt chunk.")
            data_start = position + chunk_header_size
            output_start = block_index * block_size
            output_end = output_start + chunk_blocks * block_size

            if chunk_type == 0xCAC1:  # raw
                self._segments.append((output_start, output_end, "raw", data_start))
            elif chunk_type == 0xCAC2:  # fill
                self._handle.seek(data_start)
                fill = self._handle.read(4) or b"\0\0\0\0"
                self._segments.append((output_start, output_end, "fill", fill))
            elif chunk_type == 0xCAC3:  # don't care
                self._segments.append((output_start, output_end, "zero", None))
            elif chunk_type == 0xCAC4:  # crc32
                pass
            else:
                self._handle.close()
                raise DumpError(
                    f"The sparse image uses an unknown chunk type 0x{chunk_type:x}."
                )

            block_index += chunk_blocks
            position += total_size
            if block_index > total_blocks:
                self._handle.close()
                raise DumpError("The sparse image has too many blocks.")

        self._starts = [segment[0] for segment in self._segments]

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size <= 0:
            return b""
        end = min(self.size, offset + size)
        if end <= offset:
            return b""
        output = bytearray()
        index = max(0, bisect.bisect_right(self._starts, offset) - 1)
        while index < len(self._segments) and self._segments[index][0] < end:
            start, stop, kind, payload = self._segments[index]
            index += 1
            if stop <= offset:
                continue
            overlap_start = max(start, offset)
            overlap_end = min(stop, end)
            length = overlap_end - overlap_start
            if length <= 0:
                continue
            if kind == "raw":
                self._handle.seek(int(payload) + (overlap_start - start))
                output.extend(self._handle.read(length))
            elif kind == "fill":
                pattern = payload or b"\0\0\0\0"
                repeat = (length + 3) // 4
                output.extend((pattern * repeat)[:length])
            else:
                output.extend(b"\0" * length)
            if overlap_end >= end:
                break
        return bytes(output)

    def close(self) -> None:
        self._handle.close()


def _open_image(path: Path) -> _RawReader | _SparseReader:
    with path.open("rb") as handle:
        magic = handle.read(4)
    if len(magic) == 4 and struct.unpack("<I", magic)[0] == _SPARSE_MAGIC:
        return _SparseReader(path)
    return _RawReader(path)


# ------------------------------------------------------------- split supers
def _source_size(source: _ImageSource) -> int:
    if source.entry is not None:
        return source.entry.size
    return source.path.stat().st_size


@contextmanager
def _source_stream(source: _ImageSource):
    """Open a local file or an archive member as a binary stream."""
    if source.entry is None:
        with source.path.open("rb") as handle:
            yield handle
        return

    entry = source.entry
    if entry.kind == "zip":
        with zipfile.ZipFile(entry.archive) as archive:
            with archive.open(archive.getinfo(entry.name)) as handle:
                yield handle
        return
    if entry.kind == "tar":
        with tarfile.open(entry.archive, "r:*") as archive:
            handle = archive.extractfile(archive.getmember(entry.name))
            if handle is None:
                raise DumpError(f"Could not read {entry.name} from the archive.")
            with handle:
                yield handle
        return
    raise DumpError(
        "Split super images inside .7z/.rar archives are not supported yet. "
        "Extract the archive first and retry."
    )


def _scan_sparse_part(
    source: _ImageSource,
    index: int,
    *,
    cancel: CancelToken,
) -> _SparsePartInfo:
    """Index the chunks of one sparse ``super.img.N`` part."""
    with _source_stream(source) as stream:
        header = stream.read(28)
        if len(header) < 28:
            raise DumpError("A split super image part is truncated.")
        (
            magic,
            _major,
            _minor,
            file_header_size,
            chunk_header_size,
            block_size,
            total_blocks,
            total_chunks,
            _checksum,
        ) = struct.unpack("<IHHHHIIII", header)
        if magic != _SPARSE_MAGIC:
            raise DumpError(
                "A split super image mixed sparse and raw parts; this layout "
                "is not supported."
            )
        if (
            block_size < _SECTOR_SIZE
            or block_size % _SECTOR_SIZE
            or file_header_size < 28
            or chunk_header_size < 12
        ):
            raise DumpError("A split super image part has invalid geometry.")
        if total_chunks <= 0 or total_chunks > 1_000_000:
            raise DumpError("A split super image part has an invalid chunk count.")

        segments: list[_SplitSegment] = []
        position = file_header_size
        block = 0
        first_data_block: Optional[int] = None
        for _chunk_index in range(total_chunks):
            cancel.check()
            stream.seek(position)
            chunk_header = stream.read(12)
            if len(chunk_header) < 12:
                raise DumpError("A split super image part is truncated.")
            chunk_type, _reserved, chunk_blocks, total_size = struct.unpack(
                "<HHII", chunk_header
            )
            if chunk_blocks <= 0 or total_size < chunk_header_size:
                raise DumpError("A split super image part has a corrupt chunk.")
            data_start = position + chunk_header_size

            if chunk_type == 0xCAC1:  # raw
                if total_size < chunk_header_size + chunk_blocks * block_size:
                    raise DumpError(
                        "A split super image part has a truncated raw chunk."
                    )
                if first_data_block is None:
                    first_data_block = block
                segments.append(
                    _SplitSegment(
                        start=block * block_size,
                        end=(block + chunk_blocks) * block_size,
                        kind="raw",
                        part=index,
                        data_offset=data_start,
                    )
                )
            elif chunk_type == 0xCAC2:  # fill
                stream.seek(data_start)
                pattern = stream.read(4)
                if len(pattern) < 4:
                    raise DumpError("A split super image part is truncated.")
                # Zero fills are gaps; skipping them lets later parts win in
                # overlay-style split images.
                if pattern != b"\0\0\0\0":
                    if first_data_block is None:
                        first_data_block = block
                    segments.append(
                        _SplitSegment(
                            start=block * block_size,
                            end=(block + chunk_blocks) * block_size,
                            kind="fill",
                            part=index,
                            pattern=pattern,
                        )
                    )
            elif chunk_type in (0xCAC3, 0xCAC4):  # don't care / crc32
                pass
            else:
                raise DumpError(
                    "A split super image part uses an unknown chunk type "
                    f"0x{chunk_type:x}."
                )

            block += chunk_blocks
            position += total_size

        if block > total_blocks:
            raise DumpError(
                "A split super image part contains more blocks than its header "
                "declares."
            )
        return _SparsePartInfo(
            source=source,
            block_size=block_size,
            total_blocks=total_blocks,
            covered_blocks=block,
            first_data_block=(
                total_blocks if first_data_block is None else first_data_block
            ),
            segments=segments,
        )


def _merge_split_segments(
    per_part: Sequence[list[_SplitSegment]],
) -> list[_SplitSegment]:
    """Flatten part segments, letting later parts overwrite earlier ones."""
    owned: list[_SplitSegment] = []
    # Sequential flashing writes the parts in order, so the last part wins.
    for segments in reversed(per_part):
        for segment in sorted(segments, key=lambda item: item.start):
            pieces = [segment]
            for existing in owned:
                if not pieces:
                    break
                if existing.end <= segment.start or existing.start >= segment.end:
                    continue
                next_pieces: list[_SplitSegment] = []
                for piece in pieces:
                    if existing.end <= piece.start or existing.start >= piece.end:
                        next_pieces.append(piece)
                        continue
                    if piece.start < existing.start:
                        next_pieces.append(
                            _SplitSegment(
                                start=piece.start,
                                end=existing.start,
                                kind=piece.kind,
                                part=piece.part,
                                data_offset=piece.data_offset,
                                pattern=piece.pattern,
                            )
                        )
                    if piece.end > existing.end:
                        next_pieces.append(
                            _SplitSegment(
                                start=existing.end,
                                end=piece.end,
                                kind=piece.kind,
                                part=piece.part,
                                data_offset=piece.data_offset
                                + (existing.end - piece.start),
                                pattern=piece.pattern,
                            )
                        )
                pieces = next_pieces
            for piece in pieces:
                bisect.insort(owned, piece, key=lambda item: item.start)
    return owned


class _SplitSparseReader:
    """Present split sparse/raw parts as one random-access image."""

    def __init__(
        self,
        parts: Sequence[_ImageSource],
        segments: Sequence[_SplitSegment],
        size: int,
    ) -> None:
        self.parts = list(parts)
        self.segments = list(segments)
        self.starts = [segment.start for segment in self.segments]
        self.size = size
        self._streams: dict[int, tuple[ExitStack, object]] = {}

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size <= 0:
            return b""
        end = min(self.size, offset + size)
        if end <= offset:
            return b""

        output = bytearray()
        position = offset
        index = max(0, bisect.bisect_right(self.starts, position) - 1)
        while index < len(self.segments) and position < end:
            segment = self.segments[index]
            index += 1
            if segment.end <= position:
                continue
            if segment.start > position:
                gap_end = min(segment.start, end)
                output.extend(b"\0" * (gap_end - position))
                position = gap_end
                if position >= end:
                    break
                if segment.start > position:
                    continue

            start = max(segment.start, position)
            stop = min(segment.end, end)
            length = stop - start
            if length <= 0:
                continue
            if segment.kind == "fill":
                repeat = (length + 3) // 4
                output.extend((segment.pattern * repeat)[:length])
            else:
                stream = self._stream(segment.part)
                stream.seek(segment.data_offset + (start - segment.start))
                data = stream.read(length)
                if len(data) < length:
                    raise DumpError("A split super image part is truncated.")
                output.extend(data)
            position = stop

        if position < end:
            output.extend(b"\0" * (end - position))
        return bytes(output)

    def _stream(self, index: int):
        cached = self._streams.get(index)
        if cached is not None:
            return cached[1]
        stack = ExitStack()
        stream = stack.enter_context(_source_stream(self.parts[index]))
        self._streams[index] = (stack, stream)
        return stream

    def close(self) -> None:
        for stack, _stream in self._streams.values():
            stack.close()
        self._streams.clear()


def _open_split_super(
    parts: Sequence[_ImageSource],
    *,
    cancel: CancelToken,
    progress: ProgressCB,
) -> _SplitSparseReader:
    if len(parts) < 2:
        raise DumpError("A split super image needs at least two parts.")

    with _source_stream(parts[0]) as stream:
        magic = stream.read(4)

    if magic != struct.pack("<I", _SPARSE_MAGIC):
        # Raw super images split with ``split``: just concatenate them.
        segments: list[_SplitSegment] = []
        cursor = 0
        for index, source in enumerate(parts):
            cancel.check()
            size = _source_size(source)
            if size <= 0:
                raise DumpError("A split super image part is empty.")
            segments.append(
                _SplitSegment(
                    start=cursor,
                    end=cursor + size,
                    kind="raw",
                    part=index,
                )
            )
            cursor += size
            progress(
                None,
                f"Reading split super image part {index + 1}/{len(parts)}…",
            )
        return _SplitSparseReader(parts, segments, cursor)

    infos: list[_SparsePartInfo] = []
    for index, source in enumerate(parts):
        name = (
            source.entry.basename
            if source.entry is not None
            else source.path.name
        )
        progress(
            None,
            f"Reading split super image part {index + 1}/{len(parts)} ({name})…",
        )
        infos.append(_scan_sparse_part(source, index, cancel=cancel))

    block_size = infos[0].block_size
    for info in infos[1:]:
        if info.block_size != block_size:
            raise DumpError("Split super image parts use different block sizes.")

    if len({info.total_blocks for info in infos}) == 1:
        total_blocks = infos[0].total_blocks
        coverages = [info.covered_blocks for info in infos]
        absolute = all(covered == total_blocks for covered in coverages)
        if not absolute and sum(coverages) != total_blocks:
            # Overlay parts may omit the trailing gap chunk.  A leading gap
            # in a later part then skips the region owned by earlier parts,
            # which is the fingerprint of absolute chunk positions.
            absolute = any(info.first_data_block > 0 for info in infos[1:])
        if absolute:
            offsets = [0] * len(infos)
        else:
            # Parts are sequential slices of one sparse stream, each with the
            # global block count copied into its header.
            cursor = 0
            offsets = []
            for covered in coverages:
                offsets.append(cursor)
                cursor += covered
            if cursor > total_blocks:
                raise DumpError(
                    "Split super image parts contain more blocks than the "
                    f"image declares ({cursor} > {total_blocks})."
                )
    else:
        # Each part is its own sparse segment.
        cursor = 0
        offsets = []
        for info in infos:
            offsets.append(cursor)
            cursor += info.total_blocks
        total_blocks = cursor

    per_part: list[list[_SplitSegment]] = []
    for info, offset in zip(infos, offsets):
        shift = offset * block_size
        per_part.append(
            [
                _SplitSegment(
                    start=segment.start + shift,
                    end=segment.end + shift,
                    kind=segment.kind,
                    part=segment.part,
                    data_offset=segment.data_offset,
                    pattern=segment.pattern,
                )
                for segment in info.segments
            ]
        )

    size = total_blocks * block_size
    if size <= 0 or size > 64 * 1024**3:
        raise DumpError("The split super image has an invalid size.")
    return _SplitSparseReader(parts, _merge_split_segments(per_part), size)


def _read_super_partitions(
    path: Path,
    *,
    cancel: CancelToken,
    progress: ProgressCB,
) -> dict[str, _Partition]:
    reader = _open_image(path)
    try:
        return _read_super_metadata(reader, cancel=cancel, progress=progress)
    finally:
        reader.close()


def _read_super_metadata(
    reader: _RawReader | _SparseReader | _SplitSparseReader,
    *,
    cancel: CancelToken,
    progress: ProgressCB,
) -> dict[str, _Partition]:
    progress(None, "Reading super.img metadata…")
    geometry = _read_geometry(reader)
    metadata_max_size, slot_count = geometry
    base = _RESERVED_BYTES + _GEOMETRY_SIZE * 2
    last_error: Optional[Exception] = None
    partitions: dict[str, _Partition] = {}

    # Metadata slot 0 describes the _a partitions and slot 1 the _b
    # partitions, so both slots have to be read to support "-dump vendor_b".
    for slot in range(slot_count):
        offsets = [
            base + metadata_max_size * slot,
            base + metadata_max_size * slot_count + metadata_max_size * slot,
        ]
        for offset in offsets:
            cancel.check()
            try:
                parsed = _parse_metadata(reader, offset, slot)
            except DumpError as exc:
                last_error = exc
                continue
            for name, partition in parsed.items():
                partitions.setdefault(name, partition)
            break

    if partitions:
        return partitions
    if last_error:
        raise DumpError(f"super.img metadata is invalid: {last_error}")
    raise DumpError("super.img has no logical partition metadata.")


def _read_geometry(
    reader: _RawReader | _SparseReader | _SplitSparseReader,
) -> tuple[int, int]:
    for offset in (_RESERVED_BYTES, _RESERVED_BYTES + _GEOMETRY_SIZE):
        data = reader.read_at(offset, 52)
        if len(data) < 52:
            continue
        magic, _struct_size, _checksum, metadata_max_size, slot_count, block_size = (
            struct.unpack("<II32sIII", data)
        )
        if magic != _GEOMETRY_MAGIC:
            continue
        if (
            metadata_max_size < 512
            or metadata_max_size > 64 * 1024 * 1024
            or slot_count < 1
            or slot_count > 4
            or block_size < _SECTOR_SIZE
            or block_size % _SECTOR_SIZE
        ):
            continue
        return metadata_max_size, slot_count
    raise DumpError("logical partition geometry is missing or invalid.")


def _parse_metadata(
    reader: _RawReader | _SparseReader | _SplitSparseReader, offset: int, slot: int
) -> dict[str, _Partition]:
    header = reader.read_at(offset, 128)
    if len(header) < 128:
        raise DumpError("metadata header is truncated.")
    magic, major, _minor, header_size = struct.unpack_from("<IHHI", header)
    if magic != _HEADER_MAGIC or major < 10:
        raise DumpError("metadata header magic is invalid.")
    if header_size < 128 or header_size > 4096:
        raise DumpError("metadata header size is invalid.")
    tables_size = struct.unpack_from("<I", header, 44)[0]
    if tables_size <= 0 or tables_size > 64 * 1024 * 1024:
        raise DumpError("metadata tables are invalid.")

    tables = reader.read_at(offset + header_size, tables_size)
    if len(tables) < tables_size:
        raise DumpError("metadata tables are truncated.")

    def descriptor(position: int) -> tuple[int, int, int]:
        return struct.unpack_from("<III", header, position)

    partition_offset, partition_count, partition_size = descriptor(80)
    extent_offset, extent_count, extent_size = descriptor(92)
    # The groups and block-device descriptors are not needed for extraction,
    # but their offsets are validated implicitly by the table read above.

    if partition_size < 52 or extent_size < 24:
        raise DumpError("metadata entry sizes are invalid.")
    if partition_offset + partition_count * partition_size > tables_size:
        raise DumpError("partition table is out of bounds.")
    if extent_offset + extent_count * extent_size > tables_size:
        raise DumpError("extent table is out of bounds.")

    partitions: dict[str, _Partition] = {}
    suffix = "_a" if slot == 0 else "_b"

    for index in range(partition_count):
        entry = tables[
            partition_offset + index * partition_size :
            partition_offset + index * partition_size + 52
        ]
        if len(entry) < 52:
            raise DumpError("partition entry is truncated.")
        raw_name = entry[:36].split(b"\0", 1)[0]
        name = raw_name.decode("ascii", "ignore").strip()
        if not name:
            continue
        attrs, first_extent, extent_total, _group = struct.unpack_from("<IIII", entry, 36)
        if attrs & _SLOT_SUFFIXED and not name.endswith(("_a", "_b")):
            name += suffix

        extents: list[_Extent] = []
        for extent_index in range(
            first_extent, first_extent + extent_total
        ):
            if extent_index >= extent_count:
                raise DumpError("partition extent index is out of bounds.")
            raw = tables[
                extent_offset + extent_index * extent_size :
                extent_offset + extent_index * extent_size + 24
            ]
            sectors, target_type, target_data, target_source = struct.unpack(
                "<QIQI", raw
            )
            if sectors <= 0:
                continue
            extents.append(
                _Extent(
                    sectors=sectors,
                    target_type=target_type,
                    target_data=target_data,
                    target_source=target_source,
                )
            )
        if extents:
            partitions[name] = _Partition(name=name, extents=extents)

    if not partitions:
        raise DumpError("super.img metadata contains no usable partitions.")
    return partitions


def _extract_super_partition(
    path: Path,
    partition: _Partition,
    destination: Path,
    *,
    label: str,
    cancel: CancelToken,
    progress: ProgressCB,
) -> None:
    reader = _open_image(path)
    try:
        _extract_super_partition_from(
            reader,
            partition,
            destination,
            label=label,
            cancel=cancel,
            progress=progress,
        )
    finally:
        reader.close()


def _extract_super_partition_from(
    reader: _RawReader | _SparseReader | _SplitSparseReader,
    partition: _Partition,
    destination: Path,
    *,
    label: str,
    cancel: CancelToken,
    progress: ProgressCB,
) -> None:
    total = partition.size
    done = 0
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with partial.open("wb") as output:
            for extent in partition.extents:
                remaining = extent.sectors * _SECTOR_SIZE
                offset = extent.target_data * _SECTOR_SIZE
                if extent.target_type == 1:  # dm-zero
                    while remaining:
                        cancel.check()
                        chunk = min(_COPY_CHUNK, remaining)
                        output.write(b"\0" * chunk)
                        remaining -= chunk
                        done += chunk
                        percent = done / total * 100 if total else None
                        progress(percent, f"{label} · {format_progress(done, total)}")
                    continue
                if extent.target_type != 0:
                    raise DumpError(
                        f"{partition.name} uses an unsupported extent type."
                    )
                if extent.target_source != 0:
                    raise DumpError(
                        f"{partition.name} lives on another block device and cannot "
                        "be extracted from this super image."
                    )
                while remaining:
                    cancel.check()
                    chunk_size = min(_COPY_CHUNK, remaining)
                    data = reader.read_at(offset, chunk_size)
                    if not data:
                        raise DumpError(
                            f"{partition.name} points outside the super image. "
                            "The download may be incomplete."
                        )
                    output.write(data)
                    offset += len(data)
                    remaining -= len(data)
                    done += len(data)
                    percent = done / total * 100 if total else None
                    progress(percent, f"{label} · {format_progress(done, total)}")
        if partial.stat().st_size == 0:
            raise DumpError(f"{partition.name} is empty.")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _extract_split_partitions(
    reader: _SplitSparseReader,
    partitions: dict[str, _Partition],
    actuals: Sequence[str],
    output_dir: Path,
    *,
    cancel: CancelToken,
    progress: ProgressCB,
) -> dict[str, Path]:
    """Extract several partitions from a merged split super in one pass.

    Ranges are visited in physical order so each part is decompressed from
    start to end only once, which matters for 7 GB zip members.
    """
    total = sum(partitions[name].size for name in actuals)
    done = 0
    ranges: list[tuple[int, int, str, int]] = []
    handles: dict[str, tuple[object, Path, Path]] = {}
    try:
        for actual in actuals:
            partition = partitions[actual]
            destination = output_dir / f"{actual}.img"
            partial = destination.with_suffix(destination.suffix + ".part")
            handle = partial.open("w+b")
            # Truncating creates a sparse file; zero extents stay as holes.
            handle.truncate(partition.size)
            handles[actual] = (handle, partial, destination)
            out_offset = 0
            for extent in partition.extents:
                length = extent.sectors * _SECTOR_SIZE
                if extent.target_type == 1:  # dm-zero
                    done += length
                elif extent.target_type != 0:
                    raise DumpError(
                        f"{partition.name} uses an unsupported extent type."
                    )
                elif extent.target_source != 0:
                    raise DumpError(
                        f"{partition.name} lives on another block device and cannot "
                        "be extracted from this super image."
                    )
                else:
                    ranges.append(
                        (extent.target_data * _SECTOR_SIZE, length, actual, out_offset)
                    )
                out_offset += length

        ranges.sort(key=lambda item: item[0])
        for offset, length, actual, out_offset in ranges:
            cancel.check()
            handle = handles[actual][0]
            position = offset
            remaining = length
            while remaining:
                cancel.check()
                chunk = min(_COPY_CHUNK, remaining)
                data = reader.read_at(position, chunk)
                if not data:
                    raise DumpError(
                        f"{actual} points outside the split super image. "
                        "The download may be incomplete."
                    )
                handle.seek(out_offset)
                handle.write(data)
                position += len(data)
                out_offset += len(data)
                remaining -= len(data)
                done += len(data)
                percent = done / total * 100 if total else None
                progress(
                    percent,
                    f"Extracting {actual}.img · {format_progress(done, total)}",
                )

        outputs: dict[str, Path] = {}
        for actual, (handle, partial, destination) in handles.items():
            handle.flush()
            handle.close()
            if partial.stat().st_size == 0:
                raise DumpError(f"{actual} is empty.")
            partial.replace(destination)
            outputs[actual] = destination
        return outputs
    except Exception:
        for handle, partial, _destination in handles.values():
            try:
                handle.close()
            except Exception:
                pass
            partial.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------- errors
def _missing_message(
    requested: Sequence[str],
    available: Sequence[str],
    payload_error: str,
    super_error: str,
) -> str:
    names = ", ".join(requested)
    lines = [f"None of the requested partitions were found: {names}."]
    if available:
        lines.append(
            f"Available partitions ({len(available)}): "
            f"{', '.join(available)}."
        )
    else:
        lines.append(
            "I could not find a payload.bin, super.img, or matching .img file "
            "in this download. Dump mode supports Android OTA zips with "
            "payload.bin, fastboot ROMs with super.img, raw partition images, "
            "and .7z/.rar archives containing any of these. Password-protected "
            "archives are not supported."
        )
    if payload_error:
        lines.append(f"Payload error: {payload_error[:300]}")
    if super_error:
        lines.append(f"super.img error: {super_error[:300]}")
    return "\n".join(lines)
