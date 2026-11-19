"""MCP tools (spec 5.4). Sync defs: the SDK runs them in a worker thread, so blocking I/O is fine."""
import functools
import json
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import Image, MCPServer

from .recap import (PHOENIX, AnswerError, Field, answers_from_draft, brand_of, build_draft_payload, check_rules,
                    coerce, match_events, missing, needs_recap, parse_definition, visible_ids)
from .site import SiteError

SLOT_KEYWORDS = {"setup": ["set up", "set-up"], "receipt": ["receipt"],
                 "sign": ["feature sign", "display", "branding"]}
UNKNOWN = "Unknown shift id — call find_recaps first."
TYPES = {"1": "On Premise", "2": "Off Premise", "7": "Cancelled"}
GALLERY = "event photo gallery"


class Deps:
    store = None
    site = None
    public_url = ""


def slot_field(fields: list[Field], slot: str) -> Field | None:
    for f in fields:
        if f.type == "FIELD_IMAGE_UPLOAD" and any(k in f.label.lower() for k in SLOT_KEYWORDS.get(slot, [])):
            return f
    return None


def _safe(fn):
    @functools.wraps(fn)
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except (SiteError, AnswerError) as e:
            return {"error": str(e)}
    return run


def _now() -> datetime:
    return datetime.now(PHOENIX)


def _fp(answers: dict) -> str:  # the answers we last sent (kept in drafts.fingerprint)
    return json.dumps(answers, sort_keys=True)


def _site_draft(events_id: str) -> dict | None:
    return next((d for d in Deps.site.drafts() if str(d.get("events_id")) == events_id), None)


def _load(events_id: str, import_draft: bool = False):
    ev = Deps.store.event(str(events_id))
    if not ev:
        raise AnswerError(UNKNOWN)
    defn = Deps.site.definition(ev["recap_def_id"])
    fields = parse_definition(defn)
    if import_draft and not Deps.store.answers(ev["events_id"]):  # site wins
        d = _site_draft(ev["events_id"])
        if d and answers_from_draft(d):
            Deps.store.set_answers(ev["events_id"], answers_from_draft(d), set())
    return ev, defn, fields


def _with_photos(events_id: str, fields: list[Field], answers: dict) -> dict:
    out = dict(answers)
    for p in Deps.store.photos(events_id):
        f = slot_field(fields, p["slot"])
        if f:
            out[f.instance_id] = "photo"
    return out


def _report(events_id: str, fields: list[Field]) -> dict:
    m = missing(fields, _with_photos(events_id, fields, Deps.store.answers(events_id)))
    ans = {f.instance_id: f.label for f in m["answers"]}
    up = {f.instance_id: f.label for f in m["uploads"]}
    return {"answers": ans, "uploads": up,
            "summary": f"{len(ans)} required answers missing, {len(up)} uploads missing."}


def _refresh(force: bool = False) -> None:
    st, site = Deps.store, Deps.site
    last = st.meta_get("last_refresh")
    now = datetime.now(timezone.utc)
    if not force and last and now - datetime.fromtimestamp(float(last), timezone.utc) < timedelta(minutes=5):
        return
    events = {str(e["id"]): e for e in site.events()}
    submitted = {str(s["events_id"]) for s in site.submitted()}
    drafted = {str(d.get("events_id")) for d in site.drafts()}
    rows, seen = [], set()
    for pos in site.positions():
        e = events.get(str(pos.get("events_id")))
        if not e:
            continue
        eid = str(e["id"])
        if eid in submitted:
            status = "submitted"
        elif needs_recap(pos, {**e, "id": eid}, submitted, _now()):
            status = "draft_saved" if eid in drafted else "needs_recap"
        else:
            continue
        seen.add(eid)
        rows.append({"events_id": eid, "position_id": str(pos.get("id")), "venue_name": e["venue_name"],
                     "address": e.get("street_address1"), "city": e.get("city"),
                     "start_ts": e["start_timestamp"], "end_ts": e.get("end_timestamp"), "brand": brand_of(e),
                     "recap_def_id": str(e.get("events_recaps_id")), "recap_def_name": e.get("events_recaps_name"),
                     "site_status": status, "refreshed_at": now.isoformat()})
    for old in st.events():  # no longer pending on the site (locked, cancelled...)
        if old["events_id"] not in seen:
            rows.append({**old, "site_status": "done"})
    st.upsert_events(rows)
    st.meta_set("last_refresh", str(now.timestamp()))


