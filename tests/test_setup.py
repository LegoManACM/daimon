"""`daimon setup` and `daimon service`, with no TTY, network, model calls or real service managers."""
import io
import json
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from daimon import cli
from daimon.home import Home
from daimon.setup import addons, providers, service, wizard
from daimon.setup.providers import KeyProblem, Model


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    h = Home(tmp_path / "home")
    h.init(model="ollama_chat/test-model")
    return h


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "DAIMON_CANVAS_HOME", "DAIMON_MESSAGES_HOME"):
        monkeypatch.delenv(k, raising=False)


# ---- secrets, models -------------------------------------------------------------------------------------
def test_set_secret_fills_placeholder_and_appends(home, clean_env):
    home.set_secret("ANTHROPIC_API_KEY", "sk-1")
    home.set_secret("TELEGRAM_TOKEN", "123:abc")
    home.set_secret("ANTHROPIC_API_KEY", "sk-2")
    text = home.secrets_file.read_text(encoding="utf-8")
    assert text.count("ANTHROPIC_API_KEY") == 1 and "ANTHROPIC_API_KEY=sk-2" in text
    assert "TELEGRAM_TOKEN=123:abc" in text and "# OPENAI_API_KEY=" in text
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-2"


def test_use_model_matches_init(home):
    wizard.use_model(home, "openai/gpt-5-mini")
    assert home.worker("main").model == "openai/gpt-5-mini"
    assert home.model_entry("openai/gpt-5-mini")
    assert "setup: main model" in home.history()


def test_ollama_unreachable(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)
    assert wizard.ollama_models() is None


# ---- telegram --------------------------------------------------------------------------------------------
def test_telegram_errors_hide_the_token(monkeypatch):
    def boom(url, **k):
        raise httpx.ConnectError(f"can't reach {url}")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(ValueError) as e:
        wizard.telegram("123:SECRET", "getMe")
    assert "SECRET" not in str(e.value)


def test_wait_for_chat_takes_the_message_and_marks_it_read():
    calls = []

    def api(token, method, **params):
        calls.append(params)
        if len(calls) == 1:
            return []
        if len(calls) == 2:
            return [{"update_id": 7, "message": {"chat": {"id": 42, "first_name": "Sam"}, "text": "hi"}}]
        return []
    assert wizard.wait_for_chat("t", seconds=30, api=api) == (42, "Sam")
    assert calls[-1]["offset"] == 8 and calls[-1]["timeout"] == 0


def test_wait_for_chat_times_out():
    assert wizard.wait_for_chat("t", seconds=0, api=lambda *a, **k: []) is None


# ---- add-ons ---------------------------------------------------------------------------------------------
def test_addon_source_checkout_and_wheel(tmp_path, monkeypatch):
    canvas = addons.ADDONS["canvas"]
    assert addons.source(canvas) == ["-e", str(addons.SOURCE / "canvas")]  # this repo is a checkout
    monkeypatch.setattr(addons, "SOURCE", tmp_path)
    monkeypatch.setattr(addons, "version", lambda: "1.2.3")
    assert addons.source(addons.ADDONS["messages"]) == [
        "https://github.com/LegoManACM/daimon/releases/download/v1.2.3/daimon_messages-1.2.3-py3-none-any.whl"]


def test_install_command_prefers_uv(monkeypatch):
    canvas = addons.ADDONS["canvas"]
    monkeypatch.setattr(addons.shutil, "which", lambda name: "/bin/uv")
    assert addons.install_command(canvas, "py")[:5] == ["/bin/uv", "pip", "install", "--python", "py"]
    monkeypatch.setattr(addons.shutil, "which", lambda name: None)
    assert addons.install_command(canvas, "py")[:4] == ["py", "-m", "pip", "install"]


