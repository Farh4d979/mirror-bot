from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import mirror.dumper as dumper
from mirror.config import Settings
from mirror.errors import DumpError
from mirror.utils import CancelToken

GEOMETRY_MAGIC = 0x616C4467
HEADER_MAGIC = 0x414C5030
SPARSE_MAGIC = 0xED26FF3A

SEVEN_ZIP = dumper._find_7zip(Settings(telegram_token="test")) or ""
requires_7zip = pytest.mark.skipif(
    not SEVEN_ZIP, reason="7-Zip is not installed"
)


def create_7z(
    root: Path,
    archive_name: str,
    members: dict[str, bytes],
    *,
    extra_args: tuple[str, ...] = (),
) -> Path:
    """Build a 7z archive from relative member paths and remove the inputs."""
    root.mkdir(parents=True, exist_ok=True)
    for name, data in members.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    archive = root / archive_name
    subprocess.run(
        [SEVEN_ZIP, "a", "-t7z", *extra_args, str(archive), *members],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for name in members:
        (root / name).unlink(missing_ok=True)
    return archive


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "telegram_token": "test",
        "download_dir": tmp_path,
    }
    values.update(overrides)
    return Settings(**values)


def build_super_image(
    partitions: list[dict],
    *,
    sparse: bool = False,
    data_sector: int = 4096,
) -> bytes:
    """Build a minimal raw super image with LP metadata and one extent each."""
    total_sectors = data_sector + max(part["sectors"] for part in partitions)
    total_size = total_sectors * 512
    metadata_max_size = 4096
    metadata_offset = 4096 + 8192

    partition_table = b""
    extent_table = b""
    first_extent = 0
    for part in partitions:
        name = part["name"].encode("ascii").ljust(36, b"\0")
        partition_table += struct.pack(
            "<36sIIII",
            name,
            part.get("attributes", 0),
            first_extent,
            1,
            0,
        )
        extent_table += struct.pack(
            "<QIQI",
            part["sectors"],
            0,
            data_sector,
            0,
        )
        first_extent += 1

    block_devices = struct.pack(
        "<QIIQ36sI",
        data_sector,
        4096,
        0,
        total_size,
        b"super".ljust(36, b"\0"),
        0,
    )
    tables = partition_table + extent_table + block_devices
    extent_offset = len(partition_table)
    block_offset = extent_offset + len(extent_table)

    header = struct.pack(
        "<IHHI32sI32s",
        HEADER_MAGIC,
        10,
        2,
        256,
        b"\0" * 32,
        len(tables),
        b"\0" * 32,
    )
    header += struct.pack("<III", 0, len(partitions), 52)
    header += struct.pack("<III", extent_offset, len(partitions), 24)
    header += struct.pack("<III", 0, 0, 0)
    header += struct.pack("<III", block_offset, 1, 64)
    header = header.ljust(256, b"\0")

    image = bytearray(total_size)
    geometry = struct.pack(
        "<II32sIII",
        GEOMETRY_MAGIC,
        52,
        b"\0" * 32,
        metadata_max_size,
        2,
        4096,
    )
    image[4096 : 4096 + len(geometry)] = geometry
    image[metadata_offset : metadata_offset + len(header)] = header
    image[
        metadata_offset + len(header) : metadata_offset + len(header) + len(tables)
    ] = tables

    for part in partitions:
        start = data_sector * 512
        image[start : start + len(part["data"])] = part["data"]

    return to_sparse(bytes(image)) if sparse else bytes(image)


def to_sparse(raw: bytes) -> bytes:
    block = 4096
    padded = raw + b"\0" * (-len(raw) % block)
    total_blocks = len(padded) // block
    header = struct.pack(
        "<IHHHHIIII",
        SPARSE_MAGIC,
        1,
        0,
        28,
        12,
        block,
        total_blocks,
        1,
        0,
    )
    chunk = struct.pack("<HHII", 0xCAC1, 0, total_blocks, 12 + len(padded))
    return header + chunk + padded


