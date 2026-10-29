import time

import pytest

from daimon_messages.config import Home


def ms(minutes_ago: float = 0) -> int:
    return int((time.time() - minutes_ago * 60) * 1000)


def conversation(cid="c1", name="", group=False, people=(("p2", "Sam Lee", "+15551234567"),)):
    return {"type": "conversation", "id": cid, "name": name, "group": group, "kind": "RCS",
            "participants": [{"id": "p1", "name": "Me", "number": "+15550000000", "me": True}]
            + [{"id": pid, "name": n, "number": num} for pid, n, num in people]}


def message(mid, text, cid="c1", minutes_ago=0, from_me=False, name="Sam Lee", number="+15551234567", **extra):
    return {"type": "message", "id": mid, "conv": cid, "sender": "p1" if from_me else "p2",
            "name": "" if from_me else name, "number": "" if from_me else number, "from_me": from_me,
            "ts_ms": ms(minutes_ago), "text": text, "media": [], "status": "INCOMING_COMPLETE"} | extra


@pytest.fixture
def home(tmp_path):
    h = Home(tmp_path / "msgs")
    h.config_path.write_text(h.config_path.read_text(encoding="utf-8").replace("control_port: 8771",
                                                                                "control_port: 0"),
                             encoding="utf-8")
    return h
