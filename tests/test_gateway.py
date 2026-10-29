"""Step 4 tests: the gateway, the Telegram channel against a fake Bot API, and the terminal as a channel."""
import asyncio

import pytest

from daimon import gateway as gw
from daimon.agent import Agent
from daimon.cli import Terminal
from daimon.gate import Tool
from daimon.gateway import Gateway, Message, Prompt, configured, decide
from daimon.telegram import TelegramChannel, TelegramError, chunks

from test_agent import home, script, until  # noqa: F401 - fixtures and helpers

CHAT = 42


class FakeBot:
    """Stands in for the Bot API: records calls, serves queued updates to getUpdates."""

    def __init__(self):
        self.calls, self.updates, self.next_id, self.msg_id = [], asyncio.Queue(), 1, 100
        self.fail = {}  # method -> list of errors to raise first

    async def __call__(self, method, **params):
        if self.fail.get(method):
            raise self.fail[method].pop(0)
        if method == "getUpdates":
            try:
                return [await asyncio.wait_for(self.updates.get(), 0.05)]
            except TimeoutError:
                return []
        self.calls.append((method, params))
        if method == "getMe":
            return {"username": "test_bot"}
        if method == "sendMessage":
            self.msg_id += 1
            return {"message_id": self.msg_id}
        return True

    def push(self, update):
        update["update_id"] = self.next_id
        self.next_id += 1
        self.updates.put_nowait(update)

    def text(self, text, chat=CHAT, reply_to=None):
        msg = {"chat": {"id": chat}, "from": {"id": chat}, "text": text}
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to}
        self.push({"message": msg})

    def tap(self, data, message_id, chat=CHAT):
        self.push({"callback_query": {"id": "cq", "data": data,
                                      "message": {"message_id": message_id, "chat": {"id": chat}}}})

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def channel(bot, chat=CHAT, **kw):
    return TelegramChannel("token", chat, api=bot, log=lambda _t: None, **kw)


def tool(name="send_email"):
    async def run(_args):
        return "sent"
    return Tool(name, "", {}, run, lambda _a: "ask")


def test_decide():
    assert decide("y", False).approved and decide("", False).approved
    assert decide("always", True).always and not decide("always", False).approved
    d = decide("not to Bob", True)
    assert not d.approved and d.note == "not to Bob"


def test_chunks():
    text = "\n".join(f"line {i}" for i in range(2000))
    parts = chunks(text)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    assert "\n".join(parts) == text
    assert chunks("short") == ["short"]


def test_telegram_approval_buttons():
    async def go():
        bot = FakeBot()
        ch = channel(bot)
        g = Gateway([ch])
        await g.start(None)
        try:
            ask = asyncio.create_task(g.approve(tool(), {"to": "mom@example.com"}, True, "main"))
            await until(lambda: ch.pending)
            prompt = bot.sent()[-1]
            assert "send_email" in prompt["text"] and "mom@example.com" in prompt["text"]
            buttons = prompt["reply_markup"]["inline_keyboard"][0]
            assert [b["text"] for b in buttons] == ["Yes", "No", "Always"]
            bot.tap(buttons[2]["callback_data"], 101)
            decision = await ask
            assert decision.approved and decision.always
            edit = bot.sent("editMessageText")[-1]
            assert edit["text"].endswith("→ Always") and edit["reply_markup"] == {"inline_keyboard": []}
            # A typed reply to the prompt declines with a note.
            ask = asyncio.create_task(g.approve(tool(), {}, False, "researcher"))
            await until(lambda: ch.pending)
            bot.text("use my work address", reply_to=bot.msg_id)
            decision = await ask
            assert not decision.approved and decision.note == "use my work address"
            # A tap on a stale prompt is told so.
            bot.tap("99:y", 5)
            await until(lambda: any(p.get("text") == "This request has expired." for p in bot.sent("answerCallbackQuery")))
        finally:
            await g.close()
    asyncio.run(go())


def test_first_answer_wins_across_channels():
    async def go():
        bot = FakeBot()
        tg, term = channel(bot), Terminal(interactive=True)
        g = Gateway([term, tg])
        await g.start(None)
        try:
            ask = asyncio.create_task(g.approve(tool(), {}, False, "main"))
            await until(lambda: term.pending and tg.pending)
            term.answer_oldest("n")
            assert not (await ask).approved
            await until(lambda: bot.sent("editMessageText"))
            assert bot.sent("editMessageText")[-1]["text"].endswith("answered elsewhere")
            # And the other way round: the terminal drops its queued prompt.
            ask = asyncio.create_task(g.approve(tool(), {}, False, "main"))
            await until(lambda: term.pending and tg.pending)
            bot.tap(f"{next(iter(tg.pending))}:y", bot.msg_id)
            assert (await ask).approved
            assert not term.pending
        finally:
            await g.close()
    asyncio.run(go())


