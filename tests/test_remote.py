from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from mirror import remote
from mirror.config import Settings
from mirror.handoff import HandoffError
from mirror.jobs import new_job


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "telegram_token": "token",
        "remote_job_key": remote.generate_key(),
        "remote_state_file": tmp_path / "remote-jobs.json",
    }
    values.update(overrides)
    return Settings(**values)


def make_job(**overrides):
    values = {
        "user_id": 1,
        "chat_id": 2,
        "source": "https://example.com/ota.zip",
        "host": "gofile",
        "user_name": "Launcher",
    }
    values.update(overrides)
    return new_job(**values)


def test_payload_round_trip_does_not_expose_the_source():
    key = remote.generate_key()
    payload = {"version": 1, "source": "https://secret.example/ota.zip"}

    token = remote.encrypt_payload(key, payload)

    assert "secret.example" not in token
    assert remote.decrypt_payload(key, token) == payload


def test_decrypt_rejects_a_wrong_key_and_tampering():
    key = remote.generate_key()
    token = remote.encrypt_payload(key, {"job_id": "abc"})

    with pytest.raises(remote.RemoteJobError, match="decrypt"):
        remote.decrypt_payload(remote.generate_key(), token)
    with pytest.raises(remote.RemoteJobError, match="decrypt"):
        remote.decrypt_payload(key, token[:-2] + "AA")


def test_encrypt_rejects_an_invalid_key():
    with pytest.raises(remote.RemoteJobError, match="Fernet"):
        remote.encrypt_payload("not-a-key", {"job_id": "abc"})


def test_decrypt_rejects_non_object_payloads():
    key = remote.generate_key()
    token = Fernet(key.encode("ascii")).encrypt(b"[]").decode("ascii")

    with pytest.raises(remote.RemoteJobError, match="not an object"):
        remote.decrypt_payload(key, token)


def test_build_payload_carries_the_worker_settings(tmp_path):
    settings = make_settings(
        tmp_path,
        max_file_size=12345,
        job_timeout=900,
        multi_file_mode="zip",
        ytdlp_format="best",
        gofile_token="gofile-token",
        pixeldrain_api_key="pixeldrain-key",
    )
    job = make_job(
        source_kind="telegram_file",
        telegram_file_id="file-id",
        dump_partitions=("boot", "vendor"),
        compress=True,
    )
    job.update(file_name="ota.zip", file_size=456)

    payload = remote.build_payload(job, 77, settings=settings)

    assert payload["version"] == remote.PAYLOAD_VERSION
    assert payload["job_id"] == job.id
    assert payload["message_id"] == 77
    assert payload["file_id"] == "file-id"
    assert payload["file_name"] == "ota.zip"
    assert payload["file_size"] == 456
    assert payload["dump_partitions"] == ["boot", "vendor"]
    assert payload["compress"] is True
    assert payload["settings"] == {
        "max_file_size": 12345,
        "job_timeout": 900,
        "multi_file_mode": "zip",
        "ytdlp_format": "best",
        "gofile_token": "gofile-token",
        "pixeldrain_api_key": "pixeldrain-key",
    }


def test_dispatch_carries_an_inline_message_id(tmp_path):
    settings = make_settings(tmp_path)
    captured = {}

    def fake_dispatch(repo, ref, payload, job_id, *, workflow):
        captured["payload"] = payload
        return 5, "https://github.com/owner/repo/actions/runs/5"

    manager = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=fake_dispatch,
    )
    job = make_job()

    record = manager.dispatch(job, message_id=0, inline_message_id="inline-1")

    assert record.message_id == 0
    assert record.inline_message_id == "inline-1"
    decrypted = remote.decrypt_payload(settings.remote_job_key or "", captured["payload"])
    assert decrypted["inline_message_id"] == "inline-1"
    assert decrypted["message_id"] == 0


def test_dispatch_encrypts_the_payload_and_tracks_the_run(tmp_path):
    settings = make_settings(tmp_path)
    calls = {}

    def fake_dispatch(repo, ref, payload, job_id, *, workflow):
        calls.update(
            repo=repo,
            ref=ref,
            payload=payload,
            job_id=job_id,
            workflow=workflow,
        )
        return 4242, "https://github.com/owner/repo/actions/runs/4242"

    manager = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=fake_dispatch,
    )
    job = make_job(telegram_file_id="file-id", source_kind="telegram_file")

    record = manager.dispatch(job, message_id=99)

    assert record.run_id == 4242
    assert record.message_id == 99
    assert calls["repo"] == "owner/repo"
    assert calls["ref"] == "main"
    assert calls["job_id"] == job.id
    assert calls["workflow"] == "job.yml"
    decrypted = remote.decrypt_payload(settings.remote_job_key or "", calls["payload"])
    assert decrypted["job_id"] == job.id
    assert decrypted["chat_id"] == 2
    assert decrypted["message_id"] == 99
    assert decrypted["file_id"] == "file-id"

    data = json.loads(settings.remote_state_file.read_text(encoding="utf-8"))
    assert data["jobs"] == [
        {
            "job_id": job.id,
            "user_id": 1,
            "user_name": "Launcher",
            "chat_id": 2,
            "source": "https://example.com/ota.zip",
            "source_kind": "telegram_file",
            "host": "gofile",
            "dump_partitions": [],
            "compress": False,
            "message_id": 99,
            "inline_message_id": "",
            "run_id": 4242,
            "run_url": "https://github.com/owner/repo/actions/runs/4242",
            "created_at": record.created_at,
            "state": "queued",
        }
    ]


