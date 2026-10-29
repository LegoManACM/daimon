"""The messaging service's home folder and settings.

$DAIMON_MESSAGES_HOME (default ~/.daimon-messages), kept apart from the agent's home on purpose: the agent never
reads the message stream, only what the MCP tools hand it.

  config.yaml     settings below (written with defaults on first run)
  messages.db     every stored message (SQLite + full-text index)
  session.json    the Google Messages pairing keys; unpairing deletes it
  control.json    where the running service listens (port + token), written at start
  watchers/       one markdown file per watcher (see watch.py); the agent writes them through the MCP tools
  watch.yaml      watcher settings: which model reads texts, how often (see watch.py)
  bin/            the sidecar, downloaded from the release (or built from sidecar/gmessages) on first use
  logs/
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_YAML = """\
# Settings for the messaging service (daimon-messages). Restart it after editing.

# On first pairing, fetch this many days of history (Google Messages never gives more than ~8).
backfill_days: 7
# Delete messages older than this many days. 0 keeps everything.
retention_days: 0
# Never store texts from 5-6 digit short codes (banks, deliveries, 2FA senders). Group chats are exempt.
skip_short_codes: true
# Never store login codes ("Your code is 123456").
skip_login_codes: true
# Names, numbers or chat names whose messages are never stored.
denylist: []
# Timezone for times shown to the agent ("" = this computer's).
timezone: ""
# Port the running service listens on, on 127.0.0.1 only. 0 picks a free one.
control_port: 8771
# Allow the send_message tool. The agent's consent gate still asks before each send.
allow_send: true
"""


def resolve_home(arg: str | None = None) -> Path:
    raw = arg or os.environ.get("DAIMON_MESSAGES_HOME") or "~/.daimon-messages"
    return Path(raw).expanduser().resolve()


@dataclass
class Config:
    backfill_days: int = 7
    retention_days: int = 0
    skip_short_codes: bool = True
    skip_login_codes: bool = True
    denylist: list[str] = field(default_factory=list)
    timezone: str = ""
    control_port: int = 8771
    allow_send: bool = True

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
    def db(self) -> Path:
        return self.root / "messages.db"

    @property
    def session(self) -> Path:
        return self.root / "session.json"

    @property
    def control(self) -> Path:
        return self.root / "control.json"

    @property
    def watchers(self) -> Path:
        return self.root / "watchers"

    @property
    def watch_settings(self) -> Path:
        return self.root / "watch.yaml"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def config(self) -> Config:
        raw: dict[str, Any] = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(Config)}
        cfg = Config(**{k: v for k, v in raw.items() if k in known and v is not None})
        cfg.denylist = [str(d) for d in cfg.denylist or []]
        return cfg
