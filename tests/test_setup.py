"""`daimon setup` and `daimon service`, with no TTY, network, model calls or real service managers."""
import io
import json
import os
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
from daimon.web.access import AccessError


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
                                   "consent": {"send_message": "ask", "set_watcher": "notify",
                                               "remove_watcher": "notify", "watch_settings": "ask"}}


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
    agent, messages, canvas = service.jobs(home, python="py")
    assert agent.argv == ["py", "-m", "daimon", "--home", str(home.root), "run"]
    assert messages.argv == ["py", "-m", "daimon_messages", "run"]
    assert (messages.title, messages.label) == ("Daimon Messages", "com.daimon.messages")
    assert canvas.argv == ["py", "-m", "daimon_canvas", "watch", "--every", "30"]
    assert canvas.log == home.cache / "daimon-canvas.log"
    assert [j.key for j in service.jobs(home, messages=False, canvas=False)] == ["daimon"]
    assert [j.key for j in service.jobs(home, agent=False, canvas=False)] == ["daimon-messages"]


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
    assert service.stop(home) == ["daimon: stopped", "daimon-messages: stopped", "daimon-canvas: stopped"]
    assert ["taskkill", "/PID", "123", "/T", "/F"] in sh.calls


def test_restart_restarts_installed_jobs(home, tmp_path, monkeypatch, sh):
    monkeypatch.setattr(service, "platform", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    service.install(home, messages=False)
    assert service.restart(home) == ["daimon: restarted"]
    assert ["systemctl", "--user", "restart", "daimon.service"] in sh.calls
    monkeypatch.setattr(service, "platform", lambda: "macos")
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    service.install(home, messages=False)
    sh.results["launchctl kickstart"] = (1, "", "Operation not permitted")
    assert service.restart(home) == ["daimon: couldn't restart: Operation not permitted"]
    assert ["launchctl", "kickstart", "-k", "gui/501/com.daimon.agent"] in sh.calls


def test_restart_later_runs_service_restart_outside(home, monkeypatch):
    detached = []
    monkeypatch.setattr(service, "detach", lambda where, name, text: detached.append((where, name, text)))
    log = service.restart_later(home, after_s=5)
    where, name, text = detached[0]
    assert (where, name, log) == (home.cache, "restart", home.cache / "restart.log")
    assert f'"--home" "{home.root}" "service" "restart" >> "{log}"' in text
    assert ("ping -n 6" if sys.platform == "win32" else "sleep 5") in text


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
        # Anything secret must go through secret(), which shows * as it's typed.
        assert not any(w in message.lower() for w in ("key", "token", "secret", "password")), message
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
    ask = Scripted(str(tmp_path / "home"), "home", "anthropic", "  'sk-ant-test'\n", "anthropic/claude-opus-5-5", "no", [], False)
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
    ask = Scripted(str(home.root), "keep", "home", "ollama", "ollama_chat/test-model", "yes", "123:abc", [], True, False, False)
    monkeypatch.setattr(wizard, "ollama_models", lambda: ["ollama_chat/test-model"])
    assert make_wizard(ask).run() == 0 and not ask.answers
    text = home.secrets_file.read_text(encoding="utf-8")
    assert "TELEGRAM_TOKEN=123:abc" in text and "TELEGRAM_CHAT_ID=42" in text
    assert sent == ["getMe", "sendMessage"]
    assert installed == {"messages": False, "start_now": False, "agent": True, "canvas": False}


def test_wizard_model_retry_then_keep(home, monkeypatch, clean_env):
    monkeypatch.setattr(wizard, "check_model", lambda m: "AuthenticationError: bad key")
    monkeypatch.setattr(wizard, "claude_path", lambda: None)
    ask = Scripted(str(home.root), "keep", "home", "anthropic", "", "skip", "anthropic/claude-haiku-5-5", "keep", "no", [], False)
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
    ask = Scripted(str(home.root), "keep", "home", "openrouter", "sk-or-bad", "again", "sk-or-good",
                   "?search", "special", "openrouter/z/special", "no", [], False)
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


def test_config_one_addon_leaves_the_others_alone(home, monkeypatch, clean_env):
    ran = []
    monkeypatch.setattr(wizard.addons, "installed", lambda a: True)
    monkeypatch.setattr(wizard.addons, "run_interactive", lambda argv: ran.append(argv[2:]) or 0)
    data = home.mcp()
    data["mcpServers"].update({"canvas": {"command": "x"}, "messages": {"command": "x"}, "calendar": {"command": "x"}})
    home.write_mcp(data)
    ask = Scripted("calendar", "google", "done")
    w = make_wizard(ask)
    assert w.configure(home, "addons") == 0 and not ask.answers
    assert ran == [["daimon_calendar", "add", "google"]]  # no Canvas login, no phone pairing


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


# ---- uninstall ------------------------------------------------------------------------------------------
def test_uninstall_asks_and_keeps_outside_knowledge(home, tmp_path, monkeypatch, capsys):
    from daimon.setup import uninstall
    monkeypatch.setenv("DAIMON_CANVAS_HOME", str(tmp_path / "canvas"))
    monkeypatch.setenv("DAIMON_MESSAGES_HOME", str(tmp_path / "messages"))
    (tmp_path / "messages").mkdir()
    home.set_secret("ANTHROPIC_API_KEY", "sk-ant-x")
    vault = tmp_path / "vault"
    (vault / "people").mkdir(parents=True)
    (vault / "people" / "sam.md").write_text("---\ntype: person\n---\nSam.\n", encoding="utf-8")
    home.set_knowledge_folder(str(vault))
    monkeypatch.setattr(uninstall, "tool_install", lambda: False)
    monkeypatch.setattr(uninstall, "stop_running", lambda: None)
    monkeypatch.setattr(uninstall.service, "installed", lambda h: [])
    monkeypatch.setattr(uninstall.service, "uninstall", lambda h: [])

    assert uninstall.run(str(home.root), ask=lambda prompt: "n") == 1
    assert home.exists() and "Nothing was removed" in capsys.readouterr().out

    assert uninstall.run(str(home.root), ask=lambda prompt: "y") == 0
    out = capsys.readouterr().out
    assert not home.root.exists() and not home.secrets_file.exists() and not (tmp_path / "messages").exists()
    assert (vault / "people" / "sam.md").exists() and f"Kept: your knowledge folder, {vault}" in out
    assert "Device pairing" in out and "Daimon is uninstalled" in out


def test_uninstall_counts_notes_inside_the_home(home, monkeypatch, capsys):
    from daimon.setup import uninstall
    (home.root / "knowledge" / "people").mkdir(parents=True, exist_ok=True)
    (home.root / "knowledge" / "people" / "sam.md").write_text("---\ntype: person\n---\nSam.\n", encoding="utf-8")
    monkeypatch.setattr(uninstall, "tool_install", lambda: False)
    monkeypatch.setattr(uninstall.service, "installed", lambda h: [])
    uninstall.run(str(home.root), ask=lambda prompt: "")
    assert "including 1 knowledge notes" in capsys.readouterr().out and home.exists()


# ---- updates ---------------------------------------------------------------------------------------------
def test_versions_compare():
    from daimon.setup import update
    assert update.newer("0.1.10", "0.1.9") and update.newer("0.2.0", "0.1.1") and not update.newer("0.1.1", "0.1.1")
    assert update.parse("v1.2.3-rc1") == (1, 2, 3)


def test_available_checks_daily_and_announces_once(home, monkeypatch):
    from daimon.setup import update
    calls = []
    monkeypatch.setattr(update, "latest", lambda: calls.append(1) or "9.9.9")
    assert update.available(home) == "9.9.9" and update.available(home) == "9.9.9" and len(calls) == 1
    assert update.available(home, force=True) == "9.9.9" and len(calls) == 2
    assert update.announce_once(home, "9.9.9") and not update.announce_once(home, "9.9.9")
    monkeypatch.setattr(update, "latest", lambda: "0.0.1")
    assert update.available(home, force=True) is None


def test_set_option_keeps_comments(home):
    home.set_option("updates", "auto", True)
    text = home.read("config.yaml")
    assert "  auto: true" in text and "# install new releases by itself" in text
    assert home.config()["updates"] == {"check": True, "auto": True}
    home.set_option("brand_new", "x", 3)
    assert home.config()["brand_new"] == {"x": 3}


def test_update_script_keeps_addons_and_restarts(home, tmp_path, monkeypatch):
    from daimon.setup import update
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda m: m == "daimon_canvas")
    text = update.script(home, "0.2.0", 4242, True, tmp_path / "tool", tmp_path / "update.log")
    assert "releases/download/v0.2.0/daimon-0.2.0-py3-none-any.whl" in text
    assert "--with" in text and "daimon_canvas-0.2.0" in text and "daimon_messages" not in text
    assert "4242" in text and "service stop" in text and "service start" in text
    assert text.index("service stop") < text.index("tool") < text.rindex("service start")
    quiet = update.script(None, "0.2.0", None, False, tmp_path / "tool", tmp_path / "update.log")
    assert "service" not in quiet