def test_manager_reloads_tracked_jobs(tmp_path):
    settings = make_settings(tmp_path)
    manager = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=lambda *args, **kwargs: (7, "https://example/7"),
    )
    job = make_job()
    manager.dispatch(job, message_id=5)

    reloaded = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=lambda *args, **kwargs: (8, "https://example/8"),
    )

    record = reloaded.record(job.id)
    assert record is not None
    assert record.run_id == 7
    assert record.message_id == 5
    assert reloaded.active_for(1) == [record.job]
    assert not reloaded.active_for(2)


def test_cancel_marks_the_run_once_and_respects_the_owner(tmp_path):
    settings = make_settings(tmp_path)
    cancel_calls = []
    manager = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=lambda *args, **kwargs: (7, "https://example/7"),
        cancel=lambda repo, run_id: cancel_calls.append((repo, run_id)) or True,
        view=lambda repo, run_id: {"status": "in_progress", "conclusion": ""},
    )
    job = make_job()
    manager.dispatch(job, message_id=5)

    assert not manager.cancel_job(job.id, user_id=99)
    assert cancel_calls == []
    assert manager.cancel_job(job.id, user_id=1)
    assert cancel_calls == [("owner/repo", 7)]
    assert manager.record(job.id).state == "cancelling"

    # A second click should not send another cancel request.
    assert manager.cancel_job(job.id, user_id=1)
    assert cancel_calls == [("owner/repo", 7)]

    # Polling must not lose the cancelling state while GitHub catches up.
    assert manager.refresh() == []
    assert manager.record(job.id).state == "cancelling"
    assert "Cancelling" in manager.record(job.id).job.detail
    assert manager.cancel_job(job.id, user_id=1)
    assert cancel_calls == [("owner/repo", 7)]


def test_cancel_reports_failure_without_changing_state(tmp_path):
    settings = make_settings(tmp_path)
    manager = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=lambda *args, **kwargs: (7, "https://example/7"),
        cancel=lambda repo, run_id: False,
    )
    job = make_job()
    manager.dispatch(job, message_id=5)

    assert not manager.cancel_job(job.id)
    assert manager.record(job.id).state == "queued"

    assert manager.cancel_user(1) == 0


def test_refresh_returns_completed_runs_and_forgets_them(tmp_path):
    settings = make_settings(tmp_path)
    views = {}

    def fake_view(repo, run_id):
        return views[run_id]

    manager = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=lambda *args, **kwargs: (7, "https://example/7"),
        view=fake_view,
    )
    job = make_job()
    manager.dispatch(job, message_id=5)

    views[7] = {"status": "in_progress", "conclusion": ""}
    assert manager.refresh() == []
    assert manager.record(job.id).state == "running"
    assert "GitHub Actions" in manager.record(job.id).job.detail

    views[7] = {"status": "completed", "conclusion": "cancelled"}
    events = manager.refresh()

    assert len(events) == 1
    assert events[0].record.job.id == job.id
    assert events[0].conclusion == "cancelled"
    assert manager.record(job.id) is None
    data = json.loads(settings.remote_state_file.read_text(encoding="utf-8"))
    assert data["jobs"] == []


def test_refresh_forgets_a_run_after_twelve_hours_of_errors(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    manager = remote.RemoteJobManager(
        settings,
        repo="owner/repo",
        ref="main",
        dispatch=lambda *args, **kwargs: (7, "https://example/7"),
        view=lambda repo, run_id: (_ for _ in ()).throw(HandoffError("temporary")),
    )
    job = make_job()
    record = manager.dispatch(job, message_id=5)
    record.created_at -= 13 * 60 * 60

    assert manager.refresh() == []
    assert manager.record(job.id) is None


def test_manager_requires_a_valid_key(tmp_path):
    with pytest.raises(remote.RemoteJobError, match="not configured"):
        remote.RemoteJobManager(
            Settings(
                telegram_token="token",
                remote_state_file=tmp_path / "state.json",
            ),
            repo="owner/repo",
            ref="main",
        )

    with pytest.raises(remote.RemoteJobError, match="Fernet"):
        remote.RemoteJobManager(
            Settings(
                telegram_token="token",
                remote_job_key="not-a-key",
                remote_state_file=tmp_path / "state.json",
            ),
            repo="owner/repo",
            ref="main",
        )