@_safe
def find_recaps(bar_name: str | None = None, date: str | None = None, time: str | None = None,
                include_done: bool = False) -> dict:
    _refresh()
    rows = Deps.store.events(only_pending=False)
    ok = {"needs_recap", "draft_saved"} | ({"submitted"} if include_done else set())
    hits = match_events([r for r in rows if r["site_status"] in ok], bar_name, date, time)
    out = [{"events_id": r["events_id"], "bar": r["venue_name"], "city": r["city"], "date": r["start_ts"][:10],
            "start_time": r["start_ts"][11:16], "brand": r["brand"],
            "recap_type": TYPES.get(r["recap_def_id"], "Recap"), "status": r["site_status"]} for r in hits]
    res: dict = {"shifts": out}
    if len(out) > 1:
        res["ask_user"] = "Several shifts match — ask which one (date/time/city)."
    return res


def _view(f: Field, answers: dict, vis: set, photos: set) -> dict:
    v = answers.get(f.instance_id)
    if f.type == "FIELD_MULTISELECT" and v:
        ids = v.split("|")
        v = [n for n, i in f.options.items() if i in ids]
    elif f.type == "FIELD_YES_NO" and v:
        v = "yes" if v == "t" else "no"
    elif f.type == "FIELD_IMAGE_UPLOAD" and f.instance_id in photos:
        v = "photo uploaded"
    status = ("hidden" if f.instance_id not in vis else "filled" if v else
              "missing" if f.required and f.type != "FIELD_FORMULA" else "optional")
    return {"id": f.instance_id, "label": f.label, "type": f.type, "required": f.required,
            "options": list(f.options), "value": v, "status": status}


@_safe
def get_recap_fields(events_id: str, only: str = "all") -> dict:
    ev, _, fields = _load(events_id, True)
    ans = Deps.store.answers(ev["events_id"])
    aug = _with_photos(ev["events_id"], fields, ans)
    photos = {f.instance_id for f in fields if f.instance_id in aug and f.type == "FIELD_IMAGE_UPLOAD"}
    views = [_view(f, ans, visible_ids(fields, aug), photos) for f in fields]
    if only == "missing":
        views = [v for v in views if v["status"] == "missing"]
    elif only == "uploads":
        views = [v for v in views if v["type"] == "FIELD_IMAGE_UPLOAD" and v["status"] != "hidden" and not v["value"]]
    return {"fields": views, **_report(ev["events_id"], fields)}


@_safe
def fill_recap_fields(events_id: str, answers: dict[str, str | int | float | bool | list[str]],
                      estimated: list[str] | None = None) -> dict:
    ev, _, fields = _load(events_id, True)
    eid, by_id, rejected, good = ev["events_id"], {f.instance_id: f for f in fields}, {}, {}
    for k, raw in answers.items():
        if k not in by_id:
            rejected[k] = "Unknown field id."
            continue
        try:
            good[k] = coerce(by_id[k], raw)
        except AnswerError as e:
            rejected[k] = str(e)
    for k, msg in check_rules(fields, {**Deps.store.answers(eid), **good}).items():
        if k in good:
            rejected[k] = msg
            del good[k]
    Deps.store.set_answers(eid, good, set(estimated or []) & good.keys())
    tell = "Read the still-missing items to the user and ask for them."
    if estimated:
        tell += " Tell the user which values are estimates."
    if rejected:
        tell += " Some answers were rejected; explain why and ask again."
    return {"saved": list(good), "rejected": rejected, "still_missing": _report(eid, fields), "tell_user": tell}


@_safe
def create_photo_upload_link(events_id: str) -> dict:
    ev, _, fields = _load(events_id)
    mapped = {s: slot_field(fields, s) for s in SLOT_KEYWORDS}
    token = Deps.store.new_link(ev["events_id"], 60)
    slots = {s: f.label for s, f in mapped.items() if f} | {"crowd": GALLERY}
    return {"url": f"{Deps.public_url.rstrip('/')}/upload/{token}", "expires_in_minutes": 60, "slots": slots,
            "unmapped": [s for s, f in mapped.items() if not f]}


