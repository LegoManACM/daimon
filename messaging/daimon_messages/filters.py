"""What is never stored: login codes, texts from short codes, and anyone on the denylist."""
from __future__ import annotations

import re

OTP = re.compile(
    r"\b(code|otp|passcode|password|pin|verification|verify|2fa|one[- ]time|login|log in|sign[- ]in|security)\b"
    r".{0,60}?\b\d{4,8}\b|\b\d{4,8}\b.{0,40}?\b(is your|code|verification)\b",
    re.I | re.S,
)


def digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def is_short_code(number: str) -> bool:
    d = digits(number)
    return 0 < len(d) <= 6


def is_otp(text: str) -> bool:
    return bool(OTP.search(text or ""))


def blocked(msg: dict, chat: dict | None, denylist: list[str], skip_short_codes: bool = True,
            skip_login_codes: bool = True) -> str:
    """Why this message (a sidecar event) must not be stored, or ''."""
    if skip_login_codes and is_otp(msg.get("text", "")):
        return "login code"
    numbers = [msg.get("number", "")]
    names = [msg.get("name", "")]
    if chat:
        names.append(chat.get("name", ""))
        for p in chat.get("participants", []):
            if not p.get("me"):
                numbers.append(p.get("number", ""))
                names.append(p.get("name", ""))
    if skip_short_codes and not (chat and chat.get("is_group")):
        if any(is_short_code(n) for n in numbers if n):
            return "short code"
    for entry in denylist:
        e = entry.strip().lower()
        if not e:
            continue
        d = digits(e)
        if len(d) >= 5 and any(digits(n).endswith(d[-10:]) for n in numbers if n):
            return "denylist"
        if any(n and e == n.strip().lower() for n in names):
            return "denylist"
    return ""
