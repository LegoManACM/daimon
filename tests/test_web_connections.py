"""The Connections view's backend: secrets, MCP servers, Telegram and add-on setup jobs."""
import asyncio
import json
import sys
import time

import pytest

from daimon.setup import addons
from daimon.web import connections
from daimon.web.connections import Jobs

from test_agent import home  # noqa: F401 - fixture
from test_web import W, client, make

TOKEN = "123456789:" + "A" * 30


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "MY_KEY", "GH_TOKEN", "GH_API_KEY", "GHX_TOKEN"):
        monkeypatch.setenv(k, "")  # set_secret writes os.environ: put it back afterwards


def run(home, monkeypatch, go):  # noqa: F811
    ch, agent, _ = make(home, monkeypatch)

    async def main():
        async with client(ch) as c:
            return await go(c, ch, agent)
    return asyncio.run(main())


def test_secrets_never_return_values(home, monkeypatch):  # noqa: F811
    async def go(c, ch, agent):
        r = await c.post("/api/secrets", json={"name": "MY_KEY", "value": "supersecretvalue99"}, headers=W)
        assert r.status_code == 200 and "supersecretvalue99" not in r.text
        listed = [s for s in r.json()["secrets"] if s["name"] == "MY_KEY"][0]
        assert listed == {"name": "MY_KEY", "set": True, "last4": "ue99", "used_by": []}
        assert "supersecretvalue99" not in (await c.get("/api/connections")).text
        for bad in ("lower", "has space", "1ABC", "", "A-B"):
            assert (await c.post("/api/secrets", json={"name": bad, "value": "x"}, headers=W)).status_code == 400
        assert (await c.post("/api/secrets", json={"name": "MY_KEY", "value": "two\nlines"}, headers=W)).status_code == 400
        assert (await c.post("/api/secrets", json={"name": "MY_KEY", "value": "  "}, headers=W)).status_code == 400
        r = await c.post("/api/secrets", json={"name": "MY_KEY", "remove": True}, headers=W)
        assert "MY_KEY" not in [s["name"] for s in r.json()["secrets"]]
        assert (await c.post("/api/secrets", json={"name": "MY_KEY", "value": "x"})).status_code in (400, 403)  # no X-Daimon
    run(home, monkeypatch, go)
    assert "MY_KEY" not in home.secrets()