def test_telegram_chat_with_agent(home, monkeypatch):  # noqa: F811
    script(monkeypatch, {"main": ["Hi! How can I help?"]})

    async def go():
        bot = FakeBot()
        ch = channel(bot)
        g = Gateway([ch])
        agent = Agent(home, g)
        await agent.start()
        await g.start(agent)
        try:
            bot.text("hello")
            await until(lambda: any(p["text"] == "Hi! How can I help?" for p in bot.sent()))
            assert bot.sent("sendChatAction")
            g.say("Your reminder: stretch.")  # background results reach every channel
            await until(lambda: any(p["text"] == "Your reminder: stretch." for p in bot.sent()))
        finally:
            await g.close()
            await agent.stop()
    asyncio.run(go())


def test_telegram_ignores_strangers_and_survives_errors():
    async def go():
        bot = FakeBot()
        got = []
        ch = channel(bot)
        bot.fail["getUpdates"] = [TelegramError("Conflict", 409), TelegramError("network down")]
        await ch.connect(lambda m: got.append(m) or asyncio.sleep(0))
        try:
            bot.text("hi", chat=7)
            bot.text("hi again", chat=7)
            bot.text("me")
            await until(lambda: got, timeout=10)
            assert [m.text for m in got] == ["me"]
            to_stranger = [p for p in bot.sent() if p["chat_id"] == 7]
            assert len(to_stranger) == 1 and "TELEGRAM_CHAT_ID=7" in to_stranger[0]["text"]
        finally:
            await ch.close()
    asyncio.run(go())


def test_telegram_without_chat_id_never_answers_prompts():
    async def go():
        bot = FakeBot()
        ch = channel(bot, chat=None)
        await ch.connect(lambda m: asyncio.sleep(0))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ch.prompt(Prompt("x", options=[("y", "Yes")])), 0.2)
            await ch.send("nobody hears this")
            assert not bot.sent()
        finally:
            await ch.close()
    asyncio.run(go())


def test_send_retries_rate_limits():
    async def go():
        bot = FakeBot()
        bot.fail["sendMessage"] = [TelegramError("Too Many Requests", 429, retry_after=0.01)]
        ch = channel(bot)
        await ch.send("hello")
        assert bot.sent()[-1]["text"] == "hello"
        bot.fail["sendMessage"] = [TelegramError("Bad Request: chat not found", 400)]
        with pytest.raises(TelegramError):
            await ch.send("hello")
    asyncio.run(go())


def test_configured_channels(home, monkeypatch):  # noqa: F811
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert configured(home) == []
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001")
    [tg] = configured(home)
    assert tg.chat_id == -1001 and tg.events == {"notify", "error"}
    home.write("config.yaml", home.read("config.yaml").replace("  telegram:\n", "  telegram:\n    enabled: false\n"))
    assert configured(home) == []
    asyncio.run(tg.close())


def test_failed_channel_is_dropped():
    class Broken:
        name = "broken"

        async def connect(self, on_message):
            raise TelegramError("Unauthorized", 401)

        async def close(self):
            pass

    async def go():
        g = Gateway([gw.Log(), Broken()])
        assert await g.start(None) == ["broken: Unauthorized"]
        assert [c.name for c in g.channels] == ["log"]
    asyncio.run(go())


def test_handle_replies_on_the_same_channel():
    class FakeAgent:
        async def send(self, text):
            return text.upper()

    async def go():
        sent = []
        term = Terminal(interactive=True)
        term.send = lambda text: asyncio.sleep(0, sent.append(text))
        g = Gateway([term])
        await g.start(FakeAgent())
        await term.on_message(Message("ping", "terminal"))
        assert sent == ["PING"]
    asyncio.run(go())


def test_one_running_agent_per_home(home):  # noqa: F811
    from daimon.cli import hold_lock
    lock = hold_lock(home)
    try:
        with pytest.raises(SystemExit):
            hold_lock(home)
    finally:
        lock.close()
    hold_lock(home).close()
