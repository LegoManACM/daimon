"""The home folder: every file the agent is made of, plus git history and secrets.

Nothing is cached between turns. Files are re-read whenever they're needed, so an edit (by you or by the
agent) takes effect on the next model call without a restart.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TEMPLATES = Path(__file__).parent / "templates"
PROTECTED = {"POLICY.md", "config.yaml", "mcp.json", "models.yaml"}
HIDDEN = {".git", ".cache", "threads", "tasks", "inbox", "projects"}
GIT_ID = ["-c", "user.name=Daimon", "-c", "user.email=agent@daimon.local"]


def resolve_home(flag: str | None = None) -> Path:
    return Path(flag or os.environ.get("DAIMON_HOME") or Path.home() / ".daimon").expanduser().resolve()


def git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    # stdin=DEVNULL: on Windows, a child inheriting a stdin handle that another thread is blocked reading
    # (chat input from a pipe) waits until that read finishes.
    return subprocess.run(["git", *GIT_ID, *args], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                          stdin=subprocess.DEVNULL)


def git_commit(folder: Path, message: str, paths: list[str] | None = None) -> str | None:
    """Commit `paths` (or everything) in the repo at `folder`. The short hash, or None if nothing changed."""
    git(folder, "add", "-A", "--", *(paths or ["."]))
    if git(folder, "diff", "--cached", "--quiet").returncode == 0:
        return None
    git(folder, "commit", "-q", "-m", message)
    return git(folder, "rev-parse", "--short", "HEAD").stdout.strip()


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


def join_frontmatter(meta: dict[str, Any], body: str) -> str:
    return f"---\n{yaml.safe_dump(meta, sort_keys=False).strip()}\n---\n{body.lstrip()}"


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_secrets(value: Any) -> Any:
    """Replace ${NAME} with the environment value, recursively. Values never pass through the model."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_secrets(v) for v in value]
    return value


