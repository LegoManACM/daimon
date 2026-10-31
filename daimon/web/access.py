"""Where the web page listens and who may open it. Two separate choices, each a small module.

`web.listen` in config.yaml picks the address:
  localhost   this computer only (default). Also what a reverse proxy or tunnel on this computer points at.
  tailscale   this computer's Tailscale address, so only devices on your tailnet can reach it
  all         every network interface (your home network, a VPN, anything that can reach this computer)
  <address>   any address of this computer, e.g. 192.168.1.20

`web.auth` picks who gets in:
  token       sign in once per browser with WEB_TOKEN from the secrets file (default, except on tailscale)
  tailscale   only devices logged in to your own Tailscale account, checked with `tailscale whois`; no password
  none        anyone who can reach the port. Allowed on localhost, or anywhere with `allow_open: true` when
              something in front of it (a proxy with its own login) already checks
  package.module:Class   your own: a class with `async def check(request) -> Visitor | None` (see Auth)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import ipaddress
import json
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COOKIE = "daimon_session"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


@dataclass
class Visitor:
    """Who's on the other end: shown in the page's list of open devices."""
    device: str
    user: str = ""


class AccessError(Exception):
    pass


# ---- listening ---------------------------------------------------------------------------------------
def tailscale_cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for p in (Path("C:/Program Files/Tailscale/tailscale.exe"), Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")):
        if p.exists():
            return str(p)
    return None


def tailscale(*args: str, timeout: float = 10) -> str:
    cli = tailscale_cli()
    if not cli:
        raise AccessError("Tailscale isn't installed (https://tailscale.com/download)")
    try:
        r = subprocess.run([cli, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AccessError(f"tailscale {args[0]}: {e}") from None
    if r.returncode != 0:
        raise AccessError(f"tailscale {args[0]}: {(r.stderr or r.stdout).strip() or 'failed'} (is Tailscale running and logged in?)")
    return r.stdout


def tailscale_status() -> dict[str, Any]:
    return json.loads(tailscale("status", "--json"))


def resolve_listen(listen: str) -> str:
    """The address to bind for a `web.listen` value."""
    listen = str(listen or "localhost").strip()
    if listen == "localhost":
        return "127.0.0.1"
    if listen == "all":
        return "0.0.0.0"
    if listen == "tailscale":
        ips = [ip for ip in tailscale("ip", "-4").split() if ip]
        if not ips:
            raise AccessError("Tailscale has no address for this computer (is it logged in?)")
        return ips[0]
    try:
        ipaddress.ip_address(listen)
    except ValueError:
        raise AccessError(f"web.listen {listen!r} isn't localhost, tailscale, all or an IP address") from None
    return listen


def is_loopback(host: str) -> bool:
    return host in LOOPBACK or host.startswith("127.")


def page_urls(listen: str, bound: str, port: int) -> list[str]:
    """Addresses to open the page at, best first."""
    if listen == "tailscale":
        try:
            name = (tailscale_status().get("Self") or {}).get("DNSName", "").rstrip(".")
            short = name.split(".")[0]
            return [u for u in (f"http://{short}:{port}" if short else "", f"http://{name}:{port}" if name else "",
                                f"http://{bound}:{port}") if u]
        except AccessError:
            pass
    if bound == "0.0.0.0":
        import socket
        return [f"http://{socket.gethostname()}:{port}", f"http://localhost:{port}"]
    return [f"http://{'localhost' if is_loopback(bound) else bound}:{port}"]


# ---- who gets in -------------------------------------------------------------------------------------
class Auth:
    """Decides whether a request may in. `check` returns who it is, or None for "sign in first"."""

    name = "custom"
    sign_in = False  # whether the page offers a token form when check() says no

    def __init__(self, options: dict[str, Any], home=None):
        self.options, self.home = options, home

    async def check(self, request) -> Visitor | None:  # noqa: ANN001 - a Starlette request
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


def device_of(request) -> str:  # noqa: ANN001
    ua = request.headers.get("user-agent", "")
    browser = next((b for b in ("Edg", "Firefox", "Chrome", "Safari") if b in ua), "browser")
    system = next((s for k, s in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                  ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux")) if k in ua), "")
    return f"{system} · {'Edge' if browser == 'Edg' else browser}".strip(" ·")


class TokenAuth(Auth):
    """Sign in once per browser with WEB_TOKEN. The cookie holds a value derived from the token, never the token
    itself, so changing WEB_TOKEN signs everyone out."""

    name = "token"
    sign_in = True

    def token(self) -> str:
        import os
        t = os.environ.get("WEB_TOKEN", "").strip()
        if not t:
            raise AccessError("web.auth is token but WEB_TOKEN isn't set (run `daimon config web`)")
        return t

    def session(self) -> str:
        return hmac.new(self.token().encode(), b"daimon-web-session", hashlib.sha256).hexdigest()

    def valid_token(self, given: str) -> bool:
        return bool(given) and hmac.compare_digest(given.strip().encode(), self.token().encode())

    async def check(self, request) -> Visitor | None:  # noqa: ANN001
        cookie = request.cookies.get(COOKIE, "")
        if cookie and hmac.compare_digest(cookie.encode(), self.session().encode()):
            return Visitor(device_of(request))
        return None

    def describe(self) -> str:
        return "sign-in token"


class OpenAuth(Auth):
    name = "none"

    async def check(self, request) -> Visitor | None:  # noqa: ANN001
        return Visitor(device_of(request))

    def describe(self) -> str:
        return "no sign-in"


class TailscaleAuth(Auth):
    """Only devices on the same Tailscale account as this computer. A tailnet shared with others (or a node
    shared into it) doesn't let them in. Requests from this computer itself are let in too."""

    name = "tailscale"
    CACHE_S = 300

    def __init__(self, options: dict[str, Any], home=None):
        super().__init__(options, home)
        self._seen: dict[str, tuple[float, Visitor | None]] = {}
        self._owner: Any = None

    def owner(self) -> Any:
        if self._owner is None:
            self._owner = (tailscale_status().get("Self") or {}).get("UserID")
        return self._owner

    def whois(self, ip: str) -> Visitor | None:
        try:
            info = json.loads(tailscale("whois", "--json", ip))
        except (AccessError, ValueError):
            return None
        user = info.get("UserProfile") or {}
        if user.get("ID") is None or user.get("ID") != self.owner():
            return None
        node = info.get("Node") or {}
        name = node.get("ComputedName") or (node.get("Hostinfo") or {}).get("Hostname") or ip
        return Visitor(name, user.get("LoginName", ""))

    async def check(self, request) -> Visitor | None:  # noqa: ANN001
        ip = request.client.host if request.client else ""
        if is_loopback(ip):
            return Visitor("this computer · " + device_of(request))
        seen = self._seen.get(ip)
        if seen and time.monotonic() - seen[0] < self.CACHE_S:
            v = seen[1]
        else:
            v = await asyncio.to_thread(self.whois, ip)
            self._seen[ip] = (time.monotonic(), v)
        return Visitor(f"{v.device} · {device_of(request)}", v.user) if v else None

    def describe(self) -> str:
        return "your Tailscale devices"


AUTH = {"token": TokenAuth, "tailscale": TailscaleAuth, "none": OpenAuth}


def make_auth(cfg: dict[str, Any], home=None) -> Auth:
    """The Auth for config.yaml's `web` section. Checks that the combination is safe to start."""
    listen = str(cfg.get("listen") or "localhost")
    name = str(cfg.get("auth") or ("tailscale" if listen == "tailscale" else "token"))
    if ":" in name:
        module, _, cls = name.partition(":")
        auth = getattr(importlib.import_module(module), cls)(cfg, home)
    elif name in AUTH:
        auth = AUTH[name](cfg, home)
    else:
        raise AccessError(f"web.auth {name!r} isn't token, tailscale, none or package.module:Class")
    if name == "none" and listen != "localhost" and not cfg.get("allow_open"):
        raise AccessError("web.auth none would let anyone who reaches the port in. Use it with listen: localhost, or "
                          "set allow_open: true if something in front of it already checks who gets in")
    if isinstance(auth, TokenAuth):
        auth.token()  # fail at start, not on the first visit
    return auth


def new_token() -> str:
    return secrets.token_urlsafe(24)
