"""Where a harness works. A project keeps one workspace across all its dispatches; a task outside a project gets
its own. The source, first match wins (project before worker):
  folder: work directly in this existing folder
  repo:   a git worktree of this repository on branch daimon/<name>, under the workspaces folder; if it isn't a git
          repository (yet), the harness works in it directly, like folder
  neither: a fresh scratch folder (a git repo, so changes can be shown as a diff)
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..home import Home


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=Daimon", "-c", "user.email=agent@daimon.local", *args],
                          cwd=cwd, capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL)


def root(home: Home) -> Path:
    configured = (home.config().get("harness") or {}).get("workspaces")
    return Path(configured).expanduser() if configured else home.root.parent / f"{home.root.name}-workspaces"


def prepare(home: Home, name: str, *sources: dict[str, Any]) -> tuple[Path, str | None]:
    """The workspace for `name` (a project or task id), created if needed. Returns (path, branch)."""
    folder = next((s["folder"] for s in sources if s.get("folder")), None)
    repo = next((s["repo"] for s in sources if s.get("repo")), None)
    if folder:
        path = Path(folder).expanduser()
        if not path.is_dir():
            raise ValueError(f"workspace folder {path} doesn't exist")
        return path, None
    path = root(home) / name
    if repo:
        branch = f"daimon/{name}"
        if not (path / ".git").exists():
            repo_path = Path(repo).expanduser()
            if not repo_path.is_dir():
                raise ValueError(f"{repo_path} doesn't exist")
            top = git(repo_path, "rev-parse", "--show-toplevel")
            if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo_path.resolve():
                return repo_path, None  # a plain folder (or one inside some other repo): work in it directly
            path.parent.mkdir(parents=True, exist_ok=True)
            has_branch = git(repo_path, "rev-parse", "--verify", "--quiet", branch).returncode == 0
            args = ["worktree", "add", str(path), branch] if has_branch else ["worktree", "add", "-b", branch, str(path)]
            r = git(repo_path, *args)
            if r.returncode != 0:
                raise ValueError(f"couldn't make a worktree of {repo_path}: {r.stderr.strip()}")
        return path, branch
    if not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "-q")
    return path, None


def head(path: Path) -> str | None:
    r = git(path, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def changes(path: Path, since: str | None) -> str:
    """What a run changed: commits since `since` and the uncommitted diff stat."""
    out = []
    if since:
        log = git(path, "log", "--oneline", f"{since}..HEAD").stdout.strip()
        if log:
            out.append(f"Commits:\n{log}")
    status = git(path, "status", "--short").stdout.strip()
    if status:
        out.append(f"Uncommitted:\n{status[:2000]}")
    return "\n".join(out)