@dataclass
class Worker:
    name: str
    model: str
    prompt: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Home:
    root: Path

    # ---- paths -------------------------------------------------------------------------------------
    @property
    def secrets_file(self) -> Path:
        return Path(os.environ.get("DAIMON_SECRETS") or self.root.parent / f"{self.root.name}-secrets.env")

    @property
    def cache(self) -> Path:
        return self.root / ".cache"

    def exists(self) -> bool:
        return (self.root / "config.yaml").exists()

    def agent_path(self, rel: str) -> Path:
        """Resolve a path the agent asked for, refusing anything outside its own files."""
        rel = rel.replace("\\", "/").lstrip("/")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"{rel} is outside the agent's home folder")
        if p.relative_to(self.root).parts[:1] and p.relative_to(self.root).parts[0] in HIDDEN:
            raise ValueError(f"{rel} is internal state, not an agent file")
        return p

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    # ---- init --------------------------------------------------------------------------------------
    def init(self, model: str | None = None) -> list[str]:
        created = []
        self.root.mkdir(parents=True, exist_ok=True)
        for src in TEMPLATES.rglob("*"):
            if src.is_dir():
                continue
            dst = self.root / src.relative_to(TEMPLATES)
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                created.append(self.rel(dst))
        (self.root / ".gitignore").write_text(".cache/\nthreads/\ntasks/\ninbox/\nprojects/\n", encoding="utf-8")
        if model:
            self._use_model(model)
        if not self.secrets_file.exists():
            self.secrets_file.write_text(
                "# Secrets for Daimon. Never committed; the agent can't read this file.\n"
                "# Refer to them in config files as ${NAME}.\n"
                "# ANTHROPIC_API_KEY=\n# OPENAI_API_KEY=\n# OPENROUTER_API_KEY=\n", encoding="utf-8")
            created.append(str(self.secrets_file))
        if not (self.root / ".git").exists():
            self._git("init", "-q")
        self.commit("init: starter files")
        return created

    def _use_model(self, model: str, price: tuple[float, float] | None = None) -> None:
        # Text edits rather than a YAML round-trip, so the starter files keep their comments.
        text = self.read("models.yaml")
        if not self.model_entry(model):
            cost = f"    price: {{in: {price[0]:g}, out: {price[1]:g}}}\n" if price else ""
            text = text.replace("models:\n", f"models:\n  - id: {model}\n    tier: standard\n    good_for: [general]\n"
                                             f"{cost}", 1)
        text = re.sub(r"^default_for_new_workers:.*$", f"default_for_new_workers: {model}", text, flags=re.M)
        self.write("models.yaml", text)
        main = re.sub(r"^model:.*$", f"model: {model}", self.read("workers/main.md"), count=1, flags=re.M)
        self.write("workers/main.md", main)

    def ignore(self, entry: str) -> None:
        path = self.root / ".gitignore"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if entry not in lines:
            path.write_text("\n".join([*lines, entry]) + "\n", encoding="utf-8")

    # ---- reading -----------------------------------------------------------------------------------
    def read(self, rel: str, default: str = "") -> str:
        p = self.root / rel
        return p.read_text(encoding="utf-8") if p.exists() else default

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def read_yaml(self, rel: str) -> dict[str, Any]:
        return yaml.safe_load(self.read(rel)) or {}

    def write_yaml(self, rel: str, data: dict[str, Any]) -> None:
        self.write(rel, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def config(self) -> dict[str, Any]:
        return self.read_yaml("config.yaml")

    @property
    def knowledge(self) -> Path:
        """The knowledge folder: `knowledge.folder` in config.yaml (inside the home, or any path), default knowledge/."""
        folder = str(self.config().get("knowledge", {}).get("folder") or "knowledge")
        p = Path(folder).expanduser()
        return p if p.is_absolute() else self.root / p

    def set_knowledge_folder(self, folder: str) -> None:
        self.set_option("knowledge", "folder", folder.replace(chr(92), "/"))

    def set_option(self, section: str, key: str, value: Any) -> None:
        """Set `section.key` in config.yaml. A text edit, so the file keeps its comments (the line's own too)."""
        lines = self.read("config.yaml").splitlines()
        start = next((i for i, line in enumerate(lines) if re.match(rf"^{section}:\s*(#.*)?$", line)), None)
        new = f"  {key}: {json.dumps(value)}"
        if start is None:
            lines += ["", f"{section}:", new]
        else:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].strip() and not lines[i][0].isspace() and not lines[i].startswith("#")), len(lines))
            at = next((i for i in range(start + 1, end) if re.match(rf"^  {key}:", lines[i])), None)
            if at is None:
                lines.insert(start + 1, new)
            else:
                comment = re.search(r"\s+#.*$", lines[at])
                lines[at] = f"{new.ljust(27)} {comment.group().strip()}" if comment else new
        self.write("config.yaml", "\n".join(lines) + "\n")

    def models(self) -> dict[str, Any]:
        return self.read_yaml("models.yaml")

    def mcp(self) -> dict[str, Any]:
        return json.loads(self.read("mcp.json", '{"mcpServers": {}}') or '{"mcpServers": {}}')

    def write_mcp(self, data: dict[str, Any]) -> None:
        self.write("mcp.json", json.dumps(data, indent=2) + "\n")

    def model_entry(self, model_id: str) -> dict[str, Any] | None:
        return next((m for m in self.models().get("models", []) if m.get("id") == model_id), None)

    def missing_keys(self, model_id: str) -> list[str]:
        """Provider settings a model needs that aren't set, so the agent never assigns a model it can't call."""
        params = (self.model_entry(model_id) or {}).get("params") or {}
        if params.get("api_key"):
            return []
        try:
            import litellm
            missing = litellm.validate_environment(model=model_id).get("missing_keys", [])
        except Exception:
            return []
        # Ollama's base URL defaults to localhost; an api_base in params covers *_API_BASE.
        return [k for k in missing if k != "OLLAMA_API_BASE" and not (k.endswith("_API_BASE") and params.get("api_base"))]

    def worker(self, name: str) -> Worker:
        text = self.read(f"workers/{name}.md")
        if not text:
            raise FileNotFoundError(f"workers/{name}.md does not exist")
        meta, body = split_frontmatter(text)
        harnessed = meta.get("harness") not in (None, "", "native")  # a harness picks its own default model
        model = meta.get("model") or ("" if harnessed else self.models().get("default_for_new_workers", ""))
        return Worker(name=name, model=model, prompt=body.strip(), meta=meta)

    def files(self) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            parts = p.relative_to(self.root).parts
            # Knowledge notes are reached through kg_* tools; listing them all would flood the prompt.
            if p.is_file() and parts[0] not in HIDDEN | {"knowledge"} and parts[0] != ".gitignore":
                out.append(p.relative_to(self.root).as_posix())
        return out

    def load_secrets(self) -> None:
        """Load secrets.env into the process environment (existing variables win)."""
        if not self.secrets_file.exists():
            return
        for line in self.secrets_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    def set_secret(self, name: str, value: str) -> None:
        """Set NAME=value in the secrets file (replacing the line or its `# NAME=` placeholder) and the environment."""
        path = self.secrets_file
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        pattern = re.compile(rf"^\s*#?\s*{re.escape(name)}\s*=")
        at = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
        if at is None:
            lines.append(f"{name}={value}")
        else:
            lines[at] = f"{name}={value}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        os.environ[name] = value

    # ---- git ---------------------------------------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return git(self.root, *args)

    def commit(self, message: str, paths: list[str] | None = None) -> str | None:
        """Commit changes (to `paths`, or everything). Returns the short hash, or None if nothing changed."""
        return git_commit(self.root, message, paths)

    def commit_user_edits(self) -> str | None:
        """Changes made outside the agent (you editing files) get their own commit, so undo stays clean."""
        return self.commit("user: edited files")

    def history(self, limit: int = 15) -> str:
        return self._git("log", f"-n{limit}", "--format=%h %ad %s", "--date=format:%Y-%m-%d %H:%M").stdout

    def changed_files(self, ref: str) -> list[str]:
        out = self._git("show", "--name-only", "--format=", ref)
        if out.returncode != 0:
            raise ValueError(f"unknown change {ref}")
        return [line for line in out.stdout.splitlines() if line]

    def revert(self, ref: str, reason: str) -> str:
        r = self._git("revert", "--no-commit", ref)
        if r.returncode != 0:
            self._git("revert", "--abort")
            raise ValueError(f"couldn't undo {ref} cleanly: {r.stderr.strip()}")
        return self.commit(f"agent: undo {ref}: {reason}") or "(nothing to undo)"
