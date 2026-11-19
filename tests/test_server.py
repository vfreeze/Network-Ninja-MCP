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


# ---------------- MCP tools (Task 7) ----------------
import hashlib  # noqa: E402
from datetime import datetime  # noqa: E402

from recap_mcp import server as srv  # noqa: E402
from recap_mcp.recap import PHOENIX, answers_from_draft, find, parse_definition  # noqa: E402

DEF1 = json.load(open("tests/fixtures/def1.json", encoding="utf-8"))
F1 = parse_definition(DEF1)
ATT = find(F1, "Number of people in attendance").instance_id
ENG = find(F1, "Number of Consumers Engaged").instance_id


class FakeSite:
    def __init__(self):
        self.calls = []
        self.pos, self.evs, self.subs, self.drs = [], [], [], []
        self.fail_save = False

    def uid(self): return "7"
    def positions(self): return self.pos
    def events(self): return self.evs
    def submitted(self): return self.subs
    def drafts(self): return self.drs
    def definition(self, rid): return DEF1
    def app_version(self): return "5.61.0"

    def upload_recap_photo(self, events_id, field_id, jpeg):
        self.calls.append(("upload_recap_photo", field_id))
        return f"d{len(self.calls)}", f"u{len(self.calls)}"

    def upload_gallery_photo(self, events_id, jpeg):
        self.calls.append(("upload_gallery_photo", events_id))
        return f"g{len(self.calls)}"

    def save_draft(self, payload):
        self.calls.append(("save_draft", payload))
        if self.fail_save:
            self.fail_save = False
            raise SiteError("boom")
        return {"id": "p1"}


def mkt(tmp_path=None, shifts=(("1", "Red Zone", "2026-11-10 18:00:00"),)):
    store, fs = Store(":memory:"), FakeSite()
    for i, name, start in shifts:
        fs.pos.append({"events_id": i, "recap_on_portal": "t"})
        fs.evs.append({"id": i, "venue_name": name, "start_timestamp": start, "end_timestamp": start,
                       "events_recaps_id": "1", "street_address1": "1 Main", "city": "Mesa",
                       "scheduling_fields": [{"name": "Brand Family", "print_value": "Bud Light"}]})
    srv.build_server(store, fs, "https://x.test")
    return store, fs


def test_find_recaps_lists_pending_and_disambiguates():
    store, fs = mkt(shifts=(("1", "Red Zone", "2026-11-10 18:00:00"), ("2", "Red Zone", "2026-11-12 20:00:00")))
    r = srv.find_recaps(bar_name="red zone")
    assert {x["events_id"] for x in r["shifts"]} == {"1", "2"}
    assert r["shifts"][0]["date"] == "2026-11-12" and r["shifts"][0]["start_time"] == "20:00"
    assert "ask_user" in r
    fs.subs.append({"events_id": "1"})
    store.meta_set("last_refresh", "0")
    assert [x["events_id"] for x in srv.find_recaps()["shifts"]] == ["2"]


def test_fill_reports_rejected_and_missing():
    mkt(); srv.find_recaps()
    r = srv.fill_recap_fields("1", {ATT: "forty", ENG: "12"}, estimated=[ENG])
    assert r["saved"] == [ENG] and ATT in r["rejected"]
    assert ENG not in r["still_missing"]["answers"] and ATT in r["still_missing"]["answers"]
    assert srv.fill_recap_fields("9", {})["error"].startswith("Unknown shift id")


def test_save_draft_is_idempotent_for_photos(tmp_path):
    store, fs = mkt(); srv.find_recaps()
    srv.fill_recap_fields("1", {ATT: "40"})
    f = tmp_path / "a.jpg"; f.write_bytes(b"jpg")
    store.add_photo("1", "setup", str(f)); store.add_photo("1", "crowd", str(f))
    fs.fail_save = True
    assert "error" in srv.save_recap_draft("1")
    n = [c[0] for c in fs.calls].count("upload_recap_photo") + [c[0] for c in fs.calls].count("upload_gallery_photo")
    r = srv.save_recap_draft("1")
    assert r["saved"] is True and "Staff App" in r["message"]
    n2 = [c[0] for c in fs.calls if c[0].startswith("upload")]
    assert len(n2) == n == 2
    payload = [c[1] for c in fs.calls if c[0] == "save_draft"][-1]
    setup = find(F1, "activation set up").instance_id
    frs = [fr for fo in payload["form_responses"] for fr in fo["field_responses"]]
    assert any(fr["events_custom_instances_id"] == setup and fr["value"] for fr in frs)


def draft_for(answers):
    return {"id": "p1", "events_id": "1", "form_responses": [{"id": "f1", "events_custom_forms_id": "x", "field_responses": [
        {"events_custom_instances_id": k, "value": v, "document_uuids": []} for k, v in answers.items()]}]}


