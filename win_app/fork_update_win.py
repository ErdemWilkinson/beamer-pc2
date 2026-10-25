"""Updates for a Beamer PC2 install: the fork's own git checkout instead of upstream's releases.

kur.ps1 installs Beamer PC2 as a git checkout of github.com/ErdemWilkinson/beamer-pc2. A check is
`git fetch` and a count of the commits on origin/main that this checkout lacks; nothing in the
working tree changes. Updating runs that checkout's kur.ps1 in update mode, in a window of its
own: it stops this Beamer, brings the checkout in line with GitHub, installs the dependencies and
starts Beamer again.

Only that install folder updates this way (available()); kur.ps1 refuses any other, because the
update resets the folder to GitHub's main. The checker still holds back from a checkout with
changes or commits of its own, in case it is ever pointed at one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from core.updates import Checker, Release

REPO_PAGE = "https://github.com/ErdemWilkinson/beamer-pc2/commits/main"
REPO_MARK = "ErdemWilkinson/beamer-pc2"
GIT_TIMEOUT_SECONDS = 60
INSTALL_DIR_NAME = "beamer-pc2"
BOTH_WAYS = "HEAD...origin/main"
# Shown as "Beamer PC2 1a2b3c4 is available." by the strings upstream already has.
LABEL = "PC2 {commit}"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def find_git():
    """git.exe from PATH, else where Git for Windows installs it; None when there is none."""
    found = shutil.which("git")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if base:
            for part in (("Git", "cmd", "git.exe"), ("Programs", "Git", "cmd", "git.exe")):
                candidate = Path(base, *part)
                if candidate.is_file():
                    return str(candidate)
    return None


def run_git(git, root, *args):
    """git's standard output, stripped; raises OSError when git fails."""
    result = subprocess.run(
        [git, "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
        creationflags=_NO_WINDOW, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=False)
    if result.returncode != 0:
        raise OSError(f"git {args[0]} failed: {(result.stderr or '').strip()[:200]}")
    return result.stdout.strip()


def is_install(root, home=None):
    """Whether `root` is the folder kur.ps1 installs into, which is always reset to GitHub."""
    home = Path(home or os.environ.get("USERPROFILE") or Path.home())
    try:
        return Path(root).resolve() == (home / INSTALL_DIR_NAME).resolve()
    except OSError:
        return False


def fork_checkout(root):
    """Whether `root` is a git checkout whose remotes name the beamer-pc2 repository."""
    try:
        text = (Path(root) / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return REPO_MARK in text


def offered(behind, ahead, clean, install):
    """Whether an update is worth offering: GitHub has commits this checkout lacks, and resetting
    to them loses nothing (an install never holds anything of its own)."""
    return behind > 0 and (install or (clean and ahead == 0))


class GitChecker(Checker):
    """core.updates.Checker that compares a checkout with origin/main instead of reading releases.
    `git` runs a git command in the checkout and returns its output; tests pass their own."""

    def __init__(self, current, enabled, on_result, root, git=None, install=None, **kwargs):
        super().__init__(current, enabled, on_result, **kwargs)
        self.root = Path(root)
        self.install = is_install(root) if install is None else install
        self._git = git

    def git(self, *args):
        if self._git is not None:
            return self._git(*args)
        executable = find_git()
        if executable is None:
            raise OSError("git is not installed")
        return run_git(executable, self.root, *args)

    def _known(self):
        return True

    def _look(self):
        self.git("fetch", "--quiet", "origin", "main")
        ahead, behind = (int(part) for part in self.git(
            "rev-list", "--left-right", "--count", BOTH_WAYS).split())
        clean = self.git("status", "--porcelain", "--untracked-files=no") == ""
        if not offered(behind, ahead, clean, self.install):
            return None, None
        commit = self.git("rev-parse", "--short", "origin/main")
        return Release(LABEL.format(commit=commit), REPO_PAGE, True), None


def update_command(root, powershell="powershell.exe"):
    """The command that updates and restarts the checkout at `root` through its own kur.ps1."""
    return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(Path(root) / "kur.ps1")]


def start_update(root, popen=subprocess.Popen):
    """Run kur.ps1 in update mode for `root` in a console of its own, so it outlives this Beamer,
    which it stops before touching the files."""
    env = {**os.environ, "BEAMER_PC2_DIR": str(Path(root)), "BEAMER_PC2_UPDATE": "1"}
    return popen(update_command(root), cwd=str(root), env=env, creationflags=_NEW_CONSOLE, close_fds=True)


def available(root, home=None):
    """Whether this run of Beamer is the beamer-pc2 install kur.ps1 made, which updates itself.
    Any other checkout keeps upstream's behaviour: an Update there would reset work in progress."""
    return (sys.platform == "win32" and is_install(root, home) and fork_checkout(root)
            and (Path(root) / "kur.ps1").is_file())