@pytest.mark.skipif(os.name != "nt", reason="the pip fallback is for Windows file locks")
def test_update_script_falls_back_to_pip(home, tmp_path, monkeypatch):
    import tomllib
    from daimon.setup import update
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda m: m == "daimon_canvas")
    record = tmp_path / "uv-receipt.toml"
    text = update.script(home, "0.2.0", None, True, tmp_path / "tool", tmp_path / "update.log", record)
    assert text.index("tool\" \"install") < text.index(":pip") < text.index("pip install") < text.index(":done")
    assert "daimon_canvas-0.2.0" in text[text.index("pip install"):] and str(record) in text
    assert text.index(":failed") < text.rindex("service start")  # a failed install still starts what's there
    got = tomllib.loads(update.receipt("0.2.0", r"C:\bin\daimon.exe"))["tool"]
    assert [r["name"] for r in got["requirements"]] == ["daimon", "daimon-canvas"]
    assert got["entrypoints"][0]["install-path"] == r"C:\bin\daimon.exe"
    monkeypatch.setenv("DAIMON_RELEASE_DIR", r"C:\Users\me\release")  # a local build's paths have backslashes
    assert tomllib.loads(update.receipt("0.2.0", "x"))["tool"]["requirements"][0]["url"].startswith("C:\\Users")


