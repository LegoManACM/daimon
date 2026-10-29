"""The messaging MCP server (`daimon-messages mcp`, stdio): lookups over the stored messages, plus sending.

Lookups read messages.db directly, so they work whether or not the service is running. Sending and the link's
live status go through the running service's control API.

Results are short on purpose: capped snippets and counts, never a dump of the stream.
"""
from __future__ import annotations

import asyncio
from datetime import tzinfo

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import render
from .config import Home
from .control import ServiceDown, call
from .store import Store

READ = ToolAnnotations(read_only_hint=True)
SEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
MAX_LIMIT = 100

INSTRUCTIONS = """The user's text messages (SMS, MMS, RCS through Google Messages), stored on their computer.
Use these to look things up when the user's request needs it: who said what, when plans were made, someone's
number, what came in recently. Quote only what's needed. Chats can be named by person ("Sam"), chat name, or ID."""


def build(home: Home, store: Store | None = None) -> MCPServer:
    store = store or Store(home.db)
    tz: tzinfo = home.config().tz()
    server = MCPServer("messages", instructions=INSTRUCTIONS)

    def when(text: str) -> str:
        return render.parse_when(text, tz)

    def cap(limit: int) -> int:
        return max(1, min(int(limit or 1), MAX_LIMIT))

    def pick_chat(ref: str) -> tuple[dict | None, str]:
        chat, candidates = store.resolve_chat(ref)
        if chat:
            return chat, ""
        if not candidates:
            return None, f"No chat matches {ref!r}. Try list_chats or find_contact."
        lines = [f"- {store.chat_name(c['id'], c)} (id {c['id']}, last {render.local(c['last_ts'], tz)})"
                 for c in candidates]
        return None, f"{ref!r} matches more than one chat. Say which (by id):\n" + "\n".join(lines)

    def grouped(messages: list[dict], limit: int) -> str:
        """Messages grouped by chat, chats newest first, messages in order within each."""
        by_chat: dict[str, list[dict]] = {}
        for m in messages:
            by_chat.setdefault(m["chat"], []).append(m)
        out = []
        for cid, ms in by_chat.items():
            chat = store.chat(cid)
            group = " (group)" if chat and chat["is_group"] else ""
            out.append(f"## {store.chat_name(cid, chat)}{group}  [chat id {cid}]")
            out += [render.line(m, tz, limit, with_id=True) for m in sorted(ms, key=lambda m: (m["ts"], m["seq"]))]
        return "\n".join(out)

    @server.tool(annotations=READ)
    def search_messages(query: str = "", person: str = "", chat: str = "", since: str = "", until: str = "",
                        limit: int = 10) -> str:
        """Search the user's text messages, newest first.

        query: words to find (all must match), "an exact phrase", or a prefix* . Leave empty to filter only.
        person: a name or number; matches the sender or anyone in the chat.
        chat: a chat id, chat name or person, to search inside one chat.
        since / until: 2026-10-20, 2026-10-20 18:30, today, yesterday, 3d, 12h, 2w (local time).
        """
        try:
            since_iso, until_iso = when(since), when(until)
        except ValueError as e:
            return str(e)
        chat_id = ""
        if chat:
            found, err = pick_chat(chat)
            if not found:
                return err
            chat_id = found["id"]
        hits = store.search(query, person, chat_id, since_iso, until_iso, limit=cap(limit))
        if not hits:
            return "No messages found."
        more = " (the newest; there may be more)" if len(hits) >= cap(limit) else ""
        return f"{len(hits)} message{'s' if len(hits) > 1 else ''}{more}:\n" + grouped(hits, 300)

    @server.tool(annotations=READ)
    def recent_messages(since: str = "1d", incoming_only: bool = False, limit: int = 40) -> str:
        """What was texted lately, across all chats, grouped by chat. since: e.g. 2h, 1d, today, 2026-10-20."""
        try:
            since_iso = when(since or "1d")
        except ValueError as e:
            return str(e)
        hits = store.search(since=since_iso, from_me=False if incoming_only else None, limit=cap(limit))
        if not hits:
            return f"No messages since {render.local(since_iso, tz)}."
        more = " (limit reached; there may be more)" if len(hits) >= cap(limit) else ""
        return f"{len(hits)} message{'s' if len(hits) > 1 else ''} since {render.local(since_iso, tz)}{more}:\n" + \
            grouped(hits, 300)

    @server.tool(annotations=READ)
    def list_chats(query: str = "", since: str = "", limit: int = 20) -> str:
        """Conversations, most recent first, with their newest message. query filters by chat name, person or
        number; since keeps chats active since then (e.g. 7d)."""
        try:
            since_iso = when(since)
        except ValueError as e:
            return str(e)
        chats = store.chats(query, since_iso, cap(limit))
        if not chats:
            return "No chats found."
        lines = []
        for c in chats:
            people = ", ".join(p.get("name") or p.get("number") or "?" for p in c["participants"] if not p.get("me"))
            head = f"- {store.chat_name(c['id'], c)}" + (f" (group: {people})" if c["is_group"] else "")
            last = c["last"]
            tail = f"{render.who(last)}: {render.body(last, 120)}" if last else "no stored messages"
            lines.append(f"{head}  [id {c['id']}, {c['n']} msgs, last {render.local(c['last_ts'], tz)}]\n    {tail}")
        return "\n".join(lines)

    @server.tool(annotations=READ)
    def read_chat(chat: str, before: str = "", after: str = "", limit: int = 30) -> str:
        """Read one conversation in order. By default the latest messages; `after` reads forward from a time,
        `before` reads the messages leading up to one. chat: a chat id, chat name or person."""
        found, err = pick_chat(chat)
        if not found:
            return err
        try:
            before_iso, after_iso = when(before), when(after)
        except ValueError as e:
            return str(e)
        ms = store.history(found["id"], before_iso, after_iso, cap(limit))
        people = ", ".join(f"{p.get('name') or '?'} {p.get('number') or ''}".strip()
                           for p in found["participants"] if not p.get("me"))
        head = f"## {store.chat_name(found['id'], found)}  [chat id {found['id']}; with {people or 'unknown'}]"
        if not ms:
            return head + "\nNo messages in that range."
        return head + "\n" + "\n".join(render.line(m, tz, 1000, with_id=True) for m in ms)

    @server.tool(annotations=READ)
    def message_context(message_id: str, before: int = 6, after: int = 6) -> str:
        """The messages around one message (from a search result's id), to see what it was replying to."""
        ms = store.around(message_id, max(0, min(before, 50)), max(0, min(after, 50)))
        if not ms:
            return f"No message with id {message_id!r}."
        chat = store.chat(ms[0]["chat"])
        lines = [("→ " if m["id"] == message_id else "  ") + render.line(m, tz, 1000) for m in ms]
        return f"## {store.chat_name(ms[0]['chat'], chat)}  [chat id {ms[0]['chat']}]\n" + "\n".join(lines)

    @server.tool(annotations=READ)
    def find_contact(query: str) -> str:
        """Find a person by name or number: their numbers (from the phone's contacts and chats), the chats they're
        in, and when the user last texted with them."""
        if not query.strip():
            return "Give a name or number."
        people = store.people(query)
        if not people:
            return f"No one matches {query!r}."
        lines = []
        for p in people:
            nums = ", ".join(p["numbers"]) or "no number known"
            lines.append(f"- {p['name'] or '(no name)'}: {nums}; last texted {render.local(p['last_ts'], tz)}")
            lines += [f"    {'group' if c['group'] else 'chat'}: {c['name']} [id {c['id']}]" for c in p["chats"][:5]]
        return "\n".join(lines)

    @server.tool(annotations=READ)
    def messaging_status() -> str:
        """Whether the Google Messages link is connected, and how many messages are stored."""
        counts = store.counts()
        stored = (f"{counts['n']} messages in {counts['chats']} chats, "
                  f"{render.local(counts['first'], tz)} to {render.local(counts['last'], tz)}.")
        try:
            s = call(home, "GET", "/status", timeout=5)
        except ServiceDown as e:
            return f"{e}\nStored: {stored}"
        detail = f" ({s['detail']})" if s.get("detail") else ""
        backfill = " Catching up on history now." if s.get("backfill", {}).get("running") else ""
        return f"Link: {s['state']}{detail}.{backfill}\nStored: {stored}\n" \
               f"Since the service started: {s.get('received', 0)} new, {s.get('dropped', 0)} filtered out."

    if home.config().allow_send:
        @server.tool(annotations=SEND)
        async def send_message(text: str, chat: str = "", to: str = "") -> str:
            """Send a text message as the user. Give `chat` (a chat id, chat name or person with an existing chat)
            or `to` (a phone number, which starts a new chat if needed). The message goes out from the user's phone
            and can't be unsent."""
            if not text.strip():
                return "The message is empty."
            chat_id = ""
            if chat:
                found, err = pick_chat(chat)
                if not found:
                    return err
                chat_id = found["id"]
            elif not to.strip():
                return "Say who to send it to: `chat` or `to` (a phone number)."
            try:
                result = await asyncio.to_thread(call, home, "POST", "/send",
                                                 {"text": text, "chat": chat_id, "to": to.strip()})
            except ServiceDown as e:
                return f"Not sent. {e}"
            if not result.get("ok"):
                return f"Not sent: {result.get('error', 'unknown error')}"
            name = store.chat_name(result.get("chat", ""))
            return f"Sent to {name if name != 'a chat' else (to or chat)}."

    return server


def main(home: Home) -> None:
    build(home).run("stdio")