def build_split_sparse_parts(
    raw: bytes,
    blocks_per_part: int,
    *,
    overlay: bool = False,
    trailing_gap: bool = True,
) -> list[bytes]:
    """Build ``super.img.0``, ``super.img.1``, … sparse parts from a raw image."""
    block = 4096
    padded = raw + b"\0" * (-len(raw) % block)
    total_blocks = len(padded) // block
    parts: list[bytes] = []
    for start in range(0, total_blocks, blocks_per_part):
        end = min(total_blocks, start + blocks_per_part)
        data = padded[start * block : end * block]
        body = b""
        chunks = 0
        if overlay and start:
            # Absolute-position layout: skip the blocks owned by other parts.
            body += struct.pack("<HHII", 0xCAC3, 0, start, 12)
            chunks += 1
        body += struct.pack("<HHII", 0xCAC1, 0, end - start, 12 + len(data))
        chunks += 1
        body += data
        if overlay and trailing_gap and end < total_blocks:
            body += struct.pack("<HHII", 0xCAC3, 0, total_blocks - end, 12)
            chunks += 1
        header = struct.pack(
            "<IHHHHIIII",
            SPARSE_MAGIC,
            1,
            0,
            28,
            12,
            block,
            total_blocks,
            chunks,
            0,
        )
        parts.append(header + body)
    return parts


