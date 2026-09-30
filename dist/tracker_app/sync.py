"""
sync.py
=======

Background "push stats to the website" sync for the GATE CSE Tracker.

Given a local clone of your gate-track (GitHub Pages) repo, sync_now():
  1. exports current progress to <repo>/data/stats.json, via
     stats_export.build_export() (opens its own DB connection, so it's
     safe to call from a background thread)
  2. git pulls (fast-forward only), commits, and pushes that one file

Nothing here touches the Tkinter UI directly -- app.py runs sync_now()
in a worker thread and reports the result back through its own message
queue, same pattern it already uses for volume loading.

Configuration (just the repo folder path, plus a last-synced timestamp)
lives in ~/.gate_tracker/sync_config.json, next to the tracker database.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from stats_export import build_export

CONFIG_PATH = Path.home() / ".gate_tracker" / "sync_config.json"
STATS_RELATIVE_PATH = os.path.join("data", "stats.json")
GIT_TIMEOUT = 30  # seconds, per git subprocess call


class SyncError(Exception):
    """Raised for anything that stops a sync -- message is short and
    meant to be shown directly in the UI."""


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(config):
    os.makedirs(CONFIG_PATH.parent, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_repo_path():
    return load_config().get("repo_path")


def set_repo_path(path):
    config = load_config()
    config["repo_path"] = path
    save_config(config)


def _run_git(repo_path, *args):
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SyncError(f"git {' '.join(args)} timed out")
    if result.returncode != 0:
        raise SyncError((result.stderr or result.stdout or f"git {' '.join(args)} failed").strip()[:300])
    return result.stdout


def _record_synced(repo_path):
    config = load_config()
    config["repo_path"] = repo_path
    config["last_synced"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_config(config)


def sync_now(db_path, repo_path=None):
    """
    Exports the latest stats and pushes them to the website repo.
    Runs entirely synchronously -- call this from a background thread,
    never from the UI thread (a git push can take a few seconds).

    Returns a short human-readable status string on success ("Synced."
    / "Already up to date."). Raises SyncError with a short reason on
    failure.
    """
    repo_path = repo_path or get_repo_path()
    if not repo_path:
        raise SyncError("No website repo folder set yet.")
    if not os.path.isdir(repo_path):
        raise SyncError(f"{repo_path} no longer exists.")
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        raise SyncError(f"{repo_path} doesn't look like a git repo.")
    if not shutil.which("git"):
        raise SyncError("git isn't installed (or not on PATH).")

    stats_path = os.path.join(repo_path, STATS_RELATIVE_PATH)
    os.makedirs(os.path.dirname(stats_path), exist_ok=True)

    payload = build_export(db_path)
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    try:
        _run_git(repo_path, "pull", "--ff-only")
    except SyncError as exc:
        raise SyncError(f"pull failed, resolve manually in {repo_path}: {exc}")

    _run_git(repo_path, "add", STATS_RELATIVE_PATH)

    diff = subprocess.run(
        ["git", "-C", repo_path, "diff", "--cached", "--quiet"],
        capture_output=True, timeout=GIT_TIMEOUT,
    )
    if diff.returncode == 0:
        # nothing actually changed since the last sync
        _record_synced(repo_path)
        return "Already up to date."

    total = payload["unified"]["stats"]["total"]
    when = datetime.now().strftime("%d %b %Y, %I:%M %p")
    _run_git(repo_path, "commit", "-m", f"Update stats \u2014 {total} questions tracked ({when})")
    _run_git(repo_path, "push")

    _record_synced(repo_path)
    return "Synced."
