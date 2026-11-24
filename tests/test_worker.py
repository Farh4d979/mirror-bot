from __future__ import annotations

import pytest

from mirror import remote, worker
from mirror.config import Settings
from mirror.dumper import normalize_partitions
from mirror.jobs import new_job
from mirror.uploaders.base import UploadResult


def make_settings(**overrides) -> Settings:
    values = {"telegram_token": "token"}
    values.update(overrides)
    return Settings(**values)


def base_payload(**overrides):
    payload = {
        "version": remote.PAYLOAD_VERSION,
        "job_id": "abcd1234",
        "user_id": 1,
        "user_name": "Launcher",
        "chat_id": 2,
        "message_id": 77,
        "source": "https://example.com/ota.zip",
        "source_kind": "url",
        "host": "gofile",
        "dump_partitions": [],
        "file_id": None,
        "file_name": None,
        "file_size": None,
        "settings": {},
    }
    payload.update(overrides)
    return payload


def test_job_from_payload_rebuilds_a_telegram_job():
    payload = base_payload(
        source_kind="telegram_file",
        source="ota.zip",
        file_id="telegram-file",
        file_name="../../ota.zip",
        file_size=1234,
        dump_partitions=["boot", "vendor"],
        user_name="<script>",
    )

    job, message_id = worker.job_from_payload(payload, make_settings())

    assert message_id == 77
    assert job.id == "abcd1234"
    assert job.user_id == 1
    assert job.chat_id == 2
    assert job.source_kind == "telegram_file"
    assert job.telegram_file_id == "telegram-file"
    assert job.file_name.endswith("ota.zip")
    assert "/" not in job.file_name
    assert job.file_size == 1234
    assert job.dump_partitions == normalize_partitions(["boot", "vendor"])
    assert job.status == "running"
    assert job.stage == "resolving"


def test_job_from_payload_rejects_bad_versions_and_fields():
    with pytest.raises(remote.RemoteJobError, match="version"):
        worker.job_from_payload(base_payload(version=99), make_settings())

    with pytest.raises(remote.RemoteJobError, match="required fields"):
        worker.job_from_payload(base_payload(chat_id=None), make_settings())

    with pytest.raises(remote.RemoteJobError, match="unknown source"):
        worker.job_from_payload(base_payload(source_kind="carrier_pigeon"), make_settings())

    with pytest.raises(remote.RemoteJobError, match="no source link"):
        worker.job_from_payload(base_payload(source=""), make_settings())

    with pytest.raises(remote.RemoteJobError, match="no Telegram file id"):
        worker.job_from_payload(
            base_payload(source_kind="telegram_file", file_id=None),
            make_settings(),
        )

    with pytest.raises(remote.RemoteJobError, match="dump partitions"):
        worker.job_from_payload(
            base_payload(dump_partitions="boot"),
            make_settings(),
        )


def test_worker_settings_apply_controller_limits_and_credentials():
    settings = make_settings()
    updated = worker._settings_from_payload(
        settings,
        {
            "max_file_size": 123,
            "job_timeout": 600,
            "multi_file_mode": "zip",
            "ytdlp_format": "best",
            "gofile_token": "gofile",
            "pixeldrain_api_key": "pixeldrain",
        },
    )

    assert updated.max_file_size == 123
    assert updated.job_timeout == 600
    assert updated.multi_file_mode == "zip"
    assert updated.ytdlp_format == "best"
    assert updated.gofile_token == "gofile"
    assert updated.pixeldrain_api_key == "pixeldrain"


def test_worker_settings_keep_repository_credentials_when_payload_is_blank():
    settings = make_settings(
        max_file_size=42,
        gofile_token="keep-gofile",
        pixeldrain_api_key="keep-pixeldrain",
    )
    updated = worker._settings_from_payload(
        settings,
        {"gofile_token": "", "pixeldrain_api_key": ""},
    )

    assert updated.max_file_size == 42
    assert updated.gofile_token == "keep-gofile"
    assert updated.pixeldrain_api_key == "keep-pixeldrain"


def test_worker_settings_reject_invalid_limits():
    with pytest.raises(remote.RemoteJobError, match="invalid settings"):
        worker._settings_from_payload(make_settings(), {"max_file_size": -1})

    with pytest.raises(remote.RemoteJobError, match="invalid settings"):
        worker._settings_from_payload(make_settings(), {"job_timeout": 10})

    with pytest.raises(remote.RemoteJobError, match="invalid settings"):
        worker._settings_from_payload(make_settings(), {"max_file_size": "huge"})


class FakeAPI:
    def __init__(self) -> None:
        self.edits = []
        self.deleted = []
        self.sent = []
        self.delete_result = True

    def edit_message(self, chat_id, message_id, text, *, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return self.delete_result

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return 1


def running_job():
    job = new_job(user_id=1, chat_id=2, source="https://example.com/a.zip", host="gofile")
    job.update(status="running", stage="downloading", detail="1 MB / 2 MB")
    return job


def test_progress_reporter_throttles_unchanged_cards():
    api = FakeAPI()
    job = running_job()
    reporter = worker.ProgressReporter(api, job, 55, interval=0)

    reporter(force=True)
    reporter(force=True)

    assert len(api.edits) == 1
    assert "Downloading" in api.edits[0][2]
    assert api.edits[0][3]["inline_keyboard"][0][0]["callback_data"] == f"cancel:{job.id}"

    job.update(detail="2 MB / 2 MB")
    reporter(force=True)
    assert len(api.edits) == 2


def test_progress_reporter_skips_edits_inside_the_interval():
    api = FakeAPI()
    job = running_job()
    reporter = worker.ProgressReporter(api, job, 55, interval=60)

    reporter(force=True)
    job.update(detail="changed")
    reporter()

    assert len(api.edits) == 1


def test_deliver_final_deletes_then_sends():
    api = FakeAPI()
    job = running_job()

    worker._deliver_final(api, job, 55, "done")

    assert api.deleted == [(2, 55)]
    assert api.sent == [(2, "done", None)]
    assert api.edits == []


def test_deliver_final_edits_when_delete_is_not_allowed():
    api = FakeAPI()
    api.delete_result = False
    job = running_job()

    worker._deliver_final(api, job, 55, "done")

    assert api.edits
    assert api.edits[0][2] == "done"
    assert api.sent == []


def test_job_from_payload_defaults_unknown_file_size():
    payload = base_payload(file_size="nonsense")

    job, _ = worker.job_from_payload(payload, make_settings())

    assert job.file_size is None


def test_final_dump_card_uses_each_uploaded_partition():
    job = new_job(
        user_id=1,
        chat_id=2,
        source="https://example.com/ota.zip",
        host="gofile",
        dump_partitions=("boot", "vendor"),
    )
    results = (
        UploadResult(url="https://gofile.io/d/boot", host="Gofile", file_name="boot.img", size=10),
        UploadResult(url="https://gofile.io/d/vendor", host="Gofile", file_name="vendor.img", size=20),
    )
    job.update(
        status="done",
        stage="done",
        result=results[0],
        results=results,
        file_name="boot.img, vendor.img",
        file_size=30,
    )

    text = worker.render_final(job)

    assert "Partition dump complete" in text
    assert "https://gofile.io/d/boot" in text
    assert "https://gofile.io/d/vendor" in text
    assert "boot.img" in text
    assert "vendor.img" in text