def test_add_server_keeps_existing_entries(home):
    home.write_mcp({"mcpServers": {"canvas": {"command": "mine", "args": []}, "other": {"command": "x"}}})
    assert not addons.add_server(home, addons.ADDONS["canvas"], "py")
    assert addons.add_server(home, addons.ADDONS["messages"], "py")
    servers = json.loads((home.root / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers["canvas"] == {"command": "mine", "args": []} and servers["other"] == {"command": "x"}
    assert servers["messages"] == {"command": "py", "args": ["-m", "daimon_messages", "mcp"],
                                   "consent": {"send_message": "ask"}}


# ---- service ---------------------------------------------------------------------------------------------
@pytest.fixture
def sh(monkeypatch):
    calls = []
    results = {}

    def fake(argv):
        calls.append(argv)
        for prefix, r in results.items():
            if " ".join(argv).startswith(prefix):
                return subprocess.CompletedProcess(argv, *r)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(service, "sh", fake)
    fake.calls, fake.results = calls, results
    return fake


def test_jobs(home):
    agent, messages = service.jobs(home, python="py")
    assert agent.argv == ["py", "-m", "daimon", "--home", str(home.root), "run"]
    assert messages.argv == ["py", "-m", "daimon_messages", "run"]
    assert [j.key for j in service.jobs(home, messages=False)] == ["daimon"]
    assert [j.key for j in service.jobs(home, agent=False)] == ["daimon-messages"]


def test_windows_launcher_text(home, tmp_path):
    job = service.jobs(home, messages=False, python=r"C:\py 3\python.exe")[0]
    cmd = service.windows_cmd(job, {"DAIMON_SECRETS": r"C:\s%x.env"})
    assert r'"C:\py 3\python.exe" "-m" "daimon" "--home"' in cmd
    assert 'set "DAIMON_SECRETS=C:\\s%%x.env"' in cmd and "goto loop" in cmd and "2>&1" in cmd
    assert service.windows_vbs(Path(r"C:\a b\daimon.cmd")) == \
        'CreateObject("WScript.Shell").Run "cmd /c ""C:\\a b\\daimon.cmd""", 0, True\r\n'
    xml = service.windows_task_xml(job, Path(r"C:\a&b\daimon.vbs"), r"PC\sam")
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    assert root.find(".//t:LogonTrigger/t:UserId", ns).text == r"PC\sam"
    assert root.find(".//t:RunLevel", ns).text == "LeastPrivilege"
    assert root.find(".//t:ExecutionTimeLimit", ns).text == "PT0S"
    assert root.find(".//t:DisallowStartIfOnBatteries", ns).text == "false"
    assert root.find(".//t:Arguments", ns).text == r'"C:\a&b\daimon.vbs"'


def test_windows_install_task_or_startup_folder(home, tmp_path, monkeypatch, sh):
    monkeypatch.setattr(service, "platform", lambda: "windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    lines = service.install(home, messages=False)
    assert any(c[:2] == ["schtasks", "/Create"] and "/XML" in c for c in sh.calls)
    assert "Task Scheduler" in lines[0]
    assert (home.cache / "service" / "daimon.cmd").exists() and (home.cache / "service" / "daimon.vbs").exists()
    assert not (service.startup_dir() / "Daimon.vbs").exists()

    sh.results["schtasks /Create"] = (1, "", "ERROR: Access is denied.")
    lines = service.install(home, messages=False)
    assert (service.startup_dir() / "Daimon.vbs").exists() and "Access is denied" in lines[0]


def test_windows_stop_kills_the_launcher_tree(home, monkeypatch, sh):
    monkeypatch.setattr(service, "platform", lambda: "windows")
    sh.results["powershell"] = (0, "123\n456\n", "")
    assert service.stop(home) == ["daimon: stopped", "daimon-messages: stopped"]
    assert ["taskkill", "/PID", "123", "/T", "/F"] in sh.calls


def test_plist(home, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", "/s.env")
    job = service.jobs(home, python="/venv/bin/python")[0]
    data = plistlib.loads(service.plist(job, service.env(), home).encode())
    assert data["Label"] == "com.daimon.agent" and data["RunAtLoad"] and data["KeepAlive"]
    assert data["ProgramArguments"] == ["/venv/bin/python", "-m", "daimon", "--home", str(home.root), "run"]
    assert data["StandardOutPath"] == str(home.cache / "daimon.log")
    assert data["EnvironmentVariables"]["DAIMON_SECRETS"] == "/s.env" and "PATH" in data["EnvironmentVariables"]


def test_macos_install_and_start(home, tmp_path, monkeypatch, sh):
    monkeypatch.setattr(service, "platform", lambda: "macos")
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    sh.results["launchctl print"] = (113, "", "Could not find service")
    service.install(home, messages=True, start_now=True)
    assert (tmp_path / "Library/LaunchAgents/com.daimon.agent.plist").exists()
    assert (tmp_path / "Library/LaunchAgents/com.daimon.messages.plist").exists()
    assert ["launchctl", "bootstrap", "gui/501", str(tmp_path / "Library/LaunchAgents/com.daimon.agent.plist")] \
        in sh.calls


def test_systemd_unit(home, monkeypatch):
    job = service.jobs(home, python="/home/a b/venv/bin/python")[0]
    text = service.unit(job, {"DAIMON_SECRETS": "/s%.env"}, home)
    assert 'ExecStart="/home/a b/venv/bin/python" "-m" "daimon" "--home"' in text
    assert 'Environment="DAIMON_SECRETS=/s%%.env"' in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert f"StandardOutput=append:{home.cache / 'daimon.log'}" in text


def test_linux_install_enables(home, tmp_path, monkeypatch, sh):
    monkeypatch.setattr(service, "platform", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    service.install(home, messages=False, start_now=True)
    assert (tmp_path / "cfg/systemd/user/daimon.service").exists()
    assert ["systemctl", "--user", "enable", "daimon.service"] in sh.calls
    assert ["systemctl", "--user", "start", "daimon.service"] in sh.calls
    sh.results["systemctl --user is-active daimon.service"] = (0, "active\n", "")
    assert service.status(home)[0].startswith("daimon: starts at login, running")
    assert service.uninstall(home) == ["daimon: removed"]
    assert not (tmp_path / "cfg/systemd/user/daimon.service").exists()


# ---- the wizard ------------------------------------------------------------------------------------------
class Scripted:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def _next(self, message):
        self.asked.append(message)
        return self.answers.pop(0)

    def select(self, message, choices, default=None):
        answer = self._next(message)
        assert answer in [v for _, v in choices], (message, answer)
        return answer

    def pick(self, message, choices, default=None):
        answer = self._next(message)
        assert answer in [v for _, v, _ in choices], (message, answer)
        return answer

    def text(self, message, default=""):
        return self._next(message)

    def secret(self, message):
        return self._next(message)

    def confirm(self, message, default=True):
        return self._next(message)

    def checkbox(self, message, choices):
        return self._next(message)


def make_wizard(ask):
    return wizard.Wizard(ask, console=Console(file=io.StringIO(), width=100))


def test_wizard_minimal(tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    monkeypatch.setattr(wizard, "check_model", lambda m: "")
    monkeypatch.setattr(wizard, "claude_path", lambda: None)
    monkeypatch.setattr(wizard, "fetch_models", lambda p, key: [Model("anthropic/claude-opus-5-5")])
    ask = Scripted(str(tmp_path / "home"), "home", "anthropic", "  'sk-ant-test'\n", "anthropic/claude-opus-5-5", "no", [])
    w = make_wizard(ask)
    assert w.run() == 0 and not ask.answers
    h = Home(tmp_path / "home")
    assert h.worker("main").model == "anthropic/claude-opus-5-5"
    assert "ANTHROPIC_API_KEY=sk-ant-test" in h.secrets_file.read_text(encoding="utf-8")
    out = w.out.file.getvalue()
    assert "Ready" in out and "Got 11 characters" in out


def test_wizard_telegram_and_background(home, monkeypatch, clean_env):
    sent = []

    def tg(token, method, **params):
        sent.append(method)
        return {"username": "my_daimon_bot"} if method == "getMe" else True
    installed = {}
    monkeypatch.setattr(wizard, "telegram", tg)
    monkeypatch.setattr(wizard, "wait_for_chat", lambda token: (42, "Sam"))
    monkeypatch.setattr(wizard, "check_model", lambda m: "")
    monkeypatch.setattr(wizard, "claude_path", lambda: "/bin/claude")
    monkeypatch.setattr(wizard.service, "install", lambda h, **kw: installed.update(kw) or ["daimon: somewhere"])
    ask = Scripted(str(home.root), "keep", "home", "ollama", "ollama_chat/test-model", "yes", "123:abc", [], True, False)
    monkeypatch.setattr(wizard, "ollama_models", lambda: ["ollama_chat/test-model"])
    assert make_wizard(ask).run() == 0 and not ask.answers
    text = home.secrets_file.read_text(encoding="utf-8")
    assert "TELEGRAM_TOKEN=123:abc" in text and "TELEGRAM_CHAT_ID=42" in text
    assert sent == ["getMe", "sendMessage"]
    assert installed == {"messages": False, "start_now": False, "agent": True}


def test_wizard_model_retry_then_keep(home, monkeypatch, clean_env):
    monkeypatch.setattr(wizard, "check_model", lambda m: "AuthenticationError: bad key")
    monkeypatch.setattr(wizard, "claude_path", lambda: None)
    ask = Scripted(str(home.root), "keep", "home", "anthropic", "", "skip", "anthropic/claude-haiku-5-5", "keep", "no", [])
    assert make_wizard(ask).run() == 0
    assert home.worker("main").model == "anthropic/claude-haiku-5-5"


def test_no_args_suggests_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["daimon", "--home", str(tmp_path / "nothing")])
    cli.main()
    assert "daimon setup" in capsys.readouterr().out


# ---- providers, keys, models, config ---------------------------------------------------------------------
def test_clean_and_mask_keys():
    assert providers.clean_key('  "OPENROUTER_API_KEY=sk-or-v1-abc"\n', "OPENROUTER_API_KEY") == "sk-or-v1-abc"
    assert providers.clean_key("Bearer sk-ant-x y") == "sk-ant-xy"
    assert providers.masked("sk-or-v1-0123456789abcdef") == "sk-or-…cdef" and providers.masked("short") == "*****"


def test_openrouter_models_and_bad_key(monkeypatch):
    def get(url, **kw):
        if url.endswith("/key"):
            ok = kw["headers"]["Authorization"] == "Bearer good"
            return httpx.Response(200 if ok else 401, json={} if ok else {"error": {"message": "User not found."}},
                                  request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url), json={"data": [
            {"id": "anthropic/claude-sonnet-5.5", "name": "Anthropic: Claude Sonnet 5.5", "context_length": 1000000,
             "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
            {"id": "anthropic/claude-sonnet-5.5:batch", "name": "batch"},
            {"id": "meta/llama-free", "name": "Llama (free)", "context_length": 128000,
             "pricing": {"prompt": "0", "completion": "0"}}]})
    monkeypatch.setattr(providers.httpx, "get", get)
    p = providers.PROVIDERS["openrouter"]
    with pytest.raises(KeyProblem, match="User not found"):
        providers.fetch_models(p, "bad")
    models = providers.fetch_models(p, "good")
    assert [m.id for m in models] == ["openrouter/anthropic/claude-sonnet-5.5", "openrouter/meta/llama-free"]
    assert models[0].price == (2, 10) and "1M context" in models[0].describe() and "free" in models[1].describe()
    assert [m.id for m in providers.search(models, "SONNET anthropic")] == ["openrouter/anthropic/claude-sonnet-5.5"]


def test_wizard_key_rejected_then_pasted_again_and_searched(home, monkeypatch, clean_env):
    tries = []

    def fetch(p, key):
        tries.append(key)
        if key != "sk-or-good":
            raise KeyProblem("the provider didn't accept this key (User not found.)")
        return [Model(f"openrouter/x/m{i}") for i in range(30)] + [Model("openrouter/z/special", "Special One")]
    monkeypatch.setattr(wizard, "fetch_models", fetch)
    monkeypatch.setattr(wizard, "check_model", lambda m: "")
    monkeypatch.setattr(wizard, "claude_path", lambda: None)
    ask = Scripted(str(home.root), "keep", "home", "openrouter", "sk-or-bad", "show", "sk-or-good",
                   "?search", "special", "openrouter/z/special", "no", [])
    w = make_wizard(ask)
    assert w.run() == 0 and not ask.answers
    assert tries == ["sk-or-bad", "sk-or-good"]
    assert home.worker("main").model == "openrouter/z/special"
    assert "OPENROUTER_API_KEY=sk-or-good" in home.secrets_file.read_text(encoding="utf-8")


def test_price_goes_into_models_yaml(home):
    wizard.use_model(home, "openrouter/x/priced", (2, 10))
    assert "price: {in: 2, out: 10}" in home.read("models.yaml")


def test_config_one_section_and_menu(home, monkeypatch, clean_env):
    monkeypatch.setattr(wizard, "fetch_models", lambda p, key: [Model("anthropic/claude-opus-5-5")])
    monkeypatch.setattr(wizard, "claude_path", lambda: None)
    monkeypatch.setattr(wizard.service, "status", lambda h: [])
    ask = Scripted("anthropic", "sk-ant-new-0123456789")
    assert make_wizard(ask).configure(home, "key") == 0 and not ask.answers
    assert "ANTHROPIC_API_KEY=sk-ant-new-0123456789" in home.secrets_file.read_text(encoding="utf-8")
    ask = Scripted("show", "done")
    w = make_wizard(ask)
    assert w.configure(home) == 0 and not ask.answers
    assert "ANTHROPIC_API_KEY (sk-ant…6789)" in w.out.file.getvalue()


def test_knowledge_folder_elsewhere(home, tmp_path, monkeypatch, clean_env):
    shared = tmp_path / "vault"
    (shared / "people").mkdir(parents=True)
    (shared / "people" / "sam.md").write_text("---\ntype: person\n---\nSam likes climbing.\n", encoding="utf-8")
    ask = Scripted("other", str(shared))
    w = make_wizard(ask)
    assert w.configure(home, "knowledge") == 0 and not ask.answers
    assert home.knowledge == shared.resolve() and "folder:" in home.read("config.yaml")
    assert "1 note there now" in w.out.file.getvalue() and "isn't a git repo" in w.out.file.getvalue()
    from daimon.knowledge import Knowledge
    kb = Knowledge(home)
    assert kb.where() == str(shared.resolve()) and kb.count() == 1
    assert kb.write("people/alex.md", {"type": "person"}, "Alex.", "met Alex").startswith("Saved knowledge/people/alex.md")
    assert (shared / "people" / "alex.md").exists() and not (home.root / "knowledge" / "people" / "alex.md").exists()
    ask = Scripted("home")
    make_wizard(ask).configure(home, "knowledge")
    assert home.knowledge == home.root / "knowledge"


def test_knowledge_folder_commits_in_its_own_repo(home, tmp_path):
    shared = tmp_path / "notes"
    shared.mkdir()
    import subprocess as sp
    sp.run(["git", "init", "-q"], cwd=shared, check=True)
    home.set_knowledge_folder(str(shared))
    from daimon.knowledge import Knowledge
    out = Knowledge(home).write("topics/x.md", {"type": "topic"}, "X.", "note x")
    assert "(change " in out
    log = sp.run(["git", "log", "--format=%s"], cwd=shared, capture_output=True, text=True).stdout
    assert "knowledge: note x" in log
