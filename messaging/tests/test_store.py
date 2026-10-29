from datetime import datetime, timezone

from conftest import conversation, message
from daimon_messages import render
from daimon_messages.filters import blocked
from daimon_messages.store import Store, fts_query


def seeded() -> Store:
    s = Store(":memory:")
    c = conversation()
    s.upsert_chat(c["id"], c["name"], c["group"], c["participants"])
    g = conversation("g1", "Climbing crew", True, (("p2", "Sam Lee", "+15551234567"), ("p3", "Alex", "+15559876543")))
    s.upsert_chat(g["id"], g["name"], g["group"], g["participants"])
    s.add(message("m1", "Dinner Thursday after 7?", minutes_ago=60))
    s.add(message("m2", "Yes! The ramen place", minutes_ago=50, from_me=True))
    s.add(message("m3", "Gym tomorrow at 6", cid="g1", minutes_ago=10, name="Alex", number="+15559876543"))
    return s


def test_add_is_idempotent_and_updates_body():
    s = seeded()
    assert not s.add(message("m1", "Dinner Thursday after 8?", minutes_ago=60))
    assert s.get("m1")["body"] == "Dinner Thursday after 8?"
    assert s.counts()["n"] == 3


def test_search_fulltext_person_and_chat():
    s = seeded()
    assert [m["id"] for m in s.search("ramen")] == ["m2"]
    assert [m["id"] for m in s.search("dinn*")] == ["m1"]
    assert {m["id"] for m in s.search(person="alex")} == {"m3"}
    assert {m["id"] for m in s.search(person="5551234567")} == {"m1", "m2", "m3"}  # Sam is in the group too
    assert [m["id"] for m in s.search(chat="c1")] == ["m2", "m1"]
    assert [m["id"] for m in s.search(from_me=True)] == ["m2"]
    assert s.search('"ramen place" OR drop') == []  # operators are quoted, not run


def test_fts_query_quotes_everything():
    assert fts_query('dinner "the ramen" sush* NEAR(') == '"dinner" "the ramen" "sush"* "NEAR"'


def test_history_and_around():
    s = seeded()
    assert [m["id"] for m in s.history("c1")] == ["m1", "m2"]
    assert [m["id"] for m in s.history("c1", limit=1)] == ["m2"]
    assert [m["id"] for m in s.around("m2", 5, 5)] == ["m1", "m2"]


def test_resolve_chat_prefers_the_direct_chat():
    s = seeded()
    assert s.resolve_chat("c1")[0]["id"] == "c1"
    assert s.resolve_chat("Sam")[0]["id"] == "c1"
    assert s.resolve_chat("climbing crew")[0]["id"] == "g1"
    assert s.resolve_chat("nobody") == (None, [])


def test_people_merges_contacts_and_participants():
    s = seeded()
    s.set_contacts([{"id": "k1", "name": "Sam Lee", "number": "+1 555 123 4567"}])
    people = s.people("sam")
    assert len(people) == 1
    assert {c["id"] for c in people[0]["chats"]} == {"c1", "g1"}


def test_feed_and_ack():
    s = seeded()
    batch = s.feed("graph")
    assert [m["id"] for m in batch] == ["m1", "m2", "m3"]
    s.ack("graph", batch[1]["seq"])
    assert [m["id"] for m in s.feed("graph")] == ["m3"]
    assert len(s.feed("other")) == 3


def test_filters():
    chat = {"name": "", "is_group": False, "participants": [{"number": "+15551234567", "name": "Sam"}]}
    assert blocked({"text": "Your verification code is 482913"}, chat, []) == "login code"
    assert blocked({"text": "See you at 7", "number": "72975"}, None, []) == "short code"
    assert blocked({"text": "hi"}, chat, ["(555) 123-4567"]) == "denylist"
    assert blocked({"text": "hi"}, chat, ["sam"]) == "denylist"
    assert blocked({"text": "Dinner at 7?"}, chat, []) == ""


def test_parse_when():
    tz = timezone.utc
    now = datetime(2026, 10, 25, 15, 0, tzinfo=tz)
    assert render.parse_when("today", tz, now) == "2026-10-25T00:00:00+00:00"
    assert render.parse_when("yesterday", tz, now) == "2026-10-24T00:00:00+00:00"
    assert render.parse_when("3h", tz, now) == "2026-10-25T12:00:00+00:00"
    assert render.parse_when("2026-10-20 18:30", tz, now) == "2026-10-20T18:30:00+00:00"
    assert render.parse_when("", tz, now) == ""
