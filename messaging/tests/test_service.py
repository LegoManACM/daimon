import asyncio

from conftest import conversation, message
from daimon_messages.control import call
from daimon_messages.service import Service


class FakeSidecar:
    """Stands in for the Go process: records commands, answers send/contacts."""

    def __init__(self):
        self.state = "connected"
        self.detail = self.qr = self.emoji = self.pair_error = ""
        self.sent: list[dict] = []
        self.proc = None

    def built(self):
        return True

    def since_iso(self):
        return ""

    def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, command):
        self.sent.append(command)
        return True

    async def request(self, command, timeout=60):
        self.sent.append(command)
        if command["cmd"] == "send":
            return {"type": "sent", "ok": True, "conv": command["conv"] or "new1"}
        return {"type": "contacts", "contacts": [{"id": "k1", "name": "Sam Lee", "number": "+15551234567"}]}


def service(home) -> Service:
    s = Service(home)
    s.sidecar = FakeSidecar()
    return s


def test_ingest_filters_and_backfills(home):
    async def go():
        s = service(home)
        await s.on_event(conversation())
        await s.on_event({"type": "paired"})
        await s.on_event(message("m1", "Dinner Thursday?"))
        await s.on_event(message("m2", "Your login code is 123456"))
        await s.on_event(message("m3", "ancient", minutes_ago=60 * 24 * 30))  # before the pairing window
        await s.on_event(message("m1", "Dinner Thursday?", status="INCOMING_DISPLAYED"))
        await s.on_event({"type": "status", "state": "connected"})
        await asyncio.sleep(0)  # let the contacts refresh run
        assert s.store.counts()["n"] == 1
        assert (s.received, s.dropped) == (1, 2)
        assert s.sidecar.sent[0]["cmd"] == "backfill"
        assert s.store.people("sam")[0]["numbers"] == ["+15551234567"]
        await s.on_event(message("m1", "", deleted=True))
        assert s.store.counts()["n"] == 0

    asyncio.run(go())


def test_control_api(home):
    async def go():
        s = service(home)
        await s.start()
        try:
            status = await asyncio.to_thread(call, home, "GET", "/status")
            assert status["state"] == "connected" and status["running"]
            sent = await asyncio.to_thread(call, home, "POST", "/send", {"text": "hi", "to": "+15551234567"})
            assert sent == {"ok": True, "chat": "new1"}
            assert s.sidecar.sent[-1] == {"cmd": "send", "conv": "", "to": "+15551234567", "text": "hi"}
            info = home.control.read_text()
            home.control.write_text(info.replace('"token": "', '"token": "x'))
            assert (await asyncio.to_thread(call, home, "GET", "/status"))["error"] == "unauthorized"
        finally:
            await s.stop()
        assert not home.control.exists()

    asyncio.run(go())


def test_send_refused_when_switched_off(home):
    async def go():
        s = service(home)
        s.cfg.allow_send = False
        assert not (await s.send_text("hi", to="+1555"))["ok"]

    asyncio.run(go())