def test_update_command(home, monkeypatch, capsys):
    from daimon.setup import update
    monkeypatch.setattr(update, "latest", lambda: update.current())
    monkeypatch.setattr(sys, "argv", ["daimon", "--home", str(home.root), "update"])
    cli.main()
    assert "is the latest version" in capsys.readouterr().out
    monkeypatch.setattr(update, "latest", lambda: "99.0.0")
    monkeypatch.setattr(sys, "argv", ["daimon", "--home", str(home.root), "update", "--check"])
    cli.main()
    assert "Daimon 99.0.0 is out" in capsys.readouterr().out
    launched = []
    monkeypatch.setattr(update, "launch", lambda h, v, wait_pid: launched.append(v) or home.cache / "update.log")
    monkeypatch.setattr(update, "running_jobs", lambda h: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(sys, "argv", ["daimon", "--home", str(home.root), "update"])
    cli.main()
    assert launched == ["99.0.0"] and "Updating to 99.0.0" in capsys.readouterr().out


def test_config_updates(home, monkeypatch, clean_env):
    monkeypatch.setattr(wizard.service, "status", lambda h: [])
    ask = Scripted("auto")
    assert make_wizard(ask).configure(home, "updates") == 0 and not ask.answers
    assert wizard.update.settings(home) == (True, True)
    make_wizard(Scripted("off")).configure(home, "updates")
    assert wizard.update.settings(home) == (False, False)


# ---- daimon web --------------------------------------------------------------------------------------------
def web_args(home, link=False):
    import argparse
    return argparse.Namespace(home=str(home.root), link=link)


def test_web_page_address_and_sign_in_link(home, monkeypatch):
    monkeypatch.setenv("WEB_TOKEN", "tok")
    assert cli.web_page(home) == ("http://localhost:8770", "http://localhost:8770/?token=tok", ("127.0.0.1", 8770))
    home.set_option("web", "auth", "none")
    assert cli.web_page(home)[1] == "http://localhost:8770"
    home.set_option("web", "enabled", False)
    with pytest.raises(AccessError, match="off"):
        cli.web_page(home)


def test_daimon_web_says_when_nothing_serves_it(home, monkeypatch):
    monkeypatch.setenv("WEB_TOKEN", "tok")
    monkeypatch.setattr(cli, "web_page", lambda h: ("http://localhost:1", "http://localhost:1/?token=tok", ("127.0.0.1", 1)))
    with pytest.raises(SystemExit, match="isn't running"):
        cli.cmd_web(web_args(home))


def test_daimon_web_opens_signed_in_or_prints_the_link(home, monkeypatch, capsys):
    import socket
    import webbrowser
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    url = f"http://localhost:{port}"
    monkeypatch.setattr(cli, "web_page", lambda h: (url, f"{url}/?token=tok", ("127.0.0.1", port)))
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    try:
        cli.cmd_web(web_args(home))
        cli.cmd_web(web_args(home, link=True))
    finally:
        server.close()
    assert opened == [f"{url}/?token=tok"]
    out = capsys.readouterr().out
    assert f"Opened {url}\n" in out and f"{url}/?token=tok" in out.split("Opened")[1]


def test_daimon_web_waits_for_a_starting_agent(home, monkeypatch, capsys):
    import socket
    import webbrowser
    tries = []

    class Conn:
        def close(self):
            pass

    def connect(probe, timeout):
        tries.append(probe)
        if len(tries) < 3:  # the agent holds its lock but isn't serving yet
            raise OSError("refused")
        return Conn()
    monkeypatch.setattr(cli, "web_page", lambda h: ("http://localhost:8770", "http://localhost:8770", ("127.0.0.1", 8770)))
    monkeypatch.setattr(cli, "agent_running", lambda h: True)
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(webbrowser, "open", lambda u: True)
    cli.cmd_web(web_args(home))
    assert len(tries) == 3 and "waiting for the page" in capsys.readouterr().out

    # running without the page: in `daimon chat` it says so, as the background service it restarts and serves
    up = []
    monkeypatch.setattr(socket, "create_connection", lambda probe, timeout: Conn() if up else (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(cli, "WEB_START_WAIT_S", -1)
    monkeypatch.setattr(service, "running", lambda h: False)
    with pytest.raises(SystemExit, match="daimon chat"):
        cli.cmd_web(web_args(home))
    monkeypatch.setattr(service, "running", lambda h: True)
    monkeypatch.setattr(service, "restart", lambda h: up.append(1) or ["daimon: restarted"])
    cli.cmd_web(web_args(home))
    assert up == [1] and "restarting it" in capsys.readouterr().out
    up.clear()
    monkeypatch.setattr(service, "restart", lambda h: ["daimon: restarted"])
    with pytest.raises(SystemExit, match="after a restart"):
        cli.cmd_web(web_args(home))


# ---- the fast track --------------------------------------------------------------------------------------
@pytest.fixture
def fast(tmp_path, monkeypatch, clean_env):
    """A fast-track run with everything outside the files stubbed; returns what it did."""
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    monkeypatch.delenv("WEB_TOKEN", raising=False)
    from daimon.web import access
    seen = {"installed": [], "opened": [], "probed": []}
    monkeypatch.setattr(wizard, "check_model", lambda m: "")
    monkeypatch.setattr(wizard, "fetch_models", lambda p, key: [Model("anthropic/claude-opus-5-5")])
    monkeypatch.setattr(access, "tailscale_cli", lambda: None)
    monkeypatch.setattr(wizard.service, "install",
                        lambda h, **kw: seen["installed"].append(kw) or ["daimon: task; started"])
    monkeypatch.setattr(wizard, "wait_for_page", lambda probe, seconds=0: seen["probed"].append(probe) or True)
    monkeypatch.setattr(wizard.webbrowser, "open", lambda u: seen["opened"].append(u) or True)
    seen["root"] = tmp_path / "home"
    return seen


def fast_run(root, *answers):
    ask = Scripted(str(root), "anthropic", "  'sk-ant-test'\n", "anthropic/claude-opus-5-5", *answers)
    w = make_wizard(ask)
    assert w.run(full=False) == 0 and not ask.answers
    return w, ask


def test_fast_track_asks_three_things_and_writes_them(fast):
    w, ask = fast_run(fast["root"], "localhost")
    assert ask.asked[0] == "Where should the agent live?" and ask.asked[-1] == "Where will you open it?"
    assert not any("Telegram" in q or "knowledge" in q.lower() for q in ask.asked)
    h = Home(fast["root"])
    assert h.worker("main").model == "anthropic/claude-opus-5-5"
    assert "ANTHROPIC_API_KEY=sk-ant-test" in h.secrets_file.read_text(encoding="utf-8")
    assert "WEB_TOKEN=" in h.secrets_file.read_text(encoding="utf-8")
    web = h.config()["web"]
    assert web["enabled"] and web["listen"] == "localhost" and web["auth"] == "token"
    out = w.out.file.getvalue()
    assert "1/3" in out and "2/3" in out and "3/3" in out and "/8" not in out and "/7" not in out
    assert "Finish setup in your browser" in out and "daimon setup --full" in out


def test_fast_track_starts_the_agent_with_only_the_web_page_on(fast):
    fast_run(fast["root"], "localhost")
    assert fast["installed"] == [{"messages": False, "start_now": True, "agent": True}]
    assert len(fast["opened"]) == 1 and fast["opened"][0].endswith("#setup") and "?token=" in fast["opened"][0]
    assert fast["opened"][0].count("#") == 1
    assert fast["probed"] and fast["probed"][0][0] == "127.0.0.1"


def test_fast_track_offers_tailscale_first_when_it_is_up(fast, monkeypatch):
    from daimon.web import access
    monkeypatch.setattr(access, "tailscale_cli", lambda: "/bin/tailscale")
    monkeypatch.setattr(access, "resolve_listen",
                        lambda listen: "100.64.0.1" if listen == "tailscale" else "127.0.0.1")
    seen = []
    real = Scripted

    class Spy(real):
        def select(self, message, choices, default=None):
            if message == "Where will you open it?":
                seen.append(([t for t, _ in choices], default))
            return super().select(message, choices, default)
    ask = Spy(str(fast["root"]), "anthropic", "  'sk-ant-test'\n", "anthropic/claude-opus-5-5", "tailscale")
    assert make_wizard(ask).run(full=False) == 0
    titles, default = seen[0]
    assert "(detected)" in titles[0] and default == "tailscale" and len(titles) == 3
    assert Home(fast["root"]).config()["web"]["auth"] == "tailscale"


def test_fast_track_without_tailscale_lists_two_choices(fast):
    seen = []

    class Spy(Scripted):
        def select(self, message, choices, default=None):
            if message == "Where will you open it?":
                seen.append([v for _, v in choices])
            return super().select(message, choices, default)
    ask = Spy(str(fast["root"]), "anthropic", "  'sk-ant-test'\n", "anthropic/claude-opus-5-5", "all")
    w = make_wizard(ask)
    assert w.run(full=False) == 0
    assert seen == [["localhost", "all"]]
    assert Home(fast["root"]).config()["web"]["listen"] == "all"


@pytest.mark.parametrize("how", ["raises", "not started"])
def test_fast_track_says_when_it_cannot_run_in_the_background(fast, monkeypatch, how):
    def install(h, **kw):
        if how == "raises":
            raise OSError("no scheduler")
        return ["daimon: task; not started: access denied"]
    monkeypatch.setattr(wizard.service, "install", install)
    w, _ = fast_run(fast["root"], "localhost")
    out = w.out.file.getvalue()
    assert "daimon run" in out and "Couldn't start it in the background" in out
    assert not fast["opened"] and not fast["probed"]


def test_setup_dispatch_fast_and_full(monkeypatch):
    calls = []
    monkeypatch.setattr(wizard, "run", lambda home, full=False: calls.append((home, full)) or 0)
    for argv, want in ((["setup"], False), (["setup", "--full"], True)):
        monkeypatch.setattr(sys, "argv", ["daimon", *argv])
        with pytest.raises(SystemExit):
            cli.main()
        assert calls[-1][1] is want


def test_wizard_run_full_flag_picks_the_steps(home, monkeypatch, clean_env):
    w = make_wizard(Scripted())
    order = []
    for name in ("welcome", "knowledge", "model", "telegram", "claude", "addons", "background", "finish", "launch"):
        monkeypatch.setattr(w, name, lambda n=name: order.append(n))
    monkeypatch.setattr(w, "web", lambda first_run=False: order.append("web"))
    w.run(full=False)
    assert order == ["welcome", "model", "web", "launch"]
    order.clear()
    w.run(full=True)
    assert order == ["welcome", "knowledge", "model", "telegram", "claude", "addons", "background", "finish"]


def test_background_allows_the_agent_with_web_on_and_no_telegram(home, monkeypatch, clean_env):
    home.set_option("web", "enabled", True)
    installed = {}
    monkeypatch.setattr(wizard.service, "install", lambda h, **kw: installed.update(kw) or ["daimon: somewhere"])
    ask = Scripted(True, True, False)  # keep running, start now, no auto-updates
    w = make_wizard(ask)
    w.home = home
    w.background()
    assert not ask.answers and installed["agent"] is True and installed["start_now"] is True
    assert "needs Telegram or the web page" not in w.out.file.getvalue()


def test_background_without_telegram_or_web_still_warns(home, monkeypatch, clean_env):
    home.set_option("web", "enabled", False)
    w = make_wizard(Scripted())
    w.home = home
    w.background()
    assert "needs Telegram or the web page" in w.out.file.getvalue()
