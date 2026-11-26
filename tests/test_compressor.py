from __future__ import annotations

import hashlib

import pytest

from mirror import compressor
from mirror.compressor import (
    build_7z_command,
    build_archive_name,
    compress_to_7z,
    sha256_file,
)
from mirror.config import Settings
from mirror.errors import CompressionError
from mirror.utils import CancelToken


def test_archive_name_uses_the_source_stem():
    assert build_archive_name("ota.zip") == "ota.7z"
    assert build_archive_name("payload.bin") == "payload.7z"
    assert build_archive_name("no-extension") == "no-extension.7z"


def test_archive_name_lists_dumped_partitions():
    assert (
        build_archive_name("ota.zip", ("boot", "vendor"))
        == "ota-boot-vendor.7z"
    )
    assert build_archive_name("ota.zip", ("system",)) == "ota-system.7z"


def test_archive_name_sanitizes_illegal_characters():
    assert build_archive_name('bad<>:"name.zip') == "bad____name.7z"


def test_7z_command_uses_maximum_compression(tmp_path):
    command = build_7z_command(
        "7z",
        tmp_path / "out.7z",
        ["boot.img", "vendor.img"],
    )

    assert command[0] == "7z"
    assert command[1] == "a"
    assert "-t7z" in command
    assert "-mx=9" in command
    assert "-m0=lzma2" in command
    assert "-ms=on" in command
    assert "--" in command
    assert command[-2:] == ["boot.img", "vendor.img"]


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "data.bin"
    data = b"mirror-bot" * 1000
    path.write_bytes(data)

    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_compress_rejects_a_missing_7zip(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"data")
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    monkeypatch.setattr(compressor, "find_7zip", lambda _settings: None)

    with pytest.raises(CompressionError, match="SEVEN_ZIP_PATH"):
        compress_to_7z(
            [path],
            tmp_path / "out.7z",
            settings=settings,
            progress=lambda _percent, _detail: None,
            cancel=CancelToken(),
        )


def test_compress_rejects_an_empty_file(tmp_path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    settings = Settings(telegram_token="test", download_dir=tmp_path)

    with pytest.raises(CompressionError, match="empty"):
        compress_to_7z(
            [path],
            tmp_path / "out.7z",
            settings=settings,
            progress=lambda _percent, _detail: None,
            cancel=CancelToken(),
            seven_zip="7z",
        )


def test_compress_creates_a_real_7z_archive(tmp_path):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    if not compressor.find_7zip(settings):
        pytest.skip("7-Zip is not installed")
    first = tmp_path / "boot.img"
    second = tmp_path / "vendor.img"
    first.write_bytes(b"A" * 5000)
    second.write_bytes(b"B" * 7000)
    events = []

    result = compress_to_7z(
        [first, second],
        tmp_path / "boot-vendor.7z",
        settings=settings,
        progress=lambda percent, detail: events.append((percent, detail)),
        cancel=CancelToken(),
    )

    assert result.path.name == "boot-vendor.7z"
    assert result.path.stat().st_size == result.size > 0
    assert len(result.sha256) == 64
    assert result.sha256 == sha256_file(result.path)
    assert events[-1][0] is None
