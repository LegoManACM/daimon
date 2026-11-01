"""The gateway: channels (the terminal, Telegram, ...) behind one small contract.

A channel can `send` text, show `event`s it cares about, render a `Prompt` (buttons, cards, or "reply y or n")
and hand inbound `Message`s to the gateway. The gateway is the agent's Surface: background results go to every
channel, a reply goes back where the message came from, and an approval is asked everywhere at once; the first
answer wins and the other channels drop their copy.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from .gate import Decision, Tool
from .home import expand_secrets


@dataclass
class Message:
    text: str
    channel: str
    sender: str = ""


@dataclass
class Prompt:
    title: str
    details: dict[str, str] = field(default_factory=dict)
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    who: str = "main"
    free_text: bool = True  # a typed reply is also an answer


OnMessage = Callable[[Message], Awaitable[None]]


class Channel(Protocol):
    name: str

    async def connect(self, on_message: OnMessage) -> None: ...
    async def close(self) -> None: ...
    async def send(self, text: str) -> None: ...
    def event(self, kind: str, text: str) -> None: ...  # progress and notices; each channel picks what it shows
    async def prompt(self, prompt: Prompt) -> str: ...  # an option's value or the typed reply; cancelled if answered elsewhere


def approval(tool: Tool, args: dict[str, Any], can_always: bool, who: str) -> Prompt:
    details = {}
    for k, v in args.items():
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        details[k] = text if len(text) < 600 else text[:600] + "…"
    options = [("y", "Yes"), ("n", "No")] + ([("a", "Always")] if can_always else [])
    return Prompt(tool.name, details, options, who)


def decide(answer: str, can_always: bool) -> Decision:
    low = answer.strip().lower()
    if low in ("y", "yes", ""):
        return Decision(True)
    if can_always and low in ("a", "always"):
        return Decision(True, always=True)
    if low in ("n", "no"):
        return Decision(False)
    return Decision(False, note=answer.strip())


class Gateway:
    """Implements gate.Surface over any number of channels."""

    def __init__(self, channels: list[Channel]):
        self.channels = channels
        self.agent = None
        self._sends: set[asyncio.Task] = set()

    async def start(self, agent) -> list[str]:
        """Connect every channel. One that fails is dropped; returns what went wrong for each."""
        self.agent, failed = agent, []
        for c in list(self.channels):
            if attach := getattr(c, "attach", None):  # channels that show more than the conversation (the web page)
                attach(agent)
            try:
                await c.connect(lambda msg, c=c: self.handle(msg, c))
            except Exception as e:  # noqa: BLE001
                self.channels.remove(c)
                await c.close()
                failed.append(f"{c.name}: {e}")
        return failed

    async def close(self) -> None:
        for c in self.channels:
            await c.close()
        await asyncio.gather(*self._sends, return_exceptions=True)

    async def handle(self, msg: Message, channel: Channel) -> None:
        """A message from the user on `channel`; the reply goes back there."""
        from .runner import ModelError, provider_hint  # loaded by the agent by now; at the top it'd slow every `daimon` command
        try:
            reply = await self.agent.send(msg.text)
        except ModelError as e:
            hint = provider_hint(self.agent.home.worker("main").model)
            reply = f"Model call failed: {e}" + (f"\n{hint}" if hint else "")
        await channel.send(reply)

    # ---- Surface -------------------------------------------------------------------------------------
    async def approve(self, tool: Tool, args: dict[str, Any], can_always: bool, who: str) -> Decision:
        return decide(await self.ask(approval(tool, args, can_always, who)), can_always)

    async def ask(self, prompt: Prompt) -> str:
        asks = [asyncio.create_task(c.prompt(prompt)) for c in self.channels]
        try:
            while True:
                done, pending = await asyncio.wait(asks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if not t.cancelled() and t.exception() is None:
                        return t.result()
                    if not t.cancelled():
                        self.event("error", f"prompt failed: {t.exception()}")
                if not pending:
                    raise RuntimeError("no channel could ask the user")
                asks = list(pending)
        finally:
            for t in asks:
                t.cancel()
            await asyncio.gather(*asks, return_exceptions=True)

    def event(self, kind: str, text: str) -> None:
        for c in self.channels:
            c.event(kind, text)

    def progress(self, who: str, state: dict[str, Any]) -> None:
        """Live state of a model call or tool (see progress.py), for channels that show it. Others never hear it."""
        for c in self.channels:
            if show := getattr(c, "progress", None):
                show(who, state)

    def say(self, text: str) -> None:
        for c in self.channels:
            t = asyncio.create_task(c.send(text))
            self._sends.add(t)
            t.add_done_callback(self._sends.discard)


class Log:
    """The headless `daimon run` console: prints what happens, never answers prompts."""

    name = "log"

    async def connect(self, on_message: OnMessage) -> None:
        pass

    async def close(self) -> None:
        pass

    async def send(self, text: str) -> None:
        self.print(f"agent> {text}")

    def event(self, kind: str, text: str) -> None:
        self.print(f"[{kind}] {text}")

    async def prompt(self, prompt: Prompt) -> str:
        self.print(f"[approval] {prompt.title} ({prompt.who}) is waiting for your answer")
        return await asyncio.Future()

    @staticmethod
    def print(text: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {text}", flush=True)


def configured(home) -> list[Channel]:
    """The channels set up in config.yaml: Telegram (`channels:`, on once TELEGRAM_TOKEN is set) and the web page
    (`web:`, on with enabled: true)."""
    cfg = home.config().get("channels") or {}
    out: list[Channel] = []
    tg = expand_secrets({"token": "${TELEGRAM_TOKEN}", "chat_id": "${TELEGRAM_CHAT_ID}", **(cfg.get("telegram") or {})})
    if tg.get("enabled", True) and tg.get("token"):
        from .telegram import TelegramChannel
        chat = str(tg.get("chat_id") or "").strip()
        out.append(TelegramChannel(tg["token"], int(chat) if chat.lstrip("-").isdigit() else None, tg.get("events")))
    from .web.channel import from_config
    if web := from_config(home):
        out.append(web)
    return out
