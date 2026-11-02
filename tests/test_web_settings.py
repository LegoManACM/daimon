"""The settings API: config.yaml, models.yaml, provider keys and the editable files."""
import asyncio
import functools

import pytest

from daimon.setup import providers
from daimon.web import settings

from test_agent import home  # noqa: F401 - fixture
from test_web import W, client, make


def sync(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        asyncio.run(fn(*a, **k))
    return run


async def post(c, path, body):
    return await c.post(path, json=body, headers=W)


async def setting(c, key, value):
    return await post(c, "/api/settings", {"key": key, "value": value})


@sync
async def test_each_kind_of_setting(home, monkeypatch):  # noqa: F811
    ch, _agent, _ = make(home, monkeypatch)
    async with client(ch) as c:
        got = (await c.get("/api/settings")).json()
        assert got["sections"][0]["id"] == "behaviour" and "config.yaml" in got["editable"]
        assert got["models"]["main"] and "default" in got["models"]

        r = await setting(c, "max_steps_per_turn", "45")                                     # int
        assert r.status_code == 200 and r.json()["value"] == 45 and r.json()["commit"]
        assert home.config()["max_steps_per_turn"] == 45
        assert (await setting(c, "model_calls.cache", False)).status_code == 200              # bool
        assert home.config()["model_calls"]["cache"] is False
        assert (await setting(c, "self_edit", "free")).status_code == 200                    # choice
        assert home.config()["self_edit"] == "free"
        assert (await setting(c, "limits.max_concurrent_workers", "")).json()["value"] == "unlimited"   # limit
        assert (await setting(c, "limits.max_concurrent_workers", "3")).json()["value"] == 3
        assert (await setting(c, "knowledge.embeddings", "  none ")).json()["value"] == "none"   # text
        assert (await setting(c, "web.listen", "192.168.1.20")).status_code == 200          # an address

        for key, value in [("max_steps_per_turn", "lots"), ("max_steps_per_turn", "-1"), ("model_calls.cache", "yes"),
                           ("self_edit", "whenever"), ("web.listen", ""), ("web.port", "70000"), ("nope.nothing", 1)]:
            r = await setting(c, key, value)
            assert r.status_code == 400 and r.json()["error"], (key, value)


def test_keys_missing_from_config_show_their_defaults(home):  # noqa: F811
    """A home made before a setting existed: it isn't in config.yaml, so the default is in use (not 'changed')."""
    text = home.read("config.yaml")
    start = text.index("model_calls:")
    home.write("config.yaml", text[:start] + text[text.index("\n\n", start) + 2:])  # drop the whole section
    rows = {r["key"]: r for s in settings.view(home) for r in s["items"]}
    assert rows["model_calls.first_token_timeout_s"]["value"] == rows["model_calls.first_token_timeout_s"]["default"] == 300
    assert rows["model_calls.ollama.warm_up"]["value"] is True


@sync
async def test_restart_says_where_the_page_will_be(home, monkeypatch):  # noqa: F811
    """After a port change the restart answers with the new address, so the page can follow it there."""
    from daimon.setup import service
    ch, _agent, _ = make(home, monkeypatch)
    restarts = []
    monkeypatch.setattr(service, "restart_later", lambda h, after_s=5: restarts.append(after_s))
    async with client(ch) as c:
        monkeypatch.setattr(service, "running", lambda h, key="daimon": False)
        r = await post(c, "/api/restart", {})
        assert r.status_code == 409 and not restarts               # `daimon run` in a terminal: nothing to restart
        monkeypatch.setattr(service, "running", lambda h, key="daimon": True)
        await setting(c, "web.port", 8899)
        r = await post(c, "/api/restart", {})
        assert r.status_code == 200 and restarts == [2]
        assert r.json()["urls"] and all(u.endswith(":8899") for u in r.json()["urls"])


@sync
async def test_changing_the_port_waits_for_a_restart(home, monkeypatch):  # noqa: F811
    ch, _agent, _ = make(home, monkeypatch)
    async with client(ch) as c:
        assert (await c.get("/api/settings")).json()["pending"] == []
        r = await setting(c, "web.port", 9123)
        assert r.json()["pending"] == ["Port"]
        assert (await c.get("/api/settings")).json()["pending"] == ["Port"]
        await setting(c, "web.port", settings.lookup(ch.boot["config"], ["web", "port"]) or 8770)   # back to the start
        assert (await c.get("/api/settings")).json()["pending"] == []


@sync
async def test_models(home, monkeypatch):  # noqa: F811
    ch, _agent, _ = make(home, monkeypatch)
    async with client(ch) as c:
        main = (await c.get("/api/settings")).json()["models"]["main"]
        new = {"id": "openai/test-model", "tier": "cheap", "good_for": "triage, summaries", "approval": "ask",
               "price": {"in": 1, "out": 2}}
        r = await post(c, "/api/models/save", {"entry": new})
        assert r.status_code == 200
        entry = next(m for m in r.json()["models"] if m["id"] == "openai/test-model")
        assert entry["tier"] == "cheap" and entry["good_for"] == ["triage", "summaries"] and entry["approval"] == "ask"
        assert entry["price"] == {"in": 1.0, "out": 2.0}
        assert (await post(c, "/api/models/save", {"entry": {"id": main}, "replace": "openai/test-model"})).status_code == 400  # taken
        assert (await post(c, "/api/models/save", {"entry": {"id": "nonsense"}})).status_code == 400
        assert (await post(c, "/api/models/save", {"entry": {"id": "a b/c"}})).status_code == 400
        # ids as the providers' own lists give them, OpenRouter's ~aliases included
        for odd in ("openrouter/~deepseek/deepseek-flash-latest", "openrouter/deepseek/deepseek-v4.1-flash:free"):
            assert (await post(c, "/api/models/save", {"entry": {"id": odd}})).status_code == 200
        r = await post(c, "/api/models/save", {"entry": {"id": "openai/test-model", "approval": ""},
                                               "replace": "openai/test-model"})
        assert next(m for m in r.json()["models"] if m["id"] == "openai/test-model")["approval"] is None

        r = await post(c, "/api/models/default", {"model": "openai/test-model"})
        assert r.json()["default"] == "openai/test-model"
        assert (await post(c, "/api/models/default", {"model": "x/unknown"})).status_code == 400

        r = await post(c, "/api/models/remove", {"id": main})
        assert r.status_code == 400 and "main" in r.json()["error"].lower()
        r = await post(c, "/api/models/remove", {"id": "openai/test-model"})
        assert r.status_code == 200 and all(m["id"] != "openai/test-model" for m in r.json()["models"])
        assert r.json()["default"] == main                      # the default fell back to main's model
        assert (await post(c, "/api/models/remove", {"id": "openai/test-model"})).status_code == 400


@sync
async def test_files(home, monkeypatch):  # noqa: F811
    ch, _agent, _ = make(home, monkeypatch)
    async with client(ch) as c:
        for path, text in [("config.yaml", "a: [b\n"), ("config.yaml", "- just\n- a list\n"), ("mcp.json", "{nope"),
                           ("mcp.json", "[1]"), ("workers/main.md", "x"), ("../outside.md", "x")]:
            r = await post(c, "/api/file/save", {"path": path, "text": text})
            assert r.status_code == 400 and r.json()["error"], path
        assert "a: [b" not in home.read("config.yaml")
        r = await post(c, "/api/file/save", {"path": "MEMORY.md", "text": "- likes tea"})
        assert r.status_code == 200 and r.json()["commit"]
        assert home.read("MEMORY.md") == "- likes tea\n"
        assert (await post(c, "/api/file/save", {"path": "mcp.json", "text": '{"mcpServers": {}}'})).status_code == 200


@sync
async def test_provider_keys(home, monkeypatch):  # noqa: F811
    ch, _agent, _ = make(home, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen = []

    def fake(p, key):
        seen.append(key)
        if key == "sk-bad":
            raise providers.KeyProblem("the provider did not accept this key")
        return [providers.Model("openai/a"), providers.Model("openai/b")]
    monkeypatch.setattr(providers, "fetch_models", fake)
    async with client(ch) as c:
        r = await post(c, "/api/providers/key", {"provider": "openai", "key": "sk-bad"})
        assert r.status_code == 400 and "did not accept" in r.json()["error"]
        r = await post(c, "/api/providers/key", {"provider": "openai", "key": "  sk-goodkey1234 "})
        assert r.status_code == 200 and r.json()["models"] == 2
        assert seen[-1] == "sk-goodkey1234"
        page = await c.get("/api/settings")
        row = next(p for p in page.json()["providers"] if p["key"] == "openai")
        assert row["set"] and row["last4"] == "1234" and "sk-goodkey" not in page.text
        assert (await post(c, "/api/providers/key", {"provider": "openai", "key": ""})).status_code == 400
        assert (await post(c, "/api/providers/key", {"provider": "ollama", "key": "x"})).status_code == 400
        assert (await post(c, "/api/providers/key", {"provider": "nope", "key": "x"})).status_code == 400
        found = (await c.get("/api/providers/openai/models?q=b")).json()["models"]
        assert [m["id"] for m in found] == ["openai/b"]
        assert (await c.get("/api/providers/nope/models")).status_code == 400


def test_set_config_keeps_comments(home):  # noqa: F811
    home.write("config.yaml", "# top\nweb:\n  port: 8770   # the port\n  auth: token\n"
                              "model_calls: {retries: 2, cache: true}  # inline\n"
                              "limits:\n  harness:\n    claude-code:\n      max_runs_per_day: 5\n")
    home.set_config(["web", "port"], 9000)
    home.set_config(["model_calls", "ollama", "warm_up"], False)         # into a flow mapping
    home.set_config(["limits", "harness", "claude-code", "max_runs_per_day"], None)
    home.set_config(["knowledge", "folder"], "notes")                    # a section that does not exist yet
    text = home.read("config.yaml")
    assert "# top" in text and "# the port" in text and "# inline" in text
    cfg = home.config()
    assert cfg["web"] == {"port": 9000, "auth": "token"}
    assert cfg["model_calls"] == {"retries": 2, "cache": True, "ollama": {"warm_up": False}}
    assert cfg["limits"]["harness"]["claude-code"]["max_runs_per_day"] is None and cfg["knowledge"]["folder"] == "notes"


def test_settings_view_covers_every_setting(home):  # noqa: F811
    keys = [i["key"] for s in settings.view(home) for i in s["items"]]
    assert len(keys) == len(set(keys)) and set(settings.BY_KEY) <= set(keys)


def test_lists_are_picked_not_typed(home):  # noqa: F811
    """The inbox's worker is picked from the workers there are; the page offers this computer's addresses."""
    curator = next(i for s in settings.view(home) for i in s["items"] if i["key"] == "knowledge.curator")
    assert curator["kind"] == "select" and ("curator", "curator") in curator["options"] and ("main", "main") in curator["options"]
    settings.set_setting(home, "knowledge.curator", "main")
    assert home.config()["knowledge"]["curator"] == "main"
    with pytest.raises(ValueError, match="pick one"):
        settings.set_setting(home, "knowledge.curator", "nobody")
    assert all(not a.startswith("127.") for a in settings.addresses())


def test_embedding_models_come_from_the_provider(monkeypatch):
    """Meaning-based search picks from each provider's embedding models, as LiteLLM ids."""
    pages = {
        "https://openrouter.ai/api/v1/key": {},
        "https://openrouter.ai/api/v1/embeddings/models": {"data": [
            {"id": "openai/text-embedding-3-small", "name": "Small", "pricing": {"prompt": "0.00000002"},
             "architecture": {"input_modalities": ["text"]}},
            {"id": "acme/image-only-embed", "architecture": {"input_modalities": ["image"]}}]},
        "https://api.openai.com/v1/models": {"data": [{"id": "gpt-5"}, {"id": "text-embedding-3-large"}]},
        "https://generativelanguage.googleapis.com/v1beta/models": {"models": [
            {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]},
    }
    monkeypatch.setattr(providers, "_get", lambda url, **kw: pages[url])
    monkeypatch.setattr(providers, "ollama_models", lambda base=None: ["ollama_chat/qwen3.5:9b", "ollama_chat/nomic-embed-text:latest"])
    for p in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(p, "k")
    ids = lambda p: [m["id"] for m in settings.provider_models(p, "", embeddings=True)]  # noqa: E731
    assert ids("openrouter") == ["openrouter/openai/text-embedding-3-small"]           # text in only
    assert ids("openai") == ["openai/text-embedding-3-large"]
    assert ids("gemini") == ["gemini/gemini-embedding-001"]
    assert ids("ollama") == ["ollama/nomic-embed-text:latest"]                         # ollama/, not ollama_chat/
    assert ids("anthropic") == []
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ValueError, match="key first"):
        settings.provider_models("openai", "", embeddings=True)


def test_workers_section_follows_the_harnesses(home):  # noqa: F811
    def workers():
        return next(s for s in settings.view(home) if s["id"] == "workers")
    assert workers()["title"] == "Workers & Claude Code"
    assert workers()["items"][1]["key"] == "limits.harness.claude-code.max_runs_per_day"
    home.set_config(["harnesses", "gemini"], {"driver": "cli", "command": ["gemini", "-p", "{prompt}"]})
    home.set_config(["harnesses", "my-agent"], {"driver": "cli", "command": ["x"]})
    w = workers()
    assert w["title"] == "Workers, Claude Code, Gemini & My Agent"
    caps = {i["key"]: i for i in w["items"] if i["key"].endswith("max_runs_per_day")}
    assert caps["limits.harness.gemini.max_runs_per_day"]["label"] == "Gemini runs per day"
    assert caps["limits.harness.gemini.max_runs_per_day"]["value"] is None        # no cap until you set one
    settings.set_setting(home, "limits.harness.gemini.max_runs_per_day", "5")
    assert home.config()["limits"]["harness"]["gemini"]["max_runs_per_day"] == 5
    assert home.config()["limits"]["harness"]["claude-code"]["max_runs_per_day"] == 20  # untouched
    with pytest.raises(ValueError):
        settings.set_setting(home, "limits.harness.nobody.max_runs_per_day", "5")  # not a harness here
