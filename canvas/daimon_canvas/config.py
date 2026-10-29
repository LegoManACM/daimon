"""The Canvas server's home folder and settings.

$DAIMON_CANVAS_HOME (default ~/.daimon-canvas), apart from the agent's home like the messaging service's.

  config.yaml     settings below (written with defaults on first run)
  token           the Canvas access token (`daimon-canvas login` writes it); CANVAS_TOKEN in the environment wins
  files/          what canvas_open downloaded: assignment details, attachments, course files
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_YAML = """\
# Settings for the Canvas MCP server (daimon-canvas). Clients pick changes up when they restart it.

# Your school's Canvas, like https://school.instructure.com (CANVAS_URL in the environment wins).
url: ""
# Timezone for due dates ("" = this computer's).
timezone: ""
# Files bigger than this many MB aren't downloaded.
max_file_mb: 25
# How much of an assignment or file canvas_open shows at once; read_canvas_file pages through the rest.
read_chars: 6000
"""


def resolve_home(arg: str | None = None) -> Path:
    raw = arg or os.environ.get("DAIMON_CANVAS_HOME") or "~/.daimon-canvas"
    return Path(raw).expanduser().resolve()


@dataclass
class Config:
    url: str = ""
    timezone: str = ""
    max_file_mb: int = 25
    read_chars: int = 6000

    def tz(self) -> tzinfo:
        if self.timezone:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone)
        return datetime.now().astimezone().tzinfo


class Home:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(DEFAULTS_YAML, encoding="utf-8")

    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def token_path(self) -> Path:
        return self.root / "token"

    @property
    def files(self) -> Path:
        return self.root / "files"

    def config(self) -> Config:
        raw: dict[str, Any] = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(Config)}
        cfg = Config(**{k: v for k, v in raw.items() if k in known and v is not None})
        cfg.url = (os.environ.get("CANVAS_URL") or str(cfg.url or "")).strip().rstrip("/")
        return cfg

    def token(self) -> str:
        if os.environ.get("CANVAS_TOKEN"):
            return os.environ["CANVAS_TOKEN"].strip()
        return self.token_path.read_text(encoding="utf-8").strip() if self.token_path.exists() else ""

    def save_login(self, url: str, token: str) -> None:
        """Writes the token file and the url into config.yaml, keeping the file's comments."""
        self.token_path.write_text(token.strip() + "\n", encoding="utf-8")
        lines = self.config_path.read_text(encoding="utf-8").splitlines()
        entry = f'url: "{url.strip().rstrip("/")}"'
        at = next((i for i, line in enumerate(lines) if line.startswith("url:")), None)
        if at is None:
            lines.append(entry)
        else:
            lines[at] = entry
        self.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