def test_site_edits_win():
    store, fs = mkt(); srv.find_recaps()
    srv.fill_recap_fields("1", {ATT: "40"})
    old = json.dumps({ATT: "40"}, sort_keys=True)
    store.set_draft("1", "p1", old, None)
    fs.drs.append(draft_for({ATT: "55"}))
    r = srv.save_recap_draft("1")
    assert r["saved"] is False and r["changed"] == [find(F1, "Number of people in attendance").label]
    assert store.answers("1")[ATT] == "55"
    assert not [c for c in fs.calls if c[0] == "save_draft"]


def test_site_draft_imported_when_no_local_answers():
    store, fs = mkt(); srv.find_recaps()
    fs.drs.append(draft_for({ATT: "33"}))
    r = srv.get_recap_fields("1")
    assert store.answers("1") == {ATT: "33"}
    assert next(f for f in r["fields"] if f["id"] == ATT)["value"] == "33"


def test_save_never_calls_submit():
    store, fs = mkt(); srv.find_recaps()
    srv.fill_recap_fields("1", {ATT: "40"})
    assert srv.save_recap_draft("1")["saved"] is True
    assert {c[0] for c in fs.calls} == {"save_draft"}
    assert not hasattr(fs, "submit")


def test_upload_link_and_slots():
    store, fs = mkt(); srv.find_recaps()
    r = srv.create_photo_upload_link("1")
    assert r["url"].startswith("https://x.test/upload/") and r["unmapped"] == []
    assert r["slots"]["crowd"] == "event photo gallery" and "set up" in r["slots"]["setup"].lower()


def test_read_receipt(tmp_path):
    store, fs = mkt(); srv.find_recaps()
    assert "create_photo_upload_link" in srv.read_receipt("1")["error"]
    f = tmp_path / "r.jpg"; f.write_bytes(b"jpg")
    store.add_photo("1", "receipt", str(f))
    out = srv.read_receipt("1")
    assert out[0].data == b"jpg" and isinstance(out[1], str)


def test_store_delete_answers():
    s = Store(":memory:")
    s.set_answers("1", {"a": "1", "b": "2"}, set())
    s.delete_answers("1", ["a"])
    assert s.answers("1") == {"b": "2"}


def _saved_ab():
    store, fs = mkt(); srv.find_recaps()
    srv.fill_recap_fields("1", {ATT: "40", ENG: "12"})
    store.set_draft("1", "p1", json.dumps({ATT: "40", ENG: "12"}, sort_keys=True), None)
    return store, fs


def _no_save(fs):
    return not [c for c in fs.calls if c[0] == "save_draft"]


def test_site_deletion_wins():  # (a)
    store, fs = _saved_ab()
    fs.drs.append(draft_for({ATT: "40"}))
    r = srv.save_recap_draft("1")
    assert r["saved"] is False and r["changed"] == [find(F1, "Number of Consumers Engaged").label]
    assert store.answers("1") == {ATT: "40"} and _no_save(fs)


def test_local_addition_kept_when_site_unchanged():  # (b)
    store, fs = _saved_ab()
    srv.fill_recap_fields("1", {find(F1, "Number of Consumers Sampled").instance_id: "5"})
    fs.drs.append(draft_for({ATT: "40", ENG: "12"}))
    assert srv.save_recap_draft("1")["saved"] is True
    assert len(store.answers("1")) == 3


def test_site_edit_and_local_addition():  # (c)
    store, fs = _saved_ab()
    c = find(F1, "Number of Consumers Sampled").instance_id
    srv.fill_recap_fields("1", {c: "5"})
    fs.drs.append(draft_for({ATT: "55", ENG: "12"}))
    r = srv.save_recap_draft("1")
    assert r["saved"] is False and store.answers("1") == {ATT: "55", ENG: "12", c: "5"} and _no_save(fs)


def test_no_previous_save_keeps_local_only_keys():  # (d)
    store, fs = mkt(); srv.find_recaps()
    srv.fill_recap_fields("1", {ATT: "41", ENG: "12"})
    fs.drs.append(draft_for({ATT: "40"}))
    r = srv.save_recap_draft("1")
    assert r["saved"] is False and store.answers("1") == {ATT: "40", ENG: "12"} and _no_save(fs)


# ---------------- photo upload page (Task 8) ----------------
import io  # noqa: E402

from PIL import Image as PILImage  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def _jpeg(color="red"):
    b = io.BytesIO(); PILImage.new("RGB", (40, 30), color).save(b, "JPEG"); return b.getvalue()


def _web(monkeypatch, tmp_path):
    monkeypatch.setenv("RECAP_DATA", str(tmp_path))
    store, fs = mkt(); srv.find_recaps()
    fs.removed, fs.remove_error = [], None

    def remove(ids):
        if fs.remove_error:
            raise fs.remove_error
        fs.removed += list(ids)
    fs.remove_gallery_photos = remove
    app = srv.build_server(store, fs, "https://x.test").streamable_http_app(streamable_http_path="/secret/mcp")
    return store, fs, TestClient(app, follow_redirects=False)


