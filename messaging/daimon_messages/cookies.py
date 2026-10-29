"""Google sign-in cookies for pairing, from whatever the user pastes: a JSON object, a "Copy as cURL" command (bash
or cmd), a "Copy as PowerShell" snippet, or a bare Cookie header.

Google Messages no longer pairs by QR code, so a linked device signs in with the Google account's cookies and the
phone confirms with an emoji.
"""
from __future__ import annotations

import json
import re

# What libgm needs; Google sometimes also wants __Secure-1PSIDTS, which is kept when present.
REQUIRED = ("SID", "HSID", "SSID", "OSID", "APISID", "SAPISID")

_CURL_HEADER = re.compile(r"""-H\s+\$?\^?(['"])\^?cookie:\s*(.*?)\^?\1""", re.I | re.S)
_CURL_B = re.compile(r"""(?:-b|--cookie)\s+\$?\^?(['"])(.*?)\^?\1""", re.S)
_CURL_OPEN = re.compile(r"""(?:-H\s+\$?\^?['"]\^?cookie:|(?:-b|--cookie)\s+\$?\^?['"])""", re.I)
_PWSH = re.compile(r"""System\.Net\.Cookie\(\s*"([^"]+)"\s*,\s*"([^"]*)\"""")
_NAME = re.compile(r"[\w.-]+")


class CutOff(ValueError):
    """The cURL command's cookies start but never end: the paste was truncated."""


def _header(text: str) -> dict[str, str]:
    out = {}
    for part in text.replace("^", "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and _NAME.fullmatch(name):
            out[name] = value
    return out


def parse(text: str) -> dict[str, str]:
    """The cookies in `text`, or {} if none are found. Raises ValueError for bad JSON, CutOff for a truncated cURL."""
    text = text.strip()
    if text.startswith("{"):
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items()}
    if m := _CURL_HEADER.search(text) or _CURL_B.search(text):
        return _header(m.group(2))
    if _CURL_OPEN.search(text):
        raise CutOff("the cookies in that cURL command are cut off")
    if pairs := _PWSH.findall(text):
        return dict(pairs)
    return _header(re.sub(r"^cookie:\s*", "", text, flags=re.I))


def missing(cookies: dict[str, str]) -> list[str]:
    return [name for name in REQUIRED if not cookies.get(name)]
