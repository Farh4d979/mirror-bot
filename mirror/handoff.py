"""Coordinate the local PC runner with the on-demand GitHub Actions fallback.

The PC is the primary Telegram poller.  GitHub Actions is only used when the
PC explicitly hands over (for example on shutdown), and the PC takes the bot
back on its next start.  Telegram allows a single ``getUpdates`` consumer per
bot token, so every handover also has to cancel any active Actions run before
the local process starts polling.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from .state import save_state

logger = logging.getLogger(__name__)

DEFAULT_WORKFLOW = "bot.yml"
DEFAULT_BRANCH = "main"
DEFAULT_RUN_SECONDS = 17100
DEFAULT_TAKEOVER_TIMEOUT = 180
DEFAULT_POLL_SECONDS = 3


class HandoffError(RuntimeError):
    """Raised when the local runner cannot coordinate with GitHub Actions."""


def _gh(
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    command = ["gh", *args]
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise HandoffError(
            "GitHub CLI (gh) is not installed. Install it from "
            "https://cli.github.com/ and run `gh auth login`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HandoffError(f"`gh {' '.join(args)}` timed out.") from exc

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit code {result.returncode}"
        raise HandoffError(f"`gh {' '.join(args)}` failed: {message}")
    return result


def resolve_repo(configured: Optional[str] = None) -> str:
    """Return ``owner/name`` from ACTIONS_REPO or the current gh login."""
    if configured:
        return configured.strip()
    result = _gh(["repo", "view", "--json", "nameWithOwner"])
    try:
        repo = str(json.loads(result.stdout)["nameWithOwner"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise HandoffError(
            "Could not detect the GitHub repository. Set ACTIONS_REPO=owner/name."
        ) from exc
    if not repo:
        raise HandoffError("Could not detect the GitHub repository.")
    return repo


def current_branch(cwd: Optional[Path] = None) -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(cwd or Path.cwd()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return DEFAULT_BRANCH
    branch = result.stdout.strip()
    return branch or DEFAULT_BRANCH


def active_runs(
    repo: str,
    workflow: str = DEFAULT_WORKFLOW,
    branch: Optional[str] = None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return queued or running workflow runs."""
    args = [
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        workflow,
        "--limit",
        str(limit),
        "--json",
        "databaseId,status,conclusion",
    ]
    if branch:
        args += ["--branch", branch]
    result = _gh(args)
    try:
        runs = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise HandoffError("Could not read the GitHub Actions run list.") from exc
    if not isinstance(runs, list):
        raise HandoffError("Unexpected response from `gh run list`.")
    return [
        run
        for run in runs
        if isinstance(run, dict) and run.get("status") != "completed"
    ]


def take_over(
    repo: str,
    workflow: str = DEFAULT_WORKFLOW,
    branch: Optional[str] = None,
    *,
    timeout: int = DEFAULT_TAKEOVER_TIMEOUT,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> int:
    """Cancel any active Actions run and wait until the bot workflow is idle.

    Returns the number of runs that had to be cancelled.  Raises
    :class:`HandoffError` if a run is still active after ``timeout`` seconds.
    """
    attempted: set[int] = set()
    deadline = time.monotonic() + timeout

    while True:
        runs = active_runs(repo, workflow, branch)
        if not runs:
            if attempted:
                logger.info("GitHub Actions is idle; the local bot can poll now.")
            return len(attempted)

        for run in runs:
            try:
                run_id = int(run.get("databaseId") or 0)
            except (TypeError, ValueError):
                continue
            if not run_id or run_id in attempted:
                continue
            attempted.add(run_id)
            logger.info("Cancelling GitHub Actions run %s.", run_id)
            _gh(["run", "cancel", str(run_id), "--repo", repo], check=False)

        if time.monotonic() >= deadline:
            ids = ", ".join(str(run.get("databaseId")) for run in runs)
            raise HandoffError(
                f"GitHub Actions run(s) {ids} are still active after {timeout}s. "
                "Cancel them manually, then start the local bot again."
            )
        time.sleep(poll_seconds)


def read_offset(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return max(0, int(data.get("last_update_id", 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def write_offset(path: Path, offset: int) -> None:
    save_state(path, {"last_update_id": max(0, int(offset))})


def sync_offset_from_origin(
    state_file: Path,
    ref: str,
    *,
    cwd: Optional[Path] = None,
    timeout: int = 45,
) -> int:
    """Merge the offset persisted by Actions into the local state file.

    Actions keeps its offset in ``.state/telegram.json`` on the branch.  If the
    PC was offline while Actions handled updates, starting with the old local
    offset would replay commands.  Taking the larger offset avoids that.
    """
    root = cwd or Path.cwd()
    local = read_offset(state_file)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    try:
        fetch = subprocess.run(
            ["git", "fetch", "--quiet", "origin", ref],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        if fetch.returncode != 0:
            logger.warning("Could not fetch the GitHub state file; using the local offset.")
            return local

        show = subprocess.run(
            ["git", "show", f"origin/{ref}:.state/telegram.json"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not read the GitHub state file: %s", exc)
        return local

    if show.returncode != 0:
        return local

    try:
        remote = max(0, int(json.loads(show.stdout).get("last_update_id", 0)))
    except (ValueError, TypeError, AttributeError):
        logger.warning("The GitHub state file is not valid JSON; using the local offset.")
        return local

    merged = max(local, remote)
    if merged > local:
        write_offset(state_file, merged)
        logger.info("Restored Telegram offset %s from GitHub.", merged)
    return merged


def dispatch_handoff(
    repo: str,
    ref: str,
    offset: int,
    *,
    workflow: str = DEFAULT_WORKFLOW,
    run_seconds: int = DEFAULT_RUN_SECONDS,
) -> None:
    """Start one on-demand Actions run and pass the current Telegram offset."""
    args = [
        "workflow",
        "run",
        workflow,
        "--repo",
        repo,
        "--ref",
        ref,
        "-f",
        "handoff=true",
        "-f",
        f"run_seconds={run_seconds}",
    ]
    if offset > 0:
        args += ["-f", f"offset={offset}"]
    _gh(args)
    logger.info(
        "Started the GitHub Actions fallback for %s (%ss, offset %s).",
        repo,
        run_seconds,
        offset or "unchanged",
    )
