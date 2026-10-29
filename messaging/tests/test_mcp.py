import asyncio

from mcp import Client

from conftest import conversation, message
from daimon_messages.mcp_server import build
from daimon_messages.store import Store
from test_service import service


def text(result) -> str:
    return "\n".join(c.text for c in result.content)


def seed(store: Store) -> None:
    for c in (conversation(), conversation("g1", "Climbing crew", True,
                                           (("p2", "Sam Lee", "+15551234567"), ("p3", "Alex", "+15559876543")))):
        store.upsert_chat(c["id"], c["name"], c["group"], c["participants"])
    store.add(message("m1", "Dinner Thursday after 7?", minutes_ago=60))
    store.add(message("m2", "Yes! The ramen place", minutes_ago=50, from_me=True))
    store.add(message("m3", "Gym tomorrow at 6", cid="g1", minutes_ago=10, name="Alex", number="+15559876543"))


def test_lookup_tools(home):
    async def go():
        store = Store(home.db)
        seed(store)
        async with Client(build(home, store)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {"search_messages", "recent_messages", "list_chats", "read_chat", "message_context",
                    "find_contact", "messaging_status", "send_message"} <= set(tools)
            assert tools["search_messages"].annotations.read_only_hint
            assert not tools["send_message"].annotations.read_only_hint

            out = text(await client.call_tool("search_messages", {"query": "ramen"}))
            assert "Me: Yes! The ramen place" in out and "Sam Lee" in out

            out = text(await client.call_tool("read_chat", {"chat": "Sam"}))
            assert out.index("Dinner Thursday") < out.index("ramen place")

            out = text(await client.call_tool("recent_messages", {"since": "2h", "incoming_only": True}))
            assert "Gym tomorrow" in out and "ramen" not in out

            out = text(await client.call_tool("list_chats", {}))
            assert out.index("Climbing crew") < out.index("Sam Lee")  # newest first

            out = text(await client.call_tool("message_context", {"message_id": "m2"}))
            assert "→ " in out and "Dinner Thursday" in out

            out = text(await client.call_tool("find_contact", {"query": "alex"}))
            assert "+15559876543" in out and "Climbing crew" in out

            out = text(await client.call_tool("search_messages", {"since": "next tuesday"}))
            assert "Couldn't read the time" in out

            out = text(await client.call_tool("messaging_status", {}))
            assert "isn't running" in out and "3 messages in 2 chats" in out

            out = text(await client.call_tool("send_message", {"text": "hi", "chat": "Sam"}))
            assert out.startswith("Not sent.")

    asyncio.run(go())


def test_send_through_the_service(home):
    async def go():
        svc = service(home)
        seed(svc.store)
        await svc.start()
        try:
            async with Client(build(home, svc.store)) as client:
                out = text(await client.call_tool("send_message", {"text": "On my way", "chat": "Sam"}))
                assert out == "Sent to Sam Lee."
                assert svc.sidecar.sent[-1] == {"cmd": "send", "conv": "c1", "to": "", "text": "On my way"}
                out = text(await client.call_tool("messaging_status", {}))
                assert out.startswith("Link: connected")
        finally:
            await svc.stop()

    asyncio.run(go())


def test_no_send_tool_when_switched_off(home):
    home.config_path.write_text(home.config_path.read_text().replace("allow_send: true", "allow_send: false"))

    async def go():
        async with Client(build(home)) as client:
            assert "send_message" not in {t.name for t in (await client.list_tools()).tools}

    asyncio.run(go())
