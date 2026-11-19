import base64
import json
import re
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from recap_mcp import site as site_mod
from recap_mcp.site import SiteClient, SiteError
from recap_mcp.store import Store


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


# ---- site client ----
def make_jwt(exp, uid=42):
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg({'exp': exp, 'staffing_users_id': uid})}.sig"


@pytest.fixture
def kr(monkeypatch):
    d = {("recap-mcp", "jwt"): make_jwt(time.time() + 20 * 86400)}
    monkeypatch.setattr(site_mod.keyring, "get_password", lambda s, k: d.get((s, k)))
    monkeypatch.setattr(site_mod.keyring, "set_password", lambda s, k, v: d.__setitem__((s, k), v))
    monkeypatch.setattr(site_mod.time, "sleep", lambda s: None)
    return d


def mk(handler):
    calls = []

    def h(req):
        calls.append(req)
        return handler(req, len(calls))
    return SiteClient(Store(":memory:"), httpx.Client(transport=httpx.MockTransport(h))), calls


def test_no_submit_or_claim_endpoints_in_source():
    src = "".join(p.read_text(encoding="utf-8") for p in Path("recap_mcp").glob("*.py"))
    assert re.search(r"['\"/]recap_response_set['\"]", src) is None
    assert re.search(r"['\"/][a-z_/]*claim", src) is None


def test_uid_and_get_sends_bearer(kr):
    c, calls = mk(lambda r, n: httpx.Response(200, json=[{"id": 1}]))
    assert c.uid() == "42"
    assert c.submitted() == [{"id": 1}]
    assert calls[0].headers["authorization"].startswith("Bearer ")
    assert "recap_response_set/staffing_user/42?shifts_days_back=90" in str(calls[0].url)


def test_remove_refuses_foreign_ids(kr):
    c, calls = mk(lambda r, n: httpx.Response(200, json={}))
    pid = c.store.add_photo("1", "gallery", "x.jpg")
    c.store.mark_uploaded(pid, "5", None)
    with pytest.raises(SiteError):
        c.remove_gallery_photos(["5", "999"])
    assert calls == []
    c.remove_gallery_photos(["5"])
    assert json.loads(calls[0].content) == {"ids": ["5"]}


def test_upload_retries_503_not_500(kr):
    c, calls = mk(lambda r, n: httpx.Response(503) if n == 1 else httpx.Response(200, json={"id": 7, "uuid": "u"}))
    assert c.upload_recap_photo("1", "9", b"jpg") == ("7", "u")
    assert len(calls) == 2
    c, calls = mk(lambda r, n: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(SiteError, match="500"):
        c.upload_gallery_photo("1", b"jpg")
    assert len(calls) == 1


def test_error_body_on_200_is_failure(kr):
    c, _ = mk(lambda r, n: httpx.Response(200, json={"error": "Nope"}))
    with pytest.raises(SiteError, match="Nope"):
        c.save_draft({})


def test_401_triggers_one_relogin(kr, monkeypatch):
    logins = []
    monkeypatch.setattr(site_mod, "login", lambda: logins.append(1) or make_jwt(time.time() + 9e6))
    c, calls = mk(lambda r, n: httpx.Response(401) if n == 1 else httpx.Response(200, json=[]))
    assert c.drafts() == []
    assert len(logins) == 1
    c, _ = mk(lambda r, n: httpx.Response(401))
    with pytest.raises(SiteError):
        c.drafts()


def test_definition_cached_and_app_version(kr):
    c, calls = mk(lambda r, n: httpx.Response(200, json={"id": 1}))
    c.definition("1"); c.definition("1")
    assert len(calls) == 1
    c, _ = mk(lambda r, n: httpx.Response(200, json={"version": "6.0"}))
    assert c.app_version() == "6.0"
    c, _ = mk(lambda r, n: httpx.Response(200, json=[1]))
    assert c.app_version() == "5.61.0"


def test_read_timeout_not_retried(kr):
    def boom(r, n):
        raise httpx.ReadTimeout("slow")
    c, calls = mk(boom)
    with pytest.raises(SiteError):
        c.upload_gallery_photo("1", b"jpg")
    assert len(calls) == 1


def test_connect_error_retried(kr):
    def h(r, n):
        if n == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"id": 3})
    c, calls = mk(h)
    assert c.upload_gallery_photo("1", b"jpg") == "3"
    assert len(calls) == 2


def test_login_failure_hides_password(kr, monkeypatch):
    kr[("recap-mcp", "username")] = "bob"
    kr[("recap-mcp", "password")] = "s3cretPW"

    class Page:
        def __getattr__(self, n):
            def f(*a, **k):
                if n == "fill" and a[0].startswith("input[type"):
                    raise RuntimeError('fill("s3cretPW") timed out')
            return f

        def expect_response(self, *a, **k):
            import contextlib
            return contextlib.nullcontext(type("G", (), {"value": None})())

    class B:
        def new_page(self): return Page()
        def close(self): pass

    class PW:
        chromium = type("C", (), {"launch": staticmethod(lambda **k: B())})()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(site_mod, "sync_playwright", lambda: PW())
    with pytest.raises(SiteError) as ei:
        site_mod.login()
    e = ei.value
    assert "s3cretPW" not in str(e)
    assert e.__cause__ is None and e.__suppress_context__  # `from None`
