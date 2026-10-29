"""The calendar server's home folder and settings.

$DAIMON_CALENDAR_HOME (default ~/.daimon-calendar), apart from the agent's home like the other add-ons.

  config.yaml          settings below (written with defaults on first run)
  accounts/<name>.json one per connected account: its kind, settings and credentials (`daimon-calendar add` writes them)
  ids.json             the short event ids handed to the agent, and the source keys callers gave events
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_YAML = """\
# Settings for the calendar MCP server (daimon-calendar). Clients pick changes up when they restart it.

# Timezone for times you give and get ("" = this computer's).
timezone: ""
# Where new events go when no calendar is named: "account/calendar name" or just a calendar name.
# "" = the first writable calendar of the first account.
default_calendar: ""
# Calendars to leave out of listings and free-time checks (names or "account/name").
hidden: []
# The part of the day calendar_free_time offers slots in.
day_start: "08:00"
day_end: "21:00"
# Minutes for a new event that's given a start but no end.
default_minutes: 60
"""


def resolve_home(arg: str | None = None) -> Path:
    raw = arg or os.environ.get("DAIMON_CALENDAR_HOME") or "~/.daimon-calendar"
    return Path(raw).expanduser().resolve()


@dataclass
class Config:
    timezone: str = ""
    default_calendar: str = ""
    hidden: list[str] = field(default_factory=list)
    day_start: str = "08:00"
    day_end: str = "21:00"
    default_minutes: int = 60

    def tz(self) -> tzinfo:
        if self.timezone:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone)
        return datetime.now().astimezone().tzinfo


def _known(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (raw or {}).items() if k in names and v is not None}


def account_name(text: str) -> str:
    """A safe account name: letters, digits, - and _."""
    return re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-") or "calendar"


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
    def accounts_dir(self) -> Path:
        return self.root / "accounts"

    @property
    def ids_path(self) -> Path:
        return self.root / "ids.json"

    def config(self) -> Config:
        raw: dict[str, Any] = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        cfg = Config(**_known(Config, raw))
        cfg.hidden = [str(h) for h in cfg.hidden or []]
        return cfg

    # ---- accounts ----------------------------------------------------------------------------------
    def accounts(self) -> dict[str, dict[str, Any]]:
        """name -> the saved account (kind, settings, credentials), in the order they were added."""
        if not self.accounts_dir.exists():
            return {}
        found = []
        for p in self.accounts_dir.glob("*.json"):
            data = json.loads(p.read_text(encoding="utf-8"))
            found.append((data.get("added", ""), p.stem, data))
        return {name: data for _, name, data in sorted(found)}

    def save_account(self, name: str, data: dict[str, Any]) -> Path:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        path = self.accounts_dir / f"{account_name(name)}.json"
        if "added" not in data:
            data = {"added": datetime.now().isoformat(timespec="seconds"), **data}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # credentials: owner only (no effect on Windows, where the profile folder is private)
        except OSError:
            pass
        tmp.replace(path)
        return path

    def remove_account(self, name: str) -> bool:
        path = self.accounts_dir / f"{account_name(name)}.json"
        if not path.exists():
            return False
        path.unlink()
        return True
