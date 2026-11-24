from __future__ import annotations

import json
import subprocess

import pytest

from mirror import handoff


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_read_offset_ignores_invalid_state(tmp_path):
    state = tmp_path / "telegram.json"
    state.write_text("{not json", encoding="utf-8")
    assert handoff.read_offset(state) == 0
    assert handoff.read_offset(tmp_path / "missing.json") == 0


def test_write_offset_uses_the_standard_state_shape(tmp_path):
    state = tmp_path / "nested" / "telegram.json"
    handoff.write_offset(state, 42)
    assert json.loads(state.read_text(encoding="utf-8")) == {"last_update_id": 42}


def test_sync_offset_merges_the_newer_remote_value(monkeypatch, tmp_path):
    state = tmp_path / "telegram.json"
    state.write_text(json.dumps({"last_update_id": 100}), encoding="utf-8")

    def fake_run(command, **kwargs):
        if command[1] == "fetch":
            return completed(command)
        return completed(command, stdout=json.dumps({"last_update_id": 250}))

    monkeypatch.setattr(handoff.subprocess, "run", fake_run)
    assert handoff.sync_offset_from_origin(state, "main", cwd=tmp_path) == 250
    assert json.loads(state.read_text(encoding="utf-8"))["last_update_id"] == 250


def test_sync_offset_keeps_a_newer_local_value(monkeypatch, tmp_path):
    state = tmp_path / "telegram.json"
    state.write_text(json.dumps({"last_update_id": 900}), encoding="utf-8")

    def fake_run(command, **kwargs):
        if command[1] == "fetch":
            return completed(command)
        return completed(command, stdout=json.dumps({"last_update_id": 250}))

    monkeypatch.setattr(handoff.subprocess, "run", fake_run)
    assert handoff.sync_offset_from_origin(state, "main", cwd=tmp_path) == 900
    assert json.loads(state.read_text(encoding="utf-8"))["last_update_id"] == 900


def test_sync_offset_survives_a_failed_fetch(monkeypatch, tmp_path):
    state = tmp_path / "telegram.json"
    state.write_text(json.dumps({"last_update_id": 7}), encoding="utf-8")

    def fake_run(command, **kwargs):
        return completed(command, returncode=1, stderr="offline")

    monkeypatch.setattr(handoff.subprocess, "run", fake_run)
    assert handoff.sync_offset_from_origin(state, "main", cwd=tmp_path) == 7


def test_active_runs_filters_completed(monkeypatch):
    payload = [
        {"databaseId": 1, "status": "in_progress"},
        {"databaseId": 2, "status": "completed"},
        {"databaseId": 3, "status": "queued"},
    ]

    def fake_gh(args, **kwargs):
        return completed(args, stdout=json.dumps(payload))

    monkeypatch.setattr(handoff, "_gh", fake_gh)
    runs = handoff.active_runs("owner/repo", branch="main")
    assert [run["databaseId"] for run in runs] == [1, 3]


def test_take_over_cancels_once_and_waits_for_idle(monkeypatch):
    responses = [
        [{"databaseId": 7, "status": "in_progress"}],
        [{"databaseId": 7, "status": "in_progress"}],
        [],
    ]
    calls = []

    def fake_active(*args, **kwargs):
        return responses.pop(0) if responses else []

    def fake_gh(args, **kwargs):
        calls.append(list(args))
        return completed(args)

    monkeypatch.setattr(handoff, "active_runs", fake_active)
    monkeypatch.setattr(handoff, "_gh", fake_gh)
    monkeypatch.setattr(handoff.time, "sleep", lambda seconds: None)

    assert handoff.take_over("owner/repo") == 1
    cancels = [call for call in calls if call[:2] == ["run", "cancel"]]
    assert cancels == [["run", "cancel", "7", "--repo", "owner/repo"]]


def test_take_over_times_out_with_an_active_run(monkeypatch):
    monkeypatch.setattr(
        handoff,
        "active_runs",
        lambda *args, **kwargs: [{"databaseId": 7, "status": "in_progress"}],
    )
    monkeypatch.setattr(handoff, "_gh", lambda args, **kwargs: completed(args))
    monkeypatch.setattr(handoff.time, "sleep", lambda seconds: None)
    timestamps = iter([0.0, 1.0, 999.0])
    monkeypatch.setattr(handoff.time, "monotonic", lambda: next(timestamps))

    with pytest.raises(handoff.HandoffError, match="still active"):
        handoff.take_over("owner/repo", timeout=1)


def test_dispatch_handoff_passes_offset_and_run_length(monkeypatch):
    calls = []

    def fake_gh(args, **kwargs):
        calls.append(list(args))
        return completed(args)

    monkeypatch.setattr(handoff, "_gh", fake_gh)
    handoff.dispatch_handoff("owner/repo", "main", 12345)

    assert calls
    args = calls[0]
    assert args[:5] == ["workflow", "run", "bot.yml", "--repo", "owner/repo"]
    assert "--ref" in args and args[args.index("--ref") + 1] == "main"
    assert "handoff=true" in args
    assert "run_seconds=17100" in args
    assert "offset=12345" in args


def test_dispatch_handoff_omits_a_zero_offset(monkeypatch):
    calls = []
    monkeypatch.setattr(
        handoff,
        "_gh",
        lambda args, **kwargs: calls.append(list(args)) or completed(args),
    )
    handoff.dispatch_handoff("owner/repo", "main", 0)
    assert not any("offset=" in arg for arg in calls[0])
