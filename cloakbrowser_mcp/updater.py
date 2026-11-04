"""Self-update from GitHub at startup (git checkouts only).

Runs before the server module is imported, so a pull takes effect immediately.
Safe by design: fast-forward only, skips a dirty tree or local-only commits,
throttled, time-limited, never raises, and writes only to stderr (stdout is the
MCP channel). Disable with CLOAKBROWSER_MCP_AUTOUPDATE=0.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STAMP = REPO / ".git" / "cloakbrowser_update_check"  # inside .git: never shows as a local change
INTERVAL_S = int(os.environ.get("CLOAKBROWSER_MCP_UPDATE_INTERVAL_S", 6 * 3600))
GIT_TIMEOUT_S = 20
PIP_TIMEOUT_S = 180


def _log(msg: str) -> None:
    print(f"[cloakbrowser-mcp update] {msg}", file=sys.stderr, flush=True)


def _git(*args: str, timeout: int = GIT_TIMEOUT_S) -> str:
    r = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def _due() -> bool:
    try:
        return time.time() - STAMP.stat().st_mtime >= INTERVAL_S
    except OSError:
        return True


def maybe_update() -> None:
    if os.environ.get("CLOAKBROWSER_MCP_AUTOUPDATE", "1") == "0":
        return
    if not (REPO / ".git").is_dir() or not _due():
        return
    try:
        STAMP.touch()  # touch first so a failing network doesn't retry on every start
        if _git("status", "--porcelain"):
            _log("skipped: working tree has local changes")
            return
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            return  # detached
        _git("fetch", "--quiet", "origin", branch)
        local, remote = _git("rev-parse", "HEAD"), _git("rev-parse", f"origin/{branch}")
        if local == remote:
            return
        if _git("rev-list", "--count", f"HEAD..origin/{branch}") == "0":
            return  # remote is behind or equal; local has unpushed commits
        if _git("rev-list", "--count", f"origin/{branch}..HEAD") != "0":
            _log("skipped: local commits not on origin (would not fast-forward)")
            return
        changed = _git("diff", "--name-only", "HEAD", f"origin/{branch}").splitlines()
        _git("merge", "--ff-only", f"origin/{branch}")
        _log(f"updated {local[:7]} -> {remote[:7]}")
        if "pyproject.toml" in changed:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-e", str(REPO)],
                capture_output=True, timeout=PIP_TIMEOUT_S,
            )
            _log("dependencies refreshed")
    except Exception as e:  # never block startup
        _log(f"skipped: {e}")
