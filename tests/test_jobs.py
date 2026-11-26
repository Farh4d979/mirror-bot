from __future__ import annotations

import pytest

import mirror.jobs as jobs_module
from mirror.compressor import CompressionResult
from mirror.config import Settings
from mirror.downloader import DownloadResult
from mirror.dumper import ExtractionResult
from mirror.errors import DownloadError, FileTooLarge, NoFallbackResolutionError
from mirror.jobs import JobManager, new_job
from mirror.uploaders.base import UploadResult


def test_gofile_resolution_error_does_not_fall_back_to_ytdlp(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    job = new_job(
        user_id=1,
        chat_id=1,
        source="https://gofile.io/d/broken",
        host="gofile",
    )

    def fail(self, url, depth=0):
        raise NoFallbackResolutionError(
            "That Gofile link does not exist or has expired."
        )

    monkeypatch.setattr(jobs_module.Resolver, "resolve", fail)

    with pytest.raises(NoFallbackResolutionError, match="Gofile"):
        manager._pipeline(job)


def test_telegram_file_is_uploaded_without_resolving(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    source = tmp_path / "uploads" / "token" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf-data")

    class FakeUploader:
        name = "gofile"

        def available(self):
            return True, ""

        def upload(self, path, progress, cancel):
            assert path.read_bytes() == b"pdf-data"
            return UploadResult(url="https://gofile.io/d/telegram", host="Gofile")

    monkeypatch.setattr(jobs_module, "get_uploader", lambda host, cfg: FakeUploader())

    job = new_job(
        user_id=1,
        chat_id=1,
        source="report.pdf",
        host="gofile",
        source_kind="telegram_file",
        local_path=source,
    )
    manager._pipeline(job)

    assert job.result is not None
    assert job.result.url == "https://gofile.io/d/telegram"
    assert job.file_name == "report.pdf"
    assert job.file_size == 8
    assert not source.exists()
    assert not source.parent.exists()


def test_torrent_file_pipeline_uses_local_torrent_kind(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    source = tmp_path / "uploads" / "token" / "file.torrent"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"torrent-bytes")
    seen = {}

    class FakeUploader:
        name = "gofile"

        def available(self):
            return True, ""

        def upload(self, path, progress, cancel):
            return UploadResult(url="https://gofile.io/d/torrent", host="Gofile")

    def fake_download(self, *, original_url, link, dest, progress):
        seen["kind"] = link.kind
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "movie.mkv"
        path.write_bytes(b"data")
        return DownloadResult(path=path, size=4)

    monkeypatch.setattr(jobs_module, "get_uploader", lambda host, cfg: FakeUploader())
    monkeypatch.setattr(jobs_module.Downloader, "download", fake_download)

    job = new_job(
        user_id=1,
        chat_id=1,
        source="file.torrent",
        host="gofile",
        source_kind="torrent_file",
        local_path=source,
    )
    manager._pipeline(job)

    assert seen["kind"] == "torrent_file"
    assert job.result is not None
    assert job.result.url == "https://gofile.io/d/torrent"
    assert not source.exists()


def test_telegram_file_above_configured_limit_fails(tmp_path):
    settings = Settings(telegram_token="test", download_dir=tmp_path, max_file_size=4)
    manager = JobManager(settings)
    source = tmp_path / "uploads" / "token" / "report.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf-data")

    job = new_job(
        user_id=1,
        chat_id=1,
        source="report.pdf",
        host="gofile",
        source_kind="telegram_file",
        local_path=source,
    )
    with pytest.raises(FileTooLarge, match="limit"):
        manager._pipeline(job)


def test_telegram_file_missing_on_disk_fails(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    job = new_job(
        user_id=1,
        chat_id=1,
        source="gone.bin",
        host="gofile",
        source_kind="telegram_file",
        local_path=tmp_path / "uploads" / "gone.bin",
    )

    with pytest.raises(DownloadError, match="no longer available"):
        manager._pipeline(job)


def test_dump_pipeline_uploads_each_partition(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    source = tmp_path / "uploads" / "token" / "ota.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"PK\x03\x04")

    boot = tmp_path / "dump" / "boot.img"
    vendor = tmp_path / "dump" / "vendor_a.img"
    boot.parent.mkdir()
    boot.write_bytes(b"B" * 100)
    vendor.write_bytes(b"V" * 200)

    uploaded = []

    class FakeUploader:
        name = "gofile"

        def available(self):
            return True, ""

        def upload(self, path, progress, cancel):
            uploaded.append(path.name)
            progress(100.0, f"{path.stat().st_size} B / {path.stat().st_size} B")
            return UploadResult(url=f"https://gofile.io/d/{path.stem}", host="Gofile")

    def fake_extract(root, partitions, output_dir, **kwargs):
        assert root == source
        assert partitions == ("boot", "vendor")
        return ExtractionResult(
            files=[boot, vendor],
            found=["boot", "vendor"],
            missing=["system"],
            sources=["super.img"],
        )

    monkeypatch.setattr(jobs_module, "get_uploader", lambda host, cfg: FakeUploader())
    monkeypatch.setattr(jobs_module, "extract_partitions", fake_extract)

    job = new_job(
        user_id=1,
        chat_id=1,
        source="ota.zip",
        host="gofile",
        source_kind="telegram_file",
        local_path=source,
        dump_partitions=("boot", "vendor"),
    )
    manager._pipeline(job)

    assert uploaded == ["boot.img", "vendor_a.img"]
    assert [result.file_name for result in job.results] == ["boot.img", "vendor_a.img"]
    assert [result.url for result in job.results] == [
        "https://gofile.io/d/boot",
        "https://gofile.io/d/vendor_a",
    ]
    assert job.result is job.results[0]
    assert job.missing_partitions == ("system",)
    assert job.file_size == 300
    assert not source.exists()


def test_compress_pipeline_uploads_one_archive(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    source = tmp_path / "uploads" / "token" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"movie-data")
    archive = tmp_path / "movie.7z"
    archive.write_bytes(b"7z-data")
    seen = {}
    uploaded = []

    class FakeUploader:
        name = "gofile"

        def available(self):
            return True, ""

        def upload(self, path, progress, cancel):
            uploaded.append(path.name)
            return UploadResult(url="https://gofile.io/d/archive", host="Gofile")

    def fake_compress(files, destination, **kwargs):
        seen["files"] = [path.name for path in files]
        seen["destination"] = destination.name
        return CompressionResult(
            path=archive,
            size=archive.stat().st_size,
            sha256="a" * 64,
        )

    monkeypatch.setattr(jobs_module, "get_uploader", lambda host, cfg: FakeUploader())
    monkeypatch.setattr(jobs_module, "compress_to_7z", fake_compress)

    job = new_job(
        user_id=1,
        chat_id=1,
        source="movie.mkv",
        host="gofile",
        source_kind="telegram_file",
        local_path=source,
        compress=True,
    )
    manager._pipeline(job)

    assert seen == {"files": ["movie.mkv"], "destination": "movie.7z"}
    assert uploaded == ["movie.7z"]
    assert job.file_name == "movie.7z"
    assert job.file_size == 7
    assert job.archive_sha256 == "a" * 64
    assert job.result is not None
    assert job.result.url == "https://gofile.io/d/archive"
    assert job.result.file_name == "movie.7z"


def test_compress_dump_pipeline_archives_all_partitions(tmp_path, monkeypatch):
    settings = Settings(telegram_token="test", download_dir=tmp_path)
    manager = JobManager(settings)
    source = tmp_path / "uploads" / "token" / "ota.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"PK\x03\x04")

    boot = tmp_path / "dump" / "boot.img"
    vendor = tmp_path / "dump" / "vendor_a.img"
    boot.parent.mkdir()
    boot.write_bytes(b"B" * 100)
    vendor.write_bytes(b"V" * 200)
    archive = tmp_path / "ota-boot-vendor.7z"
    archive.write_bytes(b"7z-data")
    seen = {}
    uploaded = []

    class FakeUploader:
        name = "gofile"

        def available(self):
            return True, ""

        def upload(self, path, progress, cancel):
            uploaded.append(path.name)
            return UploadResult(url="https://gofile.io/d/partitions", host="Gofile")

    def fake_extract(root, partitions, output_dir, **kwargs):
        return ExtractionResult(
            files=[boot, vendor],
            found=["boot", "vendor"],
            missing=["system"],
            sources=["super.img"],
        )

    def fake_compress(files, destination, **kwargs):
        seen["files"] = [path.name for path in files]
        seen["destination"] = destination.name
        return CompressionResult(
            path=archive,
            size=archive.stat().st_size,
            sha256="b" * 64,
        )

    monkeypatch.setattr(jobs_module, "get_uploader", lambda host, cfg: FakeUploader())
    monkeypatch.setattr(jobs_module, "extract_partitions", fake_extract)
    monkeypatch.setattr(jobs_module, "compress_to_7z", fake_compress)

    job = new_job(
        user_id=1,
        chat_id=1,
        source="ota.zip",
        host="gofile",
        source_kind="telegram_file",
        local_path=source,
        dump_partitions=("boot", "vendor"),
        compress=True,
    )
    manager._pipeline(job)

    assert seen == {
        "files": ["boot.img", "vendor_a.img"],
        "destination": "ota-boot-vendor.7z",
    }
    assert uploaded == ["ota-boot-vendor.7z"]
    assert len(job.results) == 1
    assert job.result is job.results[0]
    assert job.file_name == "ota-boot-vendor.7z"
    assert job.archive_sha256 == "b" * 64
    assert job.missing_partitions == ("system",)