def test_upload_rejects_unreadable_image(monkeypatch, tmp_path):
    store, _, c = _web(monkeypatch, tmp_path)
    t = store.new_link("1")
    r = c.post(f"/upload/{t}/crowd", files=[("files", ("a.jpg", b"not an image", "image/jpeg"))])
    assert r.status_code == 400 and store.photos("1") == []


def test_upload_link_expires(monkeypatch, tmp_path):
    store, _, c = _web(monkeypatch, tmp_path)
    t = store.new_link("1", minutes=-1)
    assert c.get(f"/upload/{t}").status_code == 404
    assert c.post(f"/upload/{t}/crowd", files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))]).status_code == 404
    assert c.get("/upload/bogus").status_code == 404


def test_crowd_delete_removes_site_copy(monkeypatch, tmp_path):
    store, fs, c = _web(monkeypatch, tmp_path)
    t = store.new_link("1")
    assert c.post(f"/upload/{t}/crowd", files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))]).status_code == 303
    p = store.photos("1", "crowd")[0]
    store.mark_uploaded(p["id"], "77", None)
    assert c.post(f"/upload/{t}/delete/{p['id']}").status_code == 303
    assert fs.removed == ["77"] and store.photos("1") == [] and not Path(p["path"]).exists()


def test_crowd_delete_keeps_row_when_site_fails(monkeypatch, tmp_path):
    store, fs, c = _web(monkeypatch, tmp_path)
    t = store.new_link("1")
    c.post(f"/upload/{t}/crowd", files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))])
    p = store.photos("1")[0]
    store.mark_uploaded(p["id"], "77", None)
    fs.remove_error = SiteError("site down")
    r = c.post(f"/upload/{t}/delete/{p['id']}")
    assert "site down" in r.text and len(store.photos("1")) == 1


def test_receipt_slot_sets_answer(monkeypatch, tmp_path):
    store, _, c = _web(monkeypatch, tmp_path)
    t = store.new_link("1")
    rid = srv.slot_field(F1, "receipt").instance_id
    r = c.post(f"/upload/{t}/receipt", files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))])
    assert r.status_code == 303 and store.answers("1")[rid] == "photo"
    c.post(f"/upload/{t}/receipt", files=[("files", ("b.jpg", _jpeg("blue"), "image/jpeg"))])
    rows = store.photos("1", "receipt")
    assert len(rows) == 1  # replaced
    assert c.get(f"/upload/{t}/thumb/{rows[0]['id']}").headers["content-type"] == "image/jpeg"
    c.post(f"/upload/{t}/delete/{rows[0]['id']}")
    assert rid not in store.answers("1")
    assert c.get(f"/upload/{t}").status_code == 200
    assert c.post(f"/upload/{t}/bogus", files=[("files", ("a.jpg", _jpeg(), "image/jpeg"))]).status_code == 404


def test_photo_id_scoped_to_token_event(monkeypatch, tmp_path):
    store, _, c = _web(monkeypatch, tmp_path)
    other = tmp_path / "o.jpg"; other.write_bytes(_jpeg())
    pid = store.add_photo("2", "crowd", str(other))
    t = store.new_link("1")
    assert c.get(f"/upload/{t}/thumb/{pid}").status_code == 404
    assert c.post(f"/upload/{t}/delete/{pid}").status_code == 404
    assert len(store.photos("2")) == 1 and other.exists()


# ---------------- CLI (Task 9) ----------------
def test_make_app_secret_path_and_host_guard():
    from recap_mcp.__main__ import make_app
    app = make_app(Store(":memory:"), FakeSite(), "x.ngrok.app", "s3cret")
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:  # context runs the app lifespan
        _probe(c)


def _probe(c):
    assert c.post("/wrong/mcp", json={}).status_code == 404
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = c.post("/s3cret/mcp", json=init, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code != 404


def test_log_filter_redacts_bearer():
    import logging
    from recap_mcp.__main__ import RedactBearer
    rec = logging.LogRecord("x", logging.INFO, "", 0, "Authorization: %s", ("Bearer abc.def",), None)
    RedactBearer().filter(rec)
    assert rec.getMessage() == "Authorization: Bearer ***"
    rec = logging.LogRecord("x", logging.INFO, "", 0, "Authorization: Bearer abc.def", (), None)
    RedactBearer().filter(rec)
    assert rec.getMessage() == "Authorization: Bearer ***"


def test_setup_logging_redacts_uvicorn_records(tmp_path):
    import logging
    from recap_mcp.__main__ import setup_logging
    root, before = logging.getLogger(), list(logging.getLogger().handlers)
    try:
        setup_logging(tmp_path / "server.log")
        logging.getLogger("uvicorn.error").error("auth Bearer x")
        for h in root.handlers:
            h.flush()
        txt = (tmp_path / "server.log").read_text(encoding="utf-8")
        assert "Bearer ***" in txt and "Bearer x" not in txt
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h); h.close()
