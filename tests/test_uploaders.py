from __future__ import annotations

import pytest

from mirror.errors import UploadError
from mirror.uploaders.base import UploadResult
from mirror.uploaders.gofile import GofileUploader
from mirror.uploaders.pixeldrain import PixeldrainUploader
from mirror.utils import CancelToken


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.headers: dict[str, str] = {}
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict | None = None
        self.last_auth = None
        self.last_data = None
        self.last_body = b""

    def post(self, url, data=None, headers=None, timeout=None):
        self.last_url = url
        self.last_headers = headers
        self.last_data = data
        chunks = []
        while True:
            chunk = data.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        self.last_body = b"".join(chunks)
        return self.response

    def put(self, url, data=None, headers=None, auth=None, timeout=None):
        self.last_url = url
        self.last_headers = headers
        self.last_auth = auth
        self.last_data = data
        while True:
            chunk = data.read(64 * 1024)
            if not chunk:
                break
        return self.response


def test_gofile_guest_upload(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello world")
    response = FakeResponse(
        200,
        {
            "status": "ok",
            "data": {"downloadPage": "https://gofile.io/d/abc123"},
        },
    )
    session = FakeSession(response)
    uploader = GofileUploader(session=session)
    result = uploader.upload(source, lambda percent, detail: None, CancelToken())
    assert result.url == "https://gofile.io/d/abc123"
    assert session.last_url == "https://upload.gofile.io/uploadfile"
    assert "Authorization" not in session.last_headers
    assert b'name="token"' not in session.last_body
    assert b'name="file"; filename="hello.txt"' in session.last_body


def test_gofile_folder_fallback(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    response = FakeResponse(200, {"status": "ok", "data": {"parentFolderCode": "XYZ789"}})
    result = GofileUploader(session=FakeSession(response)).upload(
        source, lambda percent, detail: None, CancelToken()
    )
    assert result.url == "https://gofile.io/d/XYZ789"


def test_gofile_token_is_sent_as_form_field(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    response = FakeResponse(200, {"status": "ok", "data": {"downloadPage": "https://gofile.io/d/x"}})
    session = FakeSession(response)
    GofileUploader(token="secret", session=session).upload(
        source, lambda percent, detail: None, CancelToken()
    )
    assert "Authorization" not in session.last_headers
    assert b'name="token"' in session.last_body
    assert b"secret" in session.last_body


def test_gofile_rejects_bad_token(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    response = FakeResponse(
        401,
        {"status": "error-token", "data": "The account token is invalid or has been revoked."},
    )
    with pytest.raises(UploadError, match="GOFILE_TOKEN"):
        GofileUploader(token="bad", session=FakeSession(response)).upload(
            source, lambda percent, detail: None, CancelToken()
        )


def test_gofile_rejects_bad_token_status_in_body(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    response = FakeResponse(
        200,
        {"status": "error-token", "data": "The account token is invalid or has been revoked."},
    )
    with pytest.raises(UploadError, match="account token"):
        GofileUploader(token="bad", session=FakeSession(response)).upload(
            source, lambda percent, detail: None, CancelToken()
        )


def test_gofile_guest_refusal_is_not_blamed_on_token(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    session = FakeSession(FakeResponse(401, text="unauthorized"))
    with pytest.raises(UploadError, match="guest upload") as excinfo:
        GofileUploader(session=session).upload(
            source, lambda percent, detail: None, CancelToken()
        )
    assert "GOFILE_TOKEN" not in str(excinfo.value)


def test_gofile_guest_upload_has_no_client_side_size_refusal(tmp_path, monkeypatch):
    class HugePath:
        name = "huge.bin"

        def is_file(self):
            return True

    uploader = GofileUploader(session=FakeSession(FakeResponse(200, {})))
    calls = []

    def fake_upload_once(path, progress, cancel):
        calls.append(path)
        return UploadResult(url="https://gofile.io/d/x", host="Gofile")

    monkeypatch.setattr(uploader, "_upload_once", fake_upload_once)

    result = uploader.upload(HugePath(), lambda percent, detail: None, CancelToken())

    assert result.url == "https://gofile.io/d/x"
    assert len(calls) == 1


@pytest.mark.parametrize("status_code", [200, 403])
def test_gofile_limits_error_is_explained(tmp_path, status_code):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    response = FakeResponse(
        status_code,
        {"status": "error-limits", "data": "Account quota exceeded."},
    )
    with pytest.raises(UploadError, match="guest 1 GB/file") as excinfo:
        GofileUploader(session=FakeSession(response)).upload(
            source, lambda percent, detail: None, CancelToken()
        )
    message = str(excinfo.value)
    assert "Account quota exceeded." in message
    assert "Pixeldrain" in message


def test_pixeldrain_requires_api_key():
    usable, reason = PixeldrainUploader().available()
    assert not usable
    assert "PIXELDRAIN_API_KEY" in reason
    assert "10 GB" in reason
    assert "pixeldrain.com/user/api_keys" in reason


def test_pixeldrain_upload(tmp_path):
    source = tmp_path / "hello world.txt"
    source.write_text("hello")
    response = FakeResponse(200, {"success": True, "id": "abc123"})
    session = FakeSession(response)
    uploader = PixeldrainUploader("secret", session=session)
    assert uploader.available() == (True, "")

    result = uploader.upload(source, lambda percent, detail: None, CancelToken())

    assert result.url == "https://pixeldrain.com/u/abc123"
    assert result.host == "Pixeldrain"
    assert session.last_url == "https://pixeldrain.com/api/file/hello%20world.txt"
    assert session.last_auth == ("", "secret")
    assert session.last_headers["Content-Length"] == "5"


def test_pixeldrain_rejects_bad_key(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    session = FakeSession(FakeResponse(401, text="unauthorized"))
    with pytest.raises(UploadError, match="API key"):
        PixeldrainUploader("bad", session=session).upload(
            source, lambda percent, detail: None, CancelToken()
        )


def test_pixeldrain_rejects_too_large_file(tmp_path):
    source = tmp_path / "hello.txt"
    source.write_text("hello")
    session = FakeSession(FakeResponse(413, text="file too large"))
    with pytest.raises(UploadError, match="10 GB"):
        PixeldrainUploader("secret", session=session).upload(
            source, lambda percent, detail: None, CancelToken()
        )
