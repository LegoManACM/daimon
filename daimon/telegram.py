"""Telegram channel: chat with the agent through a bot, approvals as inline buttons.

Long-polls the Bot API (no webhook, so it works behind NAT). Only the configured chat is served; anyone else
who messages the bot is told their chat id once and otherwise ignored, which is also how you find yours.
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .gateway import Message, OnMessage, Prompt

LIMIT = 4000  # Telegram's cap is 4096 characters per message
ICONS = {"notify": "•", "error": "⚠", "routine": "⟳", "task": "▸"}

Api = Callable[..., Awaitable[Any]]


class TelegramError(Exception):
    def __init__(self, text: str, code: int | None = None, retry_after: float | None = None):
        super().__init__(text)
        self.code, self.retry_after = code, retry_after


class BotApi:
    """`await api("sendMessage", chat_id=..., text=...)` returns the call's result or raises TelegramError.
    Errors never carry the URL, since it contains the token."""

    def __init__(self, token: str):
        self.token = token
        self.client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=70)

    async def __call__(self, method: str, **params: Any) -> Any:
        try:
            r = await self.client.post(method, json=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"{type(e).__name__}: {str(e).replace(self.token, '<token>')}") from None
        if not data.get("ok"):
            raise TelegramError(data.get("description", "request failed"), data.get("error_code"),
                                (data.get("parameters") or {}).get("retry_after"))
        return data["result"]

    async def close(self) -> None:
        await self.client.aclose()


@dataclass
class Pending:
    prompt: Prompt
    message_id: int
    answer: asyncio.Future


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text] if text.strip() or not out else out


def render(p: Prompt, hint: bool = True) -> str:
    lines = [f"Approve? {p.title}  ({p.who})"]
    for k, v in p.details.items():
        lines.append(f"{k}: {v}")
    if hint and p.free_text:
        lines.append("\nTap a button, or reply with a message to decline and say why.")
    return "\n".join(lines)


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_id: int | None, events: list[str] | None = None, api: Api | None = None,
                 log: Callable[[str], None] = print):
        self.chat_id = chat_id
        self.events = set(events if events is not None else ("notify", "error"))
        self.api = api or BotApi(token)
        self.log = log
        self.on_message: OnMessage | None = None
        self.pending: dict[str, Pending] = {}
        self._ids = itertools.count(1)
        self._told: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._poller: asyncio.Task | None = None

    # ---- Channel -------------------------------------------------------------------------------------
    async def connect(self, on_message: OnMessage) -> None:
        self.on_message = on_message
        try:
            me = await self.api("getMe")  # fails fast on a bad token
        except TelegramError as e:
            raise TelegramError(f"{e} (check TELEGRAM_TOKEN)" if e.code in (401, 404) else str(e), e.code) from None
        self.log(f"Telegram: connected as @{me.get('username')}"
                 + ("" if self.chat_id else ". No TELEGRAM_CHAT_ID yet: message the bot to get yours."))
        self._poller = asyncio.create_task(self._poll())

    async def close(self) -> None:
        for t in [self._poller, *self._tasks]:
            if t:
                t.cancel()
        await asyncio.gather(*[t for t in [self._poller, *self._tasks] if t], return_exceptions=True)
        if isinstance(self.api, BotApi):
            await self.api.close()

    async def send(self, text: str) -> None:
        if not self.chat_id or not text.strip():
            return
        for part in chunks(text):
            await self._call("sendMessage", chat_id=self.chat_id, text=part)

    def event(self, kind: str, text: str) -> None:
        if kind in self.events:
            self._spawn(self.send(f"{ICONS.get(kind, '·')} {text}"))

    async def prompt(self, prompt: Prompt) -> str:
        if not self.chat_id:
            await asyncio.Future()  # nobody to ask here; another channel answers
        pid = str(next(self._ids))
        buttons = [{"text": label, "callback_data": f"{pid}:{value}"} for value, label in prompt.options]
        msg = await self._call("sendMessage", chat_id=self.chat_id, text=render(prompt)[:LIMIT],
                               reply_markup={"inline_keyboard": [buttons]})
        p = self.pending[pid] = Pending(prompt, msg["message_id"], asyncio.get_running_loop().create_future())
        try:
            answer = await p.answer
            label = dict(prompt.options).get(answer, f'Replied: "{answer}"')
            await self._quiet(self._close_prompt(p, f"→ {label}"))
            return answer
        except asyncio.CancelledError:
            await self._quiet(self._close_prompt(p, "→ answered elsewhere"))
            raise
        finally:
            self.pending.pop(pid, None)

    # ---- inbound -------------------------------------------------------------------------------------
    async def _poll(self) -> None:
        offset, backoff = None, 1.0
        while True:
            try:
                updates = await self.api("getUpdates", offset=offset, timeout=50,
                                         allowed_updates=["message", "callback_query"])
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - keep polling through outages
                conflict = isinstance(e, TelegramError) and e.code == 409
                self.log(f"Telegram: {e}" + (" (is another `daimon run` or `daimon chat` using this bot?)" if conflict else ""))
                await asyncio.sleep(getattr(e, "retry_after", None) or backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    await self._update(u)
                except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                    self.log(f"Telegram: couldn't handle an update: {e}")

    async def _update(self, u: dict[str, Any]) -> None:
        if cq := u.get("callback_query"):
            await self._button(cq)
            return
        msg = u.get("message")
        if not msg:
            return
        chat = msg["chat"]["id"]
        if chat != self.chat_id:
            if chat not in self._told:
                self._told.add(chat)
                self.log(f"Telegram: message from chat {chat}, which isn't TELEGRAM_CHAT_ID; ignored.")
                await self._call("sendMessage", chat_id=chat, text=(
                    f"This agent doesn't talk to this chat. If it's your agent, put TELEGRAM_CHAT_ID={chat} in its "
                    f"secrets file and restart it."))
            return
        text = msg.get("text", "").strip()
        if not text:
            await self.send("I can only read text messages for now.")
            return
        if text == "/start":
            await self.send("Connected. Write to me here like you would in `daimon chat`.")
            return
        if self.pending:  # like the terminal: a typed line answers an open approval (the one replied to, or the oldest)
            replied = (msg.get("reply_to_message") or {}).get("message_id")
            p = next((p for p in self.pending.values() if p.message_id == replied), next(iter(self.pending.values())))
            if not p.answer.done():
                p.answer.set_result(text)
                return
        self._spawn(self._converse(Message(text, self.name, sender=str((msg.get("from") or {}).get("id", "")))))

    async def _button(self, cq: dict[str, Any]) -> None:
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        pid, _, value = cq.get("data", "").partition(":")
        p = self.pending.get(pid) if chat == self.chat_id else None
        await self._quiet(self.api("answerCallbackQuery", callback_query_id=cq["id"],
                                   text="" if p else "This request has expired."))
        if p and not p.answer.done():
            p.answer.set_result(value)
        elif not p and chat == self.chat_id and cq.get("message"):
            await self._quiet(self.api("editMessageReplyMarkup", chat_id=chat,
                                       message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []}))

    async def _converse(self, msg: Message) -> None:
        typing = asyncio.create_task(self._typing())
        try:
            await self.on_message(msg)
        finally:
            typing.cancel()

    async def _typing(self) -> None:
        while True:  # the indicator lasts ~5s, so keep it up while the agent works
            try:
                await asyncio.wait_for(self.api("sendChatAction", chat_id=self.chat_id, action="typing"), 10)
            except Exception:  # noqa: BLE001 - purely cosmetic
                pass
            await asyncio.sleep(4.5)

    # ---- helpers -------------------------------------------------------------------------------------
    async def _call(self, method: str, **params: Any) -> Any:
        """A call that matters (a message, a prompt): retried through rate limits and network blips."""
        for attempt in range(5):
            try:
                return await self.api(method, **params)
            except TelegramError as e:
                if e.code is not None and e.code != 429 or attempt == 4:
                    raise
                await asyncio.sleep(e.retry_after or 2 ** attempt)

    async def _close_prompt(self, p: Pending, outcome: str) -> None:
        await self.api("editMessageText", chat_id=self.chat_id, message_id=p.message_id,
                       text=f"{render(p.prompt, hint=False)[:LIMIT - 100]}\n\n{outcome}",
                       reply_markup={"inline_keyboard": []})

    async def _quiet(self, coro: Awaitable[Any]) -> None:
        try:
            await asyncio.wait_for(coro, 10)
        except Exception as e:  # noqa: BLE001 - cosmetic calls (edits, typing) never break the flow
            self.log(f"Telegram: {e}")

    def _spawn(self, coro: Awaitable[Any]) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