def test_extract_partition_from_super_image(tmp_path):
    data = b"V" * 4096
    raw = build_super_image(
        [
            {
                "name": "vendor",
                "attributes": 2,  # SLOT_SUFFIXED; slot 0 becomes vendor_a
                "sectors": 8,
                "data": data,
            }
        ]
    )
    root = tmp_path / "files"
    root.mkdir()
    (root / "super.img").write_bytes(raw)

    result = dumper.extract_partitions(
        root,
        ["vendor", "boot"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert [path.name for path in result.files] == ["vendor_a.img"]
    assert result.files[0].read_bytes() == data
    assert result.found == ["vendor"]
    assert result.missing == ["boot"]
    assert "super.img" in result.sources
    assert "vendor_a" in result.available


def test_extract_partition_from_sparse_super_image(tmp_path):
    data = b"S" * 4096
    raw = build_super_image(
        [{"name": "system", "sectors": 8, "data": data}], sparse=True
    )
    root = tmp_path / "files"
    root.mkdir()
    (root / "super.img").write_bytes(raw)

    result = dumper.extract_partitions(
        root,
        ["system"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "system.img"
    assert result.files[0].read_bytes() == data
    assert result.missing == []


def test_split_sparse_super_inside_zip_is_merged(tmp_path):
    data = b"S" * 4096
    raw = build_super_image([{"name": "system", "sectors": 8, "data": data}])
    parts = build_split_sparse_parts(raw, 32)
    assert len(parts) > 10

    root = tmp_path / "download"
    root.mkdir()
    with zipfile.ZipFile(
        root / "rom.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for index, part in enumerate(parts):
            archive.writestr(f"images/super.img.{index}", part)

    result = dumper.extract_partitions(
        root,
        ["system"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "system.img"
    assert result.files[0].read_bytes() == data
    assert result.found == ["system"]
    assert result.sources == ["super.img"]


def test_split_sparse_super_with_dont_care_parts(tmp_path):
    data = b"O" * 4096
    raw = build_super_image([{"name": "system", "sectors": 8, "data": data}])
    parts = build_split_sparse_parts(raw, 32, overlay=True)

    root = tmp_path / "download"
    root.mkdir()
    with zipfile.ZipFile(
        root / "rom.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for index, part in enumerate(parts):
            archive.writestr(f"images/super.img.{index}", part)

    result = dumper.extract_partitions(
        root,
        ["system"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "system.img"
    assert result.files[0].read_bytes() == data


def test_split_sparse_super_without_trailing_gaps(tmp_path):
    data = b"T" * 4096
    raw = build_super_image([{"name": "system", "sectors": 8, "data": data}])
    parts = build_split_sparse_parts(raw, 32, overlay=True, trailing_gap=False)

    root = tmp_path / "download"
    root.mkdir()
    with zipfile.ZipFile(
        root / "rom.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for index, part in enumerate(parts):
            archive.writestr(f"images/super.img.{index}", part)

    result = dumper.extract_partitions(
        root,
        ["system"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "system.img"
    assert result.files[0].read_bytes() == data


def test_split_raw_super_parts_are_concatenated(tmp_path):
    data = b"R" * 4096
    raw = build_super_image([{"name": "system", "sectors": 8, "data": data}])

    root = tmp_path / "download"
    root.mkdir()
    chunk = 131072
    for index, start in enumerate(range(0, len(raw), chunk)):
        (root / f"super.img.{index}").write_bytes(raw[start : start + chunk])

    result = dumper.extract_partitions(
        root,
        ["system"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "system.img"
    assert result.files[0].read_bytes() == data


def test_split_super_needs_every_part(tmp_path):
    data = b"S" * 4096
    raw = build_super_image([{"name": "system", "sectors": 8, "data": data}])
    parts = build_split_sparse_parts(raw, 64)

    root = tmp_path / "download"
    root.mkdir()
    with zipfile.ZipFile(
        root / "rom.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for index, part in enumerate(parts):
            if index == 4:
                continue
            archive.writestr(f"images/super.img.{index}", part)

    with pytest.raises(DumpError) as excinfo:
        dumper.extract_partitions(
            root,
            ["system"],
            tmp_path / "out",
            settings=make_settings(tmp_path),
            progress=lambda percent, detail: None,
            cancel=CancelToken(),
        )

    assert "system" in str(excinfo.value)


def test_super_metadata_reads_both_slots(tmp_path, monkeypatch):
    path = tmp_path / "super.img"
    geometry = struct.pack(
        "<II32sIII",
        GEOMETRY_MAGIC,
        52,
        b"\0" * 32,
        4096,
        2,
        4096,
    )
    image = bytearray(4096 * 6)
    image[4096 : 4096 + len(geometry)] = geometry
    path.write_bytes(bytes(image))

    slot0 = {
        "vendor_a": dumper._Partition(
            name="vendor_a",
            extents=[dumper._Extent(8, 0, 4096, 0)],
        )
    }
    slot1 = {
        "vendor_b": dumper._Partition(
            name="vendor_b",
            extents=[dumper._Extent(8, 0, 4104, 0)],
        )
    }

    def fake_parse(reader, offset, slot):
        if offset == 12288:
            return slot0
        if offset == 12288 + 4096:
            return slot1
        raise DumpError("no metadata")

    monkeypatch.setattr(dumper, "_parse_metadata", fake_parse)

    partitions = dumper._read_super_partitions(
        path,
        cancel=CancelToken(),
        progress=lambda percent, detail: None,
    )

    assert set(partitions) == {"vendor_a", "vendor_b"}


def test_direct_partition_image_is_used(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "boot.img").write_bytes(b"BOOT")
    (root / "readme.txt").write_text("notes")

    result = dumper.extract_partitions(
        root,
        ["boot", "vendor"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "boot.img"
    assert result.files[0].read_bytes() == b"BOOT"
    assert result.found == ["boot"]
    assert result.missing == ["vendor"]


def test_direct_image_inside_zip_is_extracted(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    with zipfile.ZipFile(root / "images.zip", "w") as archive:
        archive.writestr("vendor.img", b"V" * 4096)

    result = dumper.extract_partitions(
        root,
        ["vendor"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "vendor.img"
    assert result.files[0].read_bytes() == b"V" * 4096
    assert result.missing == []


def test_payload_inside_tar_is_materialized_and_cleaned(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    payload = b"CrAU" + b"\0" * 100
    info = tarfile.TarInfo("ota/payload.bin")
    info.size = len(payload)
    with tarfile.open(root / "ota.tar", "w") as archive:
        archive.addfile(info, io.BytesIO(payload))

    monkeypatch.setattr(dumper, "_find_payload_dumper", lambda settings: "payload-dumper-go")
    seen = {}

    def fake_list(binary, input_path, *, cancel, progress):
        seen["path"] = input_path
        return {"boot": 4096}

    def fake_extract(binary, input_path, partitions, sizes, output_dir, *, cancel, progress):
        (output_dir / "boot.img").write_bytes(b"B" * 4096)

    monkeypatch.setattr(dumper, "_payload_partitions", fake_list)
    monkeypatch.setattr(dumper, "_extract_payload", fake_extract)

    result = dumper.extract_partitions(
        root,
        ["boot"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert seen["path"].name == "_payload_0.bin"
    assert not seen["path"].exists()
    assert result.files[0].read_bytes() == b"B" * 4096
    assert result.missing == []


def test_missing_source_reports_payload_and_super(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    (root / "notes.txt").write_text("nothing useful")

    with pytest.raises(DumpError) as excinfo:
        dumper.extract_partitions(
            root,
            ["boot"],
            tmp_path / "out",
            settings=make_settings(tmp_path),
            progress=lambda percent, detail: None,
            cancel=CancelToken(),
        )

    message = str(excinfo.value)
    assert "None of the requested partitions" in message
    assert "payload.bin" in message
    assert "super.img" in message


def test_payload_zip_extracts_requested_partitions(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    with zipfile.ZipFile(root / "ota.zip", "w") as archive:
        archive.writestr("payload.bin", b"CrAU" + b"\0" * 100)

    monkeypatch.setattr(dumper, "_find_payload_dumper", lambda settings: "payload-dumper-go")

    def fake_list(binary, input_path, *, cancel, progress):
        assert input_path.name == "ota.zip"
        return {"boot": 4096, "vendor": 8192}

    def fake_extract(binary, input_path, partitions, sizes, output_dir, *, cancel, progress):
        for name in partitions:
            (output_dir / f"{name}.img").write_bytes(name.encode() * 10)

    monkeypatch.setattr(dumper, "_payload_partitions", fake_list)
    monkeypatch.setattr(dumper, "_extract_payload", fake_extract)

    result = dumper.extract_partitions(
        root,
        ["boot", "vendor"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert [path.name for path in result.files] == ["boot.img", "vendor.img"]
    assert result.found == ["boot", "vendor"]
    assert result.missing == []
    assert "payload.bin" in result.sources


def test_payload_missing_partition_reports_available(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    with zipfile.ZipFile(root / "ota.zip", "w") as archive:
        archive.writestr("payload.bin", b"CrAU" + b"\0" * 100)

    monkeypatch.setattr(dumper, "_find_payload_dumper", lambda settings: "payload-dumper-go")
    monkeypatch.setattr(
        dumper,
        "_payload_partitions",
        lambda binary, input_path, *, cancel, progress: {"boot": 4096},
    )

    with pytest.raises(DumpError) as excinfo:
        dumper.extract_partitions(
            root,
            ["vendor"],
            tmp_path / "out",
            settings=make_settings(tmp_path),
            progress=lambda percent, detail: None,
            cancel=CancelToken(),
        )

    assert "vendor" in str(excinfo.value)
    assert "boot" in str(excinfo.value)


def test_process_output_is_not_capped_for_long_partition_lists():
    script = (
        "for index in range(65):\n"
        "    print(f'partition_{index}:{index}')\n"
    )
    lines = dumper._run_process(
        [sys.executable, "-c", script],
        label="test partition listing",
        cancel=CancelToken(),
        progress=lambda percent, detail: None,
        interval=10.0,
    )

    assert len(lines) == 65
    assert lines[0] == "partition_0:0"
    assert lines[-1] == "partition_64:64"


def test_payload_listing_keeps_every_partition(tmp_path, monkeypatch):
    lines = ["system_ext:2048"]
    lines.extend(f"partition_{index}:{index}" for index in range(65))
    monkeypatch.setattr(dumper, "_run_process", lambda *args, **kwargs: lines)

    partitions = dumper._payload_partitions(
        "payload-dumper-go",
        tmp_path / "payload.bin",
        cancel=CancelToken(),
        progress=lambda percent, detail: None,
    )

    assert len(partitions) == 66
    assert partitions["system_ext"] == 2048 * 1024
    assert partitions["partition_64"] == 64 * 1024


def test_missing_message_shows_all_available_partitions():
    names = [f"partition_{index:02d}" for index in range(45)]
    message = dumper._missing_message(["system_ext"], names, "", "")

    assert "None of the requested partitions were found" in message
    assert all(name in message for name in names)
    assert "total" not in message.lower()
    assert "…" not in message


def test_parse_dump_args_forms():
    assert dumper.parse_dump_args(
        ["https://example.com/a.zip", "-dump", "boot,vendor"]
    ) == (["https://example.com/a.zip"], ["boot,vendor"], True)
    assert dumper.parse_dump_args(["-dump", "boot", "vendor"]) == (
        [],
        ["boot", "vendor"],
        True,
    )
    assert dumper.parse_dump_args(["--dump=boot,vendor"]) == (
        [],
        ["boot,vendor"],
        True,
    )
    assert dumper.parse_dump_args(["https://example.com/a.zip"]) == (
        ["https://example.com/a.zip"],
        [],
        False,
    )


def test_normalize_partitions():
    assert dumper.normalize_partitions(["boot,vendor.img", "Boot"]) == (
        "boot",
        "vendor",
    )
    with pytest.raises(DumpError):
        dumper.normalize_partitions(["../../etc/passwd"])


def test_unsafe_metadata_names_are_ignored():
    assert dumper._pick_partition("boot", ["boot/../../evil"]) is None
    assert dumper._pick_partition("boot", ["boot_../../evil"]) is None
    assert dumper._pick_partition("boot", ["boot_a"]) == "boot_a"


def test_pick_partition_does_not_match_a_different_prefix():
    assert dumper._pick_partition("vendor", ["vendor"]) == "vendor"
    assert dumper._pick_partition("vendor", ["vendor_a", "vendor_b"]) == "vendor_a"
    assert dumper._pick_partition("vendor", ["vendor_boot", "vendor_dlkm"]) is None
    assert (
        dumper._pick_partition("vendor_boot", ["vendor_boot_a", "vendor_boot_b"])
        == "vendor_boot_a"
    )
    assert dumper._pick_partition("vendor_a", ["vendor"]) == "vendor"
    assert dumper._pick_partition("vendor_a", ["vendor_b"]) is None


def test_missing_vendor_is_not_replaced_by_vendor_boot(tmp_path):
    data = b"B" * 4096
    raw = build_super_image(
        [{"name": "vendor_boot", "sectors": 8, "data": data}]
    )
    root = tmp_path / "files"
    root.mkdir()
    (root / "super.img").write_bytes(raw)

    with pytest.raises(DumpError) as excinfo:
        dumper.extract_partitions(
            root,
            ["vendor"],
            tmp_path / "out",
            settings=make_settings(tmp_path),
            progress=lambda percent, detail: None,
            cancel=CancelToken(),
        )

    assert "vendor_boot" in str(excinfo.value)
    assert not (tmp_path / "out" / "vendor_boot.img").exists()


@requires_7zip
def test_direct_image_inside_7z_is_extracted(tmp_path):
    root = tmp_path / "files"
    create_7z(root, "images.7z", {"images/vendor.img": b"V" * 4096})

    result = dumper.extract_partitions(
        root,
        ["vendor"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "vendor.img"
    assert result.files[0].read_bytes() == b"V" * 4096
    assert result.found == ["vendor"]
    assert result.missing == []


@requires_7zip
def test_super_image_inside_7z_is_extracted(tmp_path):
    data = b"B" * 4096
    raw = build_super_image([{"name": "vendor", "sectors": 8, "data": data}])
    root = tmp_path / "files"
    create_7z(root, "super.7z", {"super.img": raw})

    result = dumper.extract_partitions(
        root,
        ["vendor"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "vendor.img"
    assert result.files[0].read_bytes()[: len(data)] == data
    assert result.found == ["vendor"]


@requires_7zip
def test_payload_inside_7z_is_materialized_and_cleaned(tmp_path, monkeypatch):
    root = tmp_path / "files"
    payload = b"CrAU" + b"\0" * 100
    create_7z(root, "ota.7z", {"ota/payload.bin": payload})

    monkeypatch.setattr(
        dumper, "_find_payload_dumper", lambda settings: "payload-dumper-go"
    )
    seen = {}

    def fake_list(binary, input_path, *, cancel, progress):
        seen["path"] = input_path
        seen["bytes"] = input_path.read_bytes()
        return {"boot": 4096}

    def fake_extract(
        binary, input_path, partitions, sizes, output_dir, *, cancel, progress
    ):
        (output_dir / "boot.img").write_bytes(b"B" * 4096)

    monkeypatch.setattr(dumper, "_payload_partitions", fake_list)
    monkeypatch.setattr(dumper, "_extract_payload", fake_extract)

    result = dumper.extract_partitions(
        root,
        ["boot"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert seen["path"].name == "_payload_0.bin"
    assert seen["bytes"] == payload
    assert not seen["path"].exists()
    assert result.files[0].read_bytes() == b"B" * 4096
    assert result.missing == []


@requires_7zip
def test_rar_archive_extracts_direct_image(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    shutil.copy(
        Path(__file__).parent / "fixtures" / "sample.rar",
        root / "rom.rar",
    )

    result = dumper.extract_partitions(
        root,
        ["vendor"],
        tmp_path / "out",
        settings=make_settings(tmp_path),
        progress=lambda percent, detail: None,
        cancel=CancelToken(),
    )

    assert result.files[0].name == "vendor.img"
    assert result.files[0].read_bytes() == b"V" * 4096
    assert result.found == ["vendor"]
    assert result.missing == []


@requires_7zip
def test_encrypted_7z_reports_a_clear_error(tmp_path):
    root = tmp_path / "files"
    create_7z(
        root,
        "secret.7z",
        {"vendor.img": b"V" * 4096},
        extra_args=("-psecret", "-mhe=on"),
    )

    with pytest.raises(DumpError) as excinfo:
        dumper.extract_partitions(
            root,
            ["vendor"],
            tmp_path / "out",
            settings=make_settings(tmp_path),
            progress=lambda percent, detail: None,
            cancel=CancelToken(),
        )

    message = str(excinfo.value).lower()
    assert "encrypted" in message or "password" in message


def test_missing_7zip_is_reported(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir()
    (root / "rom.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 100)

    monkeypatch.setattr(dumper, "_find_7zip", lambda settings: None)

    with pytest.raises(DumpError) as excinfo:
        dumper.extract_partitions(
            root,
            ["vendor"],
            tmp_path / "out",
            settings=make_settings(tmp_path),
            progress=lambda percent, detail: None,
            cancel=CancelToken(),
        )

    assert "7-Zip" in str(excinfo.value)


def test_archive_kind_detects_7z_and_rar_magic(tmp_path):
    seven = tmp_path / "mystery.bin"
    seven.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 32)
    assert dumper._archive_kind(seven) == "7z"

    rar = tmp_path / "mystery2.bin"
    rar.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\0" * 32)
    assert dumper._archive_kind(rar) == "rar"