@_safe
def read_receipt(events_id: str) -> list[Image | str] | dict:
    ev, _, _ = _load(events_id)
    rows = Deps.store.photos(ev["events_id"], "receipt")
    if not rows:
        return {"error": "No receipt photo yet. Call create_photo_upload_link and ask the user to upload it."}
    with open(rows[-1]["path"], "rb") as fh:
        data = fh.read()
    return [Image(data=data, format="jpeg"),
            "Read the pre-tip subtotal, tip, total and line items/quantities (they inform consumers sampled). "
            "Say them back to the user, flag it if total != subtotal + tip, then call fill_recap_fields for the "
            "spend and tip fields. Total Spend is calculated by the site."]


@_safe
def save_recap_draft(events_id: str) -> dict:
    ev, defn, fields = _load(events_id)
    eid, st, site = ev["events_id"], Deps.store, Deps.site
    _refresh(force=True)
    if st.event(eid)["site_status"] not in ("needs_recap", "draft_saved"):
        return {"saved": False, "reason": "This shift no longer needs a recap (submitted or locked)."}
    existing = _site_draft(eid)
    if existing:  # 3-way: compare site draft with what we last sent; local-only keys are never touched
        site_a = answers_from_draft(existing)
        sent = json.loads((st.draft(eid) or {}).get("fingerprint") or "{}")
        diff = [k for k in site_a.keys() | sent.keys() if site_a.get(k) != sent.get(k)]
        if diff:
            by_id = {f.instance_id: f.label for f in fields}
            st.set_answers(eid, {k: site_a[k] for k in diff if k in site_a}, set())
            st.delete_answers(eid, [k for k in diff if k not in site_a])  # deleted on the site
            return {"saved": False, "changed": [by_id.get(k, k) for k in diff],
                    "reason": "You edited this draft in the Staff App; I pulled your changes in."}
    for p in st.photos(eid):
        if p["site_document_id"]:
            continue
        with open(p["path"], "rb") as fh:
            data = fh.read()
        if p["slot"] == "crowd":
            st.mark_uploaded(p["id"], site.upload_gallery_photo(eid, data), None)
        elif (f := slot_field(fields, p["slot"])):
            st.mark_uploaded(p["id"], *site.upload_recap_photo(eid, f.field_id, data))
    docs: dict[str, list] = {}
    for p in st.photos(eid):
        f = slot_field(fields, p["slot"])
        if f and p["site_document_id"]:
            docs.setdefault(f.instance_id, []).append((p["site_document_id"], p["site_document_uuid"]))
    answers = st.answers(eid)
    payload = build_draft_payload({**defn, "_app_version": site.app_version()}, fields, eid, site.uid(),
                                  answers, docs, existing, _now())
    try:
        resp = site.save_draft(payload)
    except SiteError as e:
        old = st.draft(eid) or {}
        st.set_draft(eid, old.get("partial_id"), old.get("fingerprint"), str(e))
        return {"error": f"The site rejected the draft: {e}"}
    st.set_draft(eid, str((resp or {}).get("id") or payload["id"]), _fp(answers_from_draft(payload)), None)
    st.upsert_events([{**st.event(eid), "site_status": "draft_saved"}])
    return {"saved": True, "still_missing": _report(eid, fields),
            "message": "Draft saved, not submitted. Open the Staff App to review and submit."}


DESCRIPTIONS = {
    find_recaps: "List shifts needing a recap (optionally by bar name/date/time). If several match, ask the user "
                 "which one (date/time/city) before continuing. Use events_id in the other tools.",
    get_recap_fields: "Show the recap questions with current values and what is missing. only: all|missing|uploads.",
    fill_recap_fields: "Save answers keyed by field id. Only fill what the user actually said; put estimated "
                       "numbers in `estimated` and tell the user they are estimates. Never invent feedback text. "
                       "Then read the still-missing items to the user.",
    create_photo_upload_link: "Get a phone upload link (60 min) for setup, receipt, feature sign and crowd photos.",
    read_receipt: "View the uploaded receipt photo; read subtotal, tip, total and items, then fill the fields.",
    save_recap_draft: "Save the recap as a DRAFT on the Staff App. This never submits: tell the user to open the "
                      "Staff App to review and submit. If the user edited the draft there, their changes win.",
}


def build_server(store, site, public_url: str) -> MCPServer:
    Deps.store, Deps.site, Deps.public_url = store, site, public_url
    server = MCPServer("recap-mcp")
    for fn, desc in DESCRIPTIONS.items():
        server.add_tool(fn, description=desc)
    from .upload import register_upload_routes  # late: upload imports this module
    register_upload_routes(server)
    return server
