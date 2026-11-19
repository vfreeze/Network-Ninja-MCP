from recap_mcp.store import Store
import pytest


def ev(i, status="needs_recap", start="2026-01-01"):
    return {"events_id": i, "venue_name": "V" + i, "start_ts": start, "site_status": status}


def test_events_roundtrip_and_pending_filter():
    s = Store(":memory:")
    s.upsert_events([ev("1", start="2026-01-01"), ev("2", "draft_saved", "2026-02-01"), ev("3", "submitted")])
    assert [e["events_id"] for e in s.events()] == ["2", "1"]
    assert len(s.events(only_pending=False)) == 3
    s.upsert_events([ev("1", "submitted")])
    assert s.event("1")["site_status"] == "submitted"
    assert s.event("1")["address"] is None
    assert s.event("nope") is None
    assert [e["events_id"] for e in s.events()] == ["2"]


def test_answers_overwrite_keeps_others():
    s = Store(":memory:")
    s.set_answers("1", {"a": "x", "b": "y"}, {"b"})
    s.set_answers("1", {"a": "z"}, set())
    assert s.answers("1") == {"a": "z", "b": "y"}
    assert s.answers("2") == {}


def test_photos_and_is_our_document():
    s = Store(":memory:")
    pid = s.add_photo("1", "front", "/p/a.jpg")
    s.add_photo("1", "back", "/p/b.jpg")
    assert len(s.photos("1")) == 2 and len(s.photos("1", "front")) == 1
    assert not s.is_our_document("99")
    s.mark_uploaded(pid, "99", "uuid-1")
    assert s.is_our_document("99") and s.is_our_document(99)
    row = s.delete_photo(pid)
    assert row["path"] == "/p/a.jpg"
    assert not s.is_our_document("99")
    with pytest.raises(KeyError):
        s.delete_photo(pid)


def test_links_expire():
    s = Store(":memory:")
    assert s.link_event(s.new_link("1")) == "1"
    assert s.link_event(s.new_link("1", minutes=-1)) is None
    assert s.link_event("bogus") is None


def test_draft_and_meta():
    s = Store(":memory:")
    assert s.draft("1") is None
    s.set_draft("1", "p1", "fp", None)
    s.set_draft("1", "p2", "fp2", "boom")
    d = s.draft("1")
    assert (d["partial_id"], d["fingerprint"], d["last_error"]) == ("p2", "fp2", "boom")
    assert s.meta_get("k") is None
    s.meta_set("k", "v")
    s.meta_set("k", "v2")
    assert s.meta_get("k") == "v2"
