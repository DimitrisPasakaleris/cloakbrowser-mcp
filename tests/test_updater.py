"""Auto-update: fast-forwards a clone to origin; skips unsafe states."""
import subprocess

import pytest

from cloakbrowser_mcp import updater


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    # temp dirs on some filesystems record no ownership; git refuses them as "dubious"
    # (safe.directory is only honoured from protected config, so use a global file)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[safe]\n\tdirectory = *\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "a.txt").write_text("1")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "one")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    monkeypatch.setattr(updater, "REPO", clone)
    monkeypatch.setattr(updater, "STAMP", clone / ".git" / "cloakbrowser_update_check")
    monkeypatch.delenv("CLOAKBROWSER_MCP_AUTOUPDATE", raising=False)

    def push_new_commit():
        (seed / "b.txt").write_text("2")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "two")
        git(seed, "push", "-q", "origin", "main")

    return clone, push_new_commit


def test_fast_forwards(repos):
    clone, push = repos
    push()
    updater.maybe_update()
    assert (clone / "b.txt").exists()


def test_throttled_after_first_check(repos):
    clone, push = repos
    updater.maybe_update()  # stamps
    push()
    updater.maybe_update()  # within interval, no fetch
    assert not (clone / "b.txt").exists()


def test_disabled_by_env(repos, monkeypatch):
    clone, push = repos
    push()
    monkeypatch.setenv("CLOAKBROWSER_MCP_AUTOUPDATE", "0")
    updater.maybe_update()
    assert not (clone / "b.txt").exists()


def test_skips_dirty_tree(repos):
    clone, push = repos
    push()
    (clone / "a.txt").write_text("local edit")
    updater.maybe_update()
    assert not (clone / "b.txt").exists()
    assert (clone / "a.txt").read_text() == "local edit"


def test_skips_when_local_commits_ahead(repos):
    clone, push = repos
    push()
    (clone / "c.txt").write_text("mine")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "local")
    updater.maybe_update()
    assert not (clone / "b.txt").exists()


def test_never_raises_without_git_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOAKBROWSER_MCP_AUTOUPDATE", raising=False)
    monkeypatch.setattr(updater, "REPO", tmp_path)
    monkeypatch.setattr(updater, "STAMP", tmp_path / ".git" / "cloakbrowser_update_check")
    updater.maybe_update()
