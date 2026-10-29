"""Client for the running service's control API (see service.py). Used by the CLI and the MCP server."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import Home


class ServiceDown(Exception):
    """The service isn't running (or isn't answering)."""


def call(home: Home, method: str, path: str, body: dict | None = None, timeout: float = 90) -> dict:
    try:
        info = json.loads(home.control.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as e:
        raise ServiceDown("The messaging service isn't running. Start it with `daimon-messages run`.") from e
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {info['token']}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") | {"ok": False}
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise ServiceDown("The messaging service isn't answering. Is `daimon-messages run` running?") from e