def test_servers_add_toggle_consent_remove_defaults(home, monkeypatch):  # noqa: F811
    async def go(c, ch, agent):
        post = lambda path, body: c.post(path, json=body, headers=W)  # noqa: E731
        r = await post("/api/servers", {"name": "gh", "url": "https://example.com/mcp",
                                       "headers": {"Authorization": "Bearer abcdefgh12345678"}})
        assert r.json()["ok"]
        entry = home.mcp()["mcpServers"]["gh"]
        assert entry == {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${GH_TOKEN}"}}
        assert home.secrets()["GH_TOKEN"] == "abcdefgh12345678"  # without "Bearer ", stored once
        assert "abcdefgh12345678" not in (await c.get("/api/connections")).text

        r = await post("/api/servers", {"name": "ghx", "command": "npx", "args": ["-y", "", "pkg"], "env": {"API_KEY": "k-12345678"}})
        assert r.json()["ok"] and home.mcp()["mcpServers"]["ghx"] == {"command": "npx", "args": ["-y", "pkg"], "env": {"API_KEY": "${GHX_API_KEY}"}}
        assert home.secrets()["GHX_API_KEY"] == "k-12345678"
        monkeypatch.setenv("GHX_API_KEY", "")

        assert (await post("/api/servers", {"name": "gh", "url": "https://x"})).status_code == 400  # duplicate
        assert (await post("/api/servers", {"name": "bad name!", "url": "https://x"})).status_code == 400
        assert (await post("/api/servers", {"name": "empty"})).status_code == 400

        d = (await c.get("/api/servers/gh")).json()
        assert d["url"] == "https://example.com/mcp" and d["headers"] == {"Authorization": "Bearer ${GH_TOKEN}"}
        assert d["tools"] == [] and d["disabled"] is False and "workers" in d and d["defaults"]["other"] == "ask"
        d = (await c.get("/api/servers/ghx")).json()
        assert d["command"] == "npx" and d["env"] == {"API_KEY": "${GHX_API_KEY}"}
        assert (await c.get("/api/servers/nope")).status_code == 400

        await post("/api/servers/gh", {"action": "toggle", "on": False})
        assert home.mcp()["mcpServers"]["gh"]["disabled"] is True
        names = {s["name"]: s for s in (await c.get("/api/connections")).json()["servers"]}
        assert names["gh"]["disabled"] and names["demo"]["kind"] == "command" and names["gh"]["kind"] == "url"
        await post("/api/servers/gh", {"action": "toggle", "on": True})
        assert "disabled" not in home.mcp()["mcpServers"]["gh"]

        assert (await post("/api/servers/gh", {"action": "consent", "tool": "search", "level": "ask"})).json()["ok"]
        assert home.mcp()["mcpServers"]["gh"]["consent"]["search"] == "ask"
        assert (await post("/api/servers/gh", {"action": "consent", "tool": "search", "level": "bogus"})).status_code == 400
        assert (await post("/api/servers/gh", {"action": "nonsense"})).status_code == 400

        await post("/api/mcp-defaults", {"read_only": "notify", "other": "auto"})
        assert home.mcp()["defaults"] == {"read_only": "notify", "other": "auto"}
        assert (await post("/api/mcp-defaults", {"read_only": "x", "other": "auto"})).status_code == 400

        await post("/api/servers/gh", {"action": "remove"})
        assert "gh" not in home.mcp()["mcpServers"]
        # the secret stays, but nothing uses it now: the page offers to remove it
        sec = {s["name"]: s for s in (await c.get("/api/connections")).json()["secrets"]}
        assert sec["GH_TOKEN"]["used_by"] == [] and sec["GH_TOKEN"]["set"]
    run(home, monkeypatch, go)


def test_connections_shape(home, monkeypatch):  # noqa: F811
    async def go(c, ch, agent):
        j = (await c.get("/api/connections")).json()
        assert set(j) == {"telegram", "addons", "servers", "secrets", "defaults", "pending"}
        assert [a["key"] for a in j["addons"]] == ["canvas", "messages", "calendar"] or {a["key"] for a in j["addons"]} == {"canvas", "messages", "calendar"}
        assert all({"key", "title", "blurb", "added", "tools", "error"} <= set(a) for a in j["addons"])
        assert j["telegram"]["token"] is False and j["telegram"]["events"] == ["notify", "error"]
        assert j["defaults"] == {"read_only": "auto", "other": "ask"}
        demo = [s for s in j["servers"] if s["name"] == "demo"][0]
        assert {"kind", "target", "disabled", "addon", "tools", "asks", "error", "connected"} <= set(demo)
    run(home, monkeypatch, go)


def test_telegram_flow(home, monkeypatch):  # noqa: F811
    from daimon.setup import wizard
    calls = []

    def fake(token, method, wait=15, **params):
        calls.append((method, params))
        if method == "getMe":
            return {"username": "daimon_bot"}
        return {}
    monkeypatch.setattr(wizard, "telegram", fake)
    monkeypatch.setattr(wizard, "wait_for_chat", lambda token, seconds=25, api=None: (4242, "Afrika"))

    async def go(c, ch, agent):
        post = lambda body: c.post("/api/telegram", json=body, headers=W)  # noqa: E731
        assert (await post({"action": "token", "token": "nope"})).status_code == 400
        assert (await post({"action": "wait"})).status_code == 400  # no token yet
        r = (await post({"action": "token", "token": TOKEN})).json()
        assert r["bot"] == "daimon_bot" and "Telegram" in r["pending"]
        assert home.secrets()["TELEGRAM_TOKEN"] == TOKEN
        assert home.config()["channels"]["telegram"]["token"] == "${TELEGRAM_TOKEN}"
        assert (await post({"action": "bot"})).json()["bot"] == "daimon_bot"
        assert (await post({"action": "test"})).status_code == 400  # no chat yet
        found = (await post({"action": "wait"})).json()["found"]
        assert found == {"chat": 4242, "who": "Afrika"} and home.secrets()["TELEGRAM_CHAT_ID"] == "4242"
        assert any(m == "sendMessage" and p["chat_id"] == 4242 for m, p in calls)
        assert (await post({"action": "test"})).json()["ok"]
        await post({"action": "events", "events": ["notify", "task", "bogus"]})
        assert home.config()["channels"]["telegram"]["events"] == ["notify", "task"]
        t = (await c.get("/api/connections")).json()["telegram"]
        assert t["token"] and t["chat"] and t["events"] == ["notify", "task"]
        assert TOKEN not in (await c.get("/api/connections")).text
        await post({"action": "remove"})
        assert "TELEGRAM_TOKEN" not in home.secrets() and "TELEGRAM_CHAT_ID" not in home.secrets()
        assert (await post({"action": "what"})).status_code == 400
    run(home, monkeypatch, go)


def wait_done(job, t=15):
    end = time.time() + t
    while not job.done and time.time() < end:
        time.sleep(0.02)
    assert job.done


def test_jobs_runner(tmp_path):
    jobs, gone = Jobs(), tmp_path / "temp.txt"
    gone.write_text("x")
    seen = []
    py = sys.executable
    job = jobs.start("t", [([py, "-c", "import sys;print('\\x1b[31mred\\x1b[0m');print(sys.stdin.read().upper())"], "secret-in\n")],
                     lambda j: seen.append(j.lines[:]), [gone])
    wait_done(job)
    assert job.ok and job.lines == ["red", "SECRET-IN"] and seen == [["red", "SECRET-IN"]] and not gone.exists()
    assert jobs.get(job.id).json()["done"] is True

    bad = jobs.start("t", [([py, "-c", "print('first');raise SystemExit(3)"], ""), ([py, "-c", "print('never')"], "")])
    wait_done(bad)
    assert not bad.ok and bad.lines == ["first"]

    slow = jobs.start("t", [([py, "-c", "import time;print('go',flush=True);time.sleep(30)"], "")])
    end = time.time() + 10
    while not slow.lines and time.time() < end:
        time.sleep(0.02)
    jobs.cancel(slow.id)
    wait_done(slow)
    assert not slow.ok and "Cancelled." in slow.lines
    with pytest.raises(ValueError):
        jobs.get("nope")


class Recorder(Jobs):
    def start(self, title, steps, after=None, cleanup=None):
        self.got = steps
        self.cleanup = cleanup
        self.contents = {str(p): p.read_text(encoding="utf-8") for p in cleanup or []}
        return super().start(title, [], None, cleanup)


def test_connect_addon_commands(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(addons, "installed", lambda a, python=sys.executable: True)
    monkeypatch.setattr(addons, "messages_running", lambda: True)
    jobs = Recorder()
    connect = lambda key, **f: connections.connect_addon(home, jobs, key, f, lambda j: None) and jobs.got  # noqa: E731

    [(argv, stdin)] = connect("canvas", url="https://school.instructure.com", token="tok-secret")
    assert argv[-3:] == ["login", "--url", "https://school.instructure.com"] and stdin == "tok-secret\n" and "tok-secret" not in argv
    with pytest.raises(ValueError):
        connect("canvas", url="https://school.instructure.com")

    [(argv, stdin)] = connect("calendar", kind="apple", username="me@icloud.com", password="app-pass", name="icloud")
    assert argv[-6:] == ["add", "apple", "--name", "icloud", "--username", "me@icloud.com"] and stdin == "app-pass\n"
    assert "app-pass" not in argv
    [(argv, stdin)] = connect("calendar", kind="caldav", url="https://dav.example.com", username="u", password="p")
    assert argv[-5:] == ["caldav", "--username", "u", "--url", "https://dav.example.com"] and stdin == "p\n"
    with pytest.raises(ValueError):
        connect("calendar", kind="caldav", username="u", password="p")  # needs the address
    [(argv, stdin)] = connect("calendar", kind="microsoft", client_id="abc-123")
    assert argv[-5:] == ["microsoft", "--client-id", "abc-123", "--tenant", "common"] and stdin == ""
    [(argv, _)] = connect("calendar", kind="ics", url="webcal://x/y.ics", title="Class")
    assert argv[-4:] == ["--url", "webcal://x/y.ics", "--title", "Class"]
    [(argv, stdin)] = connect("calendar", kind="google", client_id="cid", client_secret="csec")
    assert "csec" not in argv and "--client-file" in argv and stdin == ""
    client_file = argv[argv.index("--client-file") + 1]
    assert json.loads(jobs.contents[client_file]) == {"installed": {"client_id": "cid", "client_secret": "csec"}}
    time.sleep(0.2)
    assert not any(p.exists() for p in jobs.cleanup)  # the client file is removed once the job ends
    with pytest.raises(ValueError):
        connect("calendar", kind="nonsense")

    [(argv, stdin)] = connect("messages", cookies="curl 'https://messages.google.com/web/config' -H 'cookie: a=b'")
    assert argv[-3] == "pair" and argv[-2] == "--cookies" and "a=b" not in " ".join(argv)
    assert "a=b" in next(iter(jobs.contents.values()))  # the cookies travel in a temp file
    with pytest.raises(ValueError):
        connect("messages")
    with pytest.raises(ValueError):
        connect("nothing")


def test_connect_installs_first_and_adds_server(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(addons, "installed", lambda a, python=sys.executable: False)
    monkeypatch.setattr(addons, "install_command", lambda a, python=sys.executable: [sys.executable, "-c", "print('installing')"])
    jobs = Recorder()
    connections.connect_addon(home, jobs, "canvas", {"url": "https://s.instructure.com", "token": "t"}, lambda j: None)
    assert len(jobs.got) == 2 and "installing" in jobs.got[0][0][-1]  # the install step runs before the sign-in

    # a successful job adds the server and tells the page
    monkeypatch.setattr(addons, "installed", lambda a, python=sys.executable: True)
    done = []
    real = Jobs()
    monkeypatch.setattr(real, "start", lambda title, steps, after, cleanup: Jobs.start(real, title, [([sys.executable, "-c", "print('ok')"], "")], after, cleanup))
    job = connections.connect_addon(home, real, "canvas", {"url": "https://s.instructure.com", "token": "t"}, done.append)
    wait_done(job)
    assert job.ok and 'Added the "canvas" server.' in job.lines and done == [job]
    assert "canvas" in home.mcp()["mcpServers"]


def test_job_routes_and_status(home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(connections, "addon_status", lambda key: f"status of {key}")

    async def go(c, ch, agent):
        assert (await c.get("/api/addons/calendar/status")).json() == {"text": "status of calendar"}
        assert (await c.get("/api/jobs/j999")).status_code == 400
        job = ch.jobs.start("x", [([sys.executable, "-c", "print('hi')"], "")])
        await asyncio.sleep(0.5)
        assert (await c.get(f"/api/jobs/{job.id}")).json()["lines"] == ["hi"]
        assert (await c.post(f"/api/jobs/{job.id}/cancel", json={}, headers=W)).json()["ok"]
    run(home, monkeypatch, go)
