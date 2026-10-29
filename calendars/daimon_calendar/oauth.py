"""OAuth 2 for the providers that use it (Google, Microsoft): signing in once, and keeping the token fresh.

Signing in happens in the terminal (`daimon-calendar add ...`): Google through the browser with a loopback
redirect, Microsoft with a device code. Afterwards the account file holds a refresh token, and OAuthProvider
swaps it for access tokens as they expire, saving whatever the server sends back.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Callable

import httpx

from .model import CalendarError, Provider


class OAuthProvider(Provider):
    """A provider whose account data has client_id (+ client_secret), and token: access/refresh/expires_at."""
    token_url = ""

    def token_params(self) -> dict[str, str]:
        """Extra form fields for a refresh (Microsoft wants the scope again)."""
        return {}

    async def access_token(self) -> str:
        token = self.data.get("token") or {}
        if token.get("access_token") and token.get("expires_at", 0) > time.time() + 60:
            return token["access_token"]
        if not token.get("refresh_token"):
            raise CalendarError(f"{self.name} isn't signed in. The user runs `daimon-calendar add {self.kind}` again.")
        form = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                "client_id": self.data["client_id"], **self.token_params()}
        if self.data.get("client_secret"):
            form["client_secret"] = self.data["client_secret"]
        r = await self.http.post(self.token_url, data=form)
        if r.status_code in (400, 401):
            raise CalendarError(f"{self.name}'s sign-in has expired or was revoked. The user runs "
                                f"`daimon-calendar add {self.kind} --name {self.name}` again.")
        r.raise_for_status()
        self.data["token"] = merge_token(token, r.json())
        self.save()
        return self.data["token"]["access_token"]

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        """An authorized request; a 401 gets one retry with a fresh token."""
        for attempt in range(2):
            headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {await self.access_token()}"}
            r = await self.http.request(method, url, headers=headers, **kw)
            if r.status_code != 401 or attempt:
                return r
            self.data.setdefault("token", {})["expires_at"] = 0
            kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
        return r


def merge_token(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """The token to keep: a refresh response may leave the refresh token out, meaning the old one still works."""
    return {
        "access_token": new["access_token"],
        "refresh_token": new.get("refresh_token") or old.get("refresh_token", ""),
        "expires_at": time.time() + int(new.get("expires_in", 3600)),
    }


# ---- Google: browser + loopback redirect, with PKCE ----------------------------------------------
def loopback_sign_in(auth_url: str, token_url: str, client_id: str, client_secret: str, scope: str,
                     extra: dict[str, str] | None = None, say: Callable[[str], None] = print,
                     timeout: float = 300) -> dict[str, Any]:
    """Opens the consent page in the browser and catches the redirect on 127.0.0.1. Returns the token."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") != state:
                self.send_response(400)
                self.end_headers()
                return
            got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in q
            self.wfile.write(("<h3>" + ("Signed in. You can close this tab." if ok else
                                        f"Sign-in failed: {q.get('error', '?')}") + "</h3>").encode())

        def log_message(self, *args):  # keep the terminal quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Catch)
    redirect = f"http://127.0.0.1:{server.server_port}"
    params = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": scope,
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256", **(extra or {})}
    url = auth_url + "?" + urllib.parse.urlencode(params)
    say(f"Opening your browser to sign in. If it doesn't open, go to:\n{url}")
    webbrowser.open(url)

    thread = threading.Thread(target=lambda: _serve_until(server, got, timeout), daemon=True)
    thread.start()
    thread.join(timeout + 5)
    server.server_close()
    if "code" not in got:
        raise CalendarError(f"Sign-in didn't finish ({got.get('error', 'timed out')}).")
    form = {"grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
            "client_id": client_id, "code_verifier": verifier}
    if client_secret:
        form["client_secret"] = client_secret
    r = httpx.post(token_url, data=form, timeout=30)
    if r.is_error:
        raise CalendarError(f"Couldn't finish signing in: {r.text[:300]}")
    return merge_token({}, r.json())


def _serve_until(server: http.server.HTTPServer, got: dict, timeout: float) -> None:
    server.timeout = 1
    end = time.monotonic() + timeout
    while not got and time.monotonic() < end:
        server.handle_request()


# ---- Microsoft: device code ----------------------------------------------------------------------
def device_code_sign_in(device_url: str, token_url: str, client_id: str, scope: str,
                        say: Callable[[str], None] = print, client: httpx.Client | None = None) -> dict[str, Any]:
    """Shows a code to enter at the provider's page, then waits for the user to approve. Returns the token."""
    http = client or httpx.Client(timeout=30)
    r = http.post(device_url, data={"client_id": client_id, "scope": scope})
    if r.is_error:
        raise CalendarError(f"Couldn't start signing in: {r.text[:300]}")
    flow = r.json()
    say(flow.get("message") or f"Go to {flow['verification_uri']} and enter {flow['user_code']}.")
    interval = int(flow.get("interval", 5))
    end = time.monotonic() + int(flow.get("expires_in", 900))
    while time.monotonic() < end:
        time.sleep(interval)
        r = http.post(token_url, data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                       "client_id": client_id, "device_code": flow["device_code"]})
        body = r.json()
        if r.is_success:
            return merge_token({}, body)
        err = body.get("error")
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            raise CalendarError(f"Sign-in failed: {body.get('error_description') or err}")
    raise CalendarError("Sign-in timed out.")
