"""The messaging MCP server (`daimon-messages mcp`, stdio): lookups over the stored messages, plus sending.

Lookups read messages.db directly, so they work whether or not the service is running. Sending and the link's
live status go through the running service's control API.

Results are short on purpose: capped snippets and counts, never a dump of the stream.

Watchers (watch.py) are managed here too: the tools write watchers/*.md and watch.yaml, and the running service
picks the changes up on its next pass.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, tzinfo
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import render
from .config import Home
from .control import ServiceDown, call
from .store import Store, utcnow
from .watch import ACTIONS, MATCH_KEYS, PRESETS, Folder, Watcher, load_settings, save_settings

READ = ToolAnnotations(read_only_hint=True)
SEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
WATCH = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
MAX_LIMIT = 100

INSTRUCTIONS = """The user's text messages (SMS, MMS, RCS through Google Messages), stored on their computer.
Use these to look things up when the user's request needs it: who said what, when plans were made, someone's
number, what came in recently. Quote only what's needed. Chats can be named by person ("Sam"), chat name, or ID.

Watchers read new texts as they arrive and wake you when one matches what it's told to look for. Set one up
whenever the user asks to be told about something in their texts, and also on your own when you notice it would
help (someone said they'll text when they land, a plan is waiting on an answer, a delivery is due); say so when you
do. Use list_watchers, set_watcher, test_watcher, remove_watcher, watcher_hits, watch_settings."""


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

    # ---- watchers ----

    folder = Folder(home.watchers)

    def describe(w: Watcher, stats: dict, settings) -> str:
        m = ", ".join(f"{k}={v}" for k, v in w.match.items() if v not in (None, "", []))
        state = "on" if w.enabled else "off"
        if w.enabled and (problems := w.problems(settings)):
            state = "inactive: " + "; ".join(problems)
        s = stats.get(w.name)
        fired = f"fired {s['n']}x, last {render.local(s['last'], tz)}" if s else "never fired"
        how = "model reads texts" + (f" ({w.model})" if w.model else "") if w.judge else "fires on the match alone"
        lines = [f"- {w.name} [{state}] {w.description}",
                 f"    match: {m or 'every text'}; {how}; then {w.action}" + (" (once)" if w.once else "")
                 + (f", expires {w.expires}" if w.expires else "") + f"; {fired}; by {w.created_by}"]
        if w.then:
            lines.append(f"    then: {w.then}")
        return "\n".join(lines)

    @server.tool(annotations=READ)
    def list_watchers() -> str:
        """The text watchers: what each matches, whether it's on, how often it fired, and the model reading texts."""
        settings = load_settings(home.watch_settings)
        watchers = folder.all()
        head = (f"Model reading texts: {settings.model or 'none set (watchers that need judgment are inactive)'}"
                + (f"; also allowed: {', '.join(settings.models)}" if settings.models else "")
                + f". A chat is read once it's been quiet {settings.quiet_seconds}s.")
        if not watchers and not folder.errors:
            return head + "\nNo watchers yet. Make one with set_watcher."
        stats = store.hit_stats()
        body = [describe(w, stats, settings) for w in watchers.values()]
        body += [f"- {n} [unreadable: {e}]" for n, e in folder.errors.items()]
        return head + "\n" + "\n".join(body)

    @server.tool(annotations=READ)
    def get_watcher(name: str) -> str:
        """One watcher's whole file: its settings and what it looks for."""
        path = home.watchers / f"{name}.md"
        return path.read_text(encoding="utf-8") if path.exists() else f"No watcher named {name!r}."

    @server.tool(annotations=WATCH)
    def set_watcher(name: str, look_for: str | None = None, description: str | None = None,
                    chats: list[str] | None = None, not_chats: list[str] | None = None,
                    senders: list[str] | None = None, any_words: list[str] | None = None,
                    all_words: list[str] | None = None, regex: str | None = None, preset: str | None = None,
                    incoming_only: bool | None = None, groups: str | None = None, judge: bool | None = None,
                    fields: list[str] | dict[str, str] | None = None, action: str | None = None, then: str | None = None,
                    once: bool | None = None, expires: str | None = None, cooldown_minutes: int | None = None,
                    context: int | None = None, model: str | None = None, enabled: bool | None = None,
                    why: str | None = None) -> str:
        """Create a text watcher, or change one (only the arguments given change). New texts that pass the match
        are read by a model once their chat goes quiet; if they're what `look_for` describes, the watcher fires:
        it wakes you (action=wake) with what it saw and your `then` note, hands it to the curator (inbox), or only
        keeps it (log; see watcher_hits). A watcher never replies or acts by itself.

        Use it when the user asks ("tell me if Mom texts about Sunday", "watch for my package") and on your own
        when it would clearly help (someone will text when they land; a plan waits on a reply). Set `why`, tell
        the user what you set up, and try it with test_watcher.

        name: short id, lowercase-with-dashes (e.g. sam-flight).
        look_for: plain words for the model: what counts and what doesn't.
        Match (everything given must pass; keep it loose, the model does the judging):
          chats / not_chats: people, chat names or chat ids. senders: who wrote it ("me" = the user).
          any_words / all_words: words, prefix* allowed. regex: a regular expression.
          preset: planning (days, times, let's, I'll...), changes (cancel, reschedule, late), question.
          incoming_only: default true (the user's own texts are skipped). groups: include | exclude | only.
        judge: false fires on the match alone, at once (for exact things, like a word from one person).
        fields: things to pull out when it fires: a list (["time", "place"]) or, when something will use them
          without reading (your reactions), each with its format: {"start": "ISO 8601 local datetime", ...}.
          A hit also arrives as an event (messages.watch_hit) with these fields in its data.
        then: what you should do when it fires (shown to you then). once: switch off after one hit.
        expires: a date or date-time after which it stops. cooldown_minutes: quiet time per chat after firing.
        context: how many earlier texts the model sees (default 8). model: one allowed in watch_settings.
        enabled: false keeps it but switches it off.
        """
        existing = folder.get(name)
        w = existing or Watcher(name=name, created=utcnow(), created_by="agent")
        given: dict[str, Any] = {
            "look_for": look_for, "description": description, "judge": judge, "fields": fields, "action": action,
            "then": then, "once": once, "expires": expires, "cooldown": cooldown_minutes, "context": context,
            "model": model, "enabled": enabled, "why": why}
        for k, v in given.items():
            if v is not None:
                setattr(w, k, v)
        match = {"chats": chats, "not_chats": not_chats, "from": senders, "any": any_words, "all": all_words,
                 "regex": regex, "preset": preset, "incoming_only": incoming_only, "groups": groups}
        w.match = {k: v for k, v in (w.match | {k: v for k, v in match.items() if v is not None}).items()
                   if k in MATCH_KEYS and v not in (None, "", [])}
        if preset and preset not in PRESETS:
            return f"Unknown preset {preset!r}: use {', '.join(PRESETS)}."
        if w.action not in ACTIONS:
            return f"action must be one of {', '.join(ACTIONS)}."
        if groups and groups not in ("include", "exclude", "only"):
            return "groups must be include, exclude or only."
        if w.expires:
            try:
                datetime.fromisoformat(w.expires)
            except ValueError:
                return "expires must be a date (2026-11-05) or a date and time (2026-11-05 18:00)."
        try:
            folder.save(w)
        except ValueError as e:
            return str(e)
        settings = load_settings(home.watch_settings)
        problems = w.problems(settings)
        note = ("\nNot active yet: " + "; ".join(problems)) if w.enabled and problems else ""
        return ("Changed" if existing else "Made") + " watcher:\n" + describe(w, store.hit_stats(), settings) + note

    @server.tool(annotations=WATCH)
    def remove_watcher(name: str) -> str:
        """Delete a watcher (its past hits are kept). To pause one instead: set_watcher(name, enabled=False)."""
        return f"Removed {name}." if folder.remove(name) else f"No watcher named {name!r}."

    @server.tool(annotations=READ)
    async def test_watcher(name: str, since: str = "7d", chat: str = "") -> str:
        """Try a watcher on texts already stored, delivering nothing: how many pass its match, and for the newest
        few conversations, whether the model calls them a hit. Use it after set_watcher to tune the match and
        look_for. Needs the messaging service running."""
        try:
            since_iso = when(since or "7d")
        except ValueError as e:
            return str(e)
        try:
            r = await asyncio.to_thread(call, home, "POST", "/watch/test",
                                        {"name": name, "since": since_iso, "chat": chat}, 600)
        except ServiceDown as e:
            return str(e)
        if r.get("error"):
            return r["error"]
        out = [f"{r['scanned']} texts since {render.local(since_iso, tz)}; {r['matched']} passed the match, in "
               f"{r['groups']} conversation(s)."]
        if r.get("problems"):
            out.append("Problems: " + "; ".join(r["problems"]))
        for res in r["results"]:
            if "error" in res:
                verdict = f"error: {res['error']}"
            elif res.get("match"):
                verdict = "WOULD FIRE" + (f": {res['summary']}" if res.get("summary") else "")
                if res.get("fields"):
                    verdict += " " + json.dumps(res["fields"], ensure_ascii=False)
            else:
                verdict = "no"
            out.append(f"## {res['chat']}: {verdict}\n" + "\n".join(f"    {t}" for t in res["texts"]))
        return "\n".join(out)

    @server.tool(annotations=READ)
    def watcher_hits(name: str = "", since: str = "7d", limit: int = 20) -> str:
        """What watchers fired on lately, newest first (all of them, or one)."""
        try:
            since_iso = when(since or "7d")
        except ValueError as e:
            return str(e)
        hits = store.hits(name, since_iso, cap(limit))
        if not hits:
            return "No hits in that time."
        lines = []
        for h in hits:
            found = "; ".join(f"{k}: {v}" for k, v in h["fields"].items())
            failed = h["delivered"].startswith("failed")
            lines.append(f"- {render.local(h['ts'], tz)} {h['watcher']} in {store.chat_name(h['chat'])} "
                         f"[chat id {h['chat']}]: {h['summary'] or '(matched)'}" + (f" [{found}]" if found else "")
                         + (f" (didn't reach the agent: {h['delivered'][8:]})" if failed else "")
                         + f"\n    messages: {', '.join(h['messages'][-5:])}")
        return "\n".join(lines)

    @server.tool(annotations=WATCH)
    def watch_settings(model: str | None = None, models: list[str] | None = None, params: dict | None = None,
                       quiet_seconds: int | None = None, max_checks_per_hour: int | None = None,
                       agent_inbox: str | None = None) -> str:
        """Show or change how watchers read texts (only the arguments given change; none just shows them).

        model: the LiteLLM model that reads texts for watchers. A local one keeps texts on this computer
          (ollama_chat/qwen3.5:9b, lm_studio/...); a cloud one sends them to that provider
          (anthropic/claude-haiku-5-5, openai/gpt-5-mini, openrouter/..., gemini/...). "" switches judging off.
        models: other models individual watchers may pick. params: provider options (api_base, ...; ${NAME}
          reads the secrets file). quiet_seconds: how long a chat must be quiet before it's read.
        max_checks_per_hour: cap on model calls. agent_inbox: where hits go (default the agent's inbox folder).
        """
        s = load_settings(home.watch_settings)
        changes = {"model": model, "models": models, "params": params, "quiet_seconds": quiet_seconds,
                   "max_checks_per_hour": max_checks_per_hour, "agent_inbox": agent_inbox}
        changed = [k for k, v in changes.items() if v is not None]
        for k in changed:
            setattr(s, k, changes[k])
        if changed or not home.watch_settings.exists():
            save_settings(home.watch_settings, s)
        shown = {k: v for k, v in vars(s).items() if k != "secrets"}
        shown["params"] = {k: "***" if any(t in k.lower() for t in ("key", "token", "secret")) else v
                           for k, v in s.params.items()}
        return ("Changed " + ", ".join(changed) + ". " if changed else "") + \
            "Watch settings: " + json.dumps(shown, ensure_ascii=False)

    return server


def main(home: Home) -> None:
    build(home).run("stdio")
