from __future__ import annotations

import time

import pytest

from mirror.errors import InvalidUrl, JobCancelled, JobTimeout
from mirror.utils import (
    CancelToken,
    exceeds_limit,
    extract_url,
    filename_from_headers,
    format_progress,
    human_size,
    parse_size,
    progress_bar,
    sanitize_filename,
    SpeedMeter,
    validate_http_url,
)


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(8 * 1024**3) == "8.0 GB"
    assert human_size(None) == "?"


def test_parse_size():
    assert parse_size("8G", 1) == 8 * 1024**3
    assert parse_size("512M", 1) == 512 * 1024**2
    assert parse_size("1024", 1) == 1024
    assert parse_size("nonsense", 7) == 7
    assert parse_size(None, 9) == 9


def test_exceeds_limit_treats_zero_as_unlimited():
    assert not exceeds_limit(10**12, 0)
    assert not exceeds_limit(10**12, -1)
    assert not exceeds_limit(100, 100)
    assert exceeds_limit(101, 100)


def test_extract_url():
    assert extract_url("look https://example.com/a?b=1). done") == "https://example.com/a?b=1"
    assert extract_url("magnet:?xt=urn:btih:abc123").startswith("magnet:")
    assert extract_url("no link here") is None


def test_sanitize_filename():
    assert sanitize_filename('a/b\\c:d?.zip') == "a_b_c_d_.zip"
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert sanitize_filename("..") == "download.bin"
    assert sanitize_filename("", "fallback.bin") == "fallback.bin"
    long_name = "x" * 400 + ".zip"
    assert len(sanitize_filename(long_name)) <= 180


def test_filename_from_headers():
    assert (
        filename_from_headers({"content-disposition": 'attachment; filename="report.pdf"'})
        == "report.pdf"
    )
    assert (
        filename_from_headers({"content-disposition": "attachment; filename*=UTF-8''caf%C3%A9.zip"})
        == "café.zip"
    )
    assert filename_from_headers({}) is None


def test_validate_http_url_blocks_local_networks():
    with pytest.raises(InvalidUrl):
        validate_http_url("http://127.0.0.1/file")
    with pytest.raises(InvalidUrl):
        validate_http_url("http://localhost/file")
    with pytest.raises(InvalidUrl):
        validate_http_url("file:///etc/passwd")
    with pytest.raises(InvalidUrl):
        validate_http_url("http://10.0.0.5/file")


def test_progress_bar():
    assert progress_bar(0, 10) == "░" * 10
    assert progress_bar(100, 10) == "█" * 10
    assert progress_bar(None, 4) == "░" * 4


def test_format_progress():
    assert format_progress(1024, 2048, 512, 2) == "1.0 KB / 2.0 KB · 512 B/s · ETA 2s"
    assert format_progress(1024, None) == "1.0 KB"
    assert format_progress(0, 0) == "0 B"


def test_speed_meter_rate_and_eta():
    now = [0.0]
    meter = SpeedMeter(min_span=0, clock=lambda: now[0])
    assert meter.update(0, 100) == (None, None)

    now[0] = 1.0
    speed, eta = meter.update(50, 100)
    assert speed == pytest.approx(50.0)
    assert eta == pytest.approx(1.0)

    now[0] = 2.0
    speed, eta = meter.update(100, 100)
    assert speed == pytest.approx(50.0)
    assert eta is None


def test_cancel_token():
    token = CancelToken()
    token.check()
    token.cancel()
    with pytest.raises(JobCancelled):
        token.check()

    expired = CancelToken(deadline=time.monotonic() - 1)
    with pytest.raises(JobTimeout):
        expired.check()
