"""Pure logic: parse a recap definition and decide which fields are visible."""
import difflib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class Field:
    instance_id: str
    field_id: str
    form_id: str
    block_id: str | None
    label: str
    type: str
    required: bool
    options: dict[str, str]  # active option name -> option id
    logic: list[list[tuple[str, str, str]]]  # groups of (ref_instance_id, logic, value)
    validations: list[tuple[str, str | None, str | None]]  # (type, ref_instance_id, value.strip())


def _live(x) -> bool:
    return x.get("is_archived") != "t"


def parse_definition(defn: dict) -> list[Field]:
    out: list[Field] = []

    def walk(items, form_id):
        for it in items:
            if not _live(it):
                continue
            if "field" not in it:  # section/form
                walk(it.get("items", []), it["id"])
                continue
            fd = it["field"]
            if not _live(fd):
                continue
            opts = fd.get("options") or []
            if isinstance(opts, dict):
                opts = list(opts.values())
            vals = [
                (v["validation_type"], v.get("references_events_custom_instances_id"),
                 v["value"].strip() if v.get("value") is not None else None)
                for v in it.get("validations", []) if _live(v)
            ]
            groups = [
                [(c["references_events_custom_instances_id"], c["logic"], c["value"])
                 for c in g.get("conditions", []) if _live(c)]
                for g in it.get("logic_groups", []) if _live(g)
            ]
            out.append(Field(
                instance_id=it["id"], field_id=fd["id"], form_id=form_id,
                block_id=it.get("events_custom_field_blocks_id"),
                label=fd["name"], type=fd["defined_as"],
                required=any(v[0] == "responseRequired" for v in vals),
                options={o["name"]: o["id"] for o in opts if o.get("active") == "t"},
                logic=[g for g in groups if g], validations=vals,
            ))

    for page in defn["pages"]:
        if _live(page):
            walk(page.get("items", []), "")
    return out


def _norm(v: str) -> str:
    return {"t": "1", "f": "0"}.get(v, v)


def _holds(c, answers: dict[str, str]) -> bool:
    ref, _, want = c
    got = answers.get(ref)
    if got is None:
        return False
    return _norm(want) in {_norm(p) for p in got.split("|")}  # multiselect: value in selected ids


def visible_ids(fields: list[Field], answers: dict[str, str]) -> set[str]:
    by_id = {f.instance_id: f for f in fields}
    memo: dict[str, bool] = {}

    def vis(iid: str) -> bool:
        if iid not in by_id:
            return False
        if iid in memo:
            return memo[iid]
        memo[iid] = False  # cycle guard
        f = by_id[iid]
        if not f.logic or any(c[1] != "EQUALS" or c[0] not in by_id for g in f.logic for c in g):
            r = True  # unknown operator / dangling ref: never hide a possibly-required field
        else:
            r = any(
                all(_holds(c, answers) and vis(c[0]) for c in g) for g in f.logic
            )
        memo[iid] = r
        return r

    return {f.instance_id for f in fields if vis(f.instance_id)}


def unknown_logic(fields: list[Field]) -> list[str]:
    ids = {f.instance_id for f in fields}
    out = []
    for f in fields:
        for g in f.logic:
            for ref, op, _ in g:
                if op != "EQUALS":
                    out.append(f"{f.label}: {op}")
                if ref not in ids:
                    out.append(f"{f.label}: unknown reference {ref}")
    return out


def find(fields: list[Field], label_part: str) -> Field:
    hits = [f for f in fields if label_part.lower() in f.label.lower()]
    if len(hits) != 1:
        raise KeyError(f"{len(hits)} fields match {label_part!r}")
    return hits[0]


class AnswerError(ValueError):
    """Message is read aloud to the user."""


_YES = {"yes", "y", "yeah", "true", "t", "1"}
_NO = {"no", "n", "false", "f", "0"}


def coerce(field: Field, raw) -> str:
    t, label = field.type, field.label
    if t == "FIELD_FORMULA":
        raise AnswerError(f"{label} is calculated and set by the site.")
    if t == "FIELD_IMAGE_UPLOAD":
        raise AnswerError(f"{label} is a photo; use the photo upload link.")
    if t == "FIELD_MULTISELECT":
        names = raw if isinstance(raw, list) else str(raw).split(",")
        by_lower = {n.lower(): i for n, i in field.options.items()}
        want = {n.strip().lower() for n in names if n.strip()}
        bad = want - by_lower.keys()
        if bad:
            raise AnswerError(f"{label}: {', '.join(sorted(bad))} is not an option. Choose from {', '.join(field.options)}.")
        return "|".join(i for n, i in field.options.items() if n.lower() in want)
    if t == "FIELD_YES_NO":
        s = str(raw).strip().lower()  # str(True) == "true"
        if s in _YES:
            return "t"
        if s in _NO:
            return "f"
        raise AnswerError(f"{label} needs a yes or no answer.")
    s = str(raw).strip()
    if t == "FIELD_INTEGER":
        try:
            n = float(s.replace(",", "").replace(" ", ""))
        except ValueError:
            n = -1.5
        if not math.isfinite(n) or n < 0 or n != int(n):
            raise AnswerError(f"{label} needs a whole number, zero or more.")
        return str(int(n))
    if t == "FIELD_FLOAT2":
        try:
            n = float(s.replace("$", "").replace(",", "").replace(" ", ""))
        except ValueError:
            raise AnswerError(f"{label} needs a dollar amount like 49.50.")
        if not math.isfinite(n):
            raise AnswerError(f"{label} needs a dollar amount like 49.50.")
        return f"{n:.2f}"
    if t in ("FIELD_TEXT", "FIELD_TEXTAREA"):
        if not s:
            raise AnswerError(f"{label} can't be empty.")
        return s
    raise AnswerError(f"{label} has field type {t}, which isn't supported.")


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def check_rules(fields: list[Field], answers: dict[str, str]) -> dict[str, str]:
    by_id = {f.instance_id: f for f in fields}
    vis = visible_ids(fields, answers)
    out: dict[str, str] = {}
    for f in fields:
        mine = _num(answers.get(f.instance_id))
        if f.instance_id not in vis or mine is None:
            continue
        for typ, ref, val in f.validations:
            if typ == "lteValue":
                lim, msg = _num(val), f"{f.label} must be {val} or less."
            elif typ in ("ltField", "lteField") and ref in by_id:
                lim = _num(answers.get(ref))
                word = "less than" if typ == "ltField" else "no more than"
                msg = f"{f.label} must be {word} {by_id[ref].label}."
            else:
                continue
            if lim is not None and (mine >= lim if typ == "ltField" else mine > lim):
                out[f.instance_id] = msg
                break
    return out


def missing(fields: list[Field], answers: dict[str, str]) -> dict[str, list[Field]]:
    vis = visible_ids(fields, answers)
    out: dict[str, list[Field]] = {"answers": [], "uploads": []}
    for f in fields:
        if f.instance_id in vis and f.required and f.type != "FIELD_FORMULA" and not answers.get(f.instance_id):
            out["uploads" if f.type == "FIELD_IMAGE_UPLOAD" else "answers"].append(f)
    return out


PHOENIX = timezone(timedelta(hours=-7), "MST")  # Arizona has no DST; site timestamps are naive local


def needs_recap(position: dict, event: dict, submitted_event_ids: set[str], now: datetime) -> bool:
    start = datetime.strptime(event["start_timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=PHOENIX)
    rid = event.get("events_recaps_id")
    return bool(
        start < now.astimezone(PHOENIX)
        and position.get("recap_on_portal") == "t"
        and rid and rid != "4"
        and event.get("recaps_locked") != "t"
        and "CANCEL" not in (event.get("events_statuses_definition") or "")
        and event["id"] not in submitted_event_ids
    )


def brand_of(event: dict) -> str | None:
    for f in event.get("scheduling_fields", []):
        if f.get("name") == "Brand Family":
            return f.get("print_value")
    return None


def normalise_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", s.lower().replace("&", " and ").replace("'", ""))
    return " ".join(s.split())


def match_events(rows: list[dict], bar_name: str | None, date: str | None, time: str | None) -> list[dict]:
    q = normalise_name(bar_name) if bar_name else None
    out = []
    for r in rows:
        if q:
            v = normalise_name(r.get("venue_name") or "")
            if q not in v and difflib.SequenceMatcher(None, q, v).ratio() < 0.6:
                continue
        if date or time:
            try:
                start = datetime.strptime(r.get("start_ts") or "", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue  # unparseable start: can't match a date/time
        if date and not r["start_ts"].startswith(date):
            continue
        if time:
            want = datetime.strptime(f"{start:%Y-%m-%d} {time}", "%Y-%m-%d %H:%M")
            if abs(start - want) > timedelta(minutes=60):
                continue
        out.append(r)
    return sorted(out, key=lambda r: r.get("start_ts") or "", reverse=True)


def build_draft_payload(defn: dict, fields: list[Field], event_id: str, uid: str,
                        answers: dict[str, str], photo_docs: dict[str, list[tuple[str, str]]],
                        existing: dict | None, now: datetime) -> dict:
    existing = existing or {}
    n = 0

    def prov() -> str:
        nonlocal n
        n += 1
        return f"provisional_{n}"

    old_forms = {f["events_custom_forms_id"]: f for f in existing.get("form_responses", [])}
    old_fields = {fr["events_custom_instances_id"]: fr
                  for f in old_forms.values() for fr in f.get("field_responses", [])}
    local = now.astimezone(PHOENIX)
    rid = existing.get("id") or prov()
    vis = visible_ids(fields, answers)
    forms: dict[str, dict] = {}
    for f in fields:
        if f.instance_id not in vis or f.type == "FIELD_FORMULA":
            continue
        if f.type == "FIELD_IMAGE_UPLOAD":
            docs = photo_docs.get(f.instance_id) or []
            value, uuids = "|".join(d for d, _ in docs), [u for _, u in docs]
            kept = old_fields.get(f.instance_id)
            if not docs and kept and kept.get("value"):  # photo added in the Staff App: carry it through
                value, uuids = kept["value"], kept.get("document_uuids") or []
        else:
            value, uuids = answers.get(f.instance_id), []
        if not value:
            continue
        form = forms.get(f.form_id)
        if form is None:
            form = forms[f.form_id] = {
                "id": old_forms.get(f.form_id, {}).get("id") or prov(),
                "events_custom_responses_id": rid, "events_custom_forms_id": f.form_id,
                "products_id": None, "product_labels_id": None, "merchandising_activities_id": None,
                "customers_id": None, "sampling_feedback": None, "out_of_stock": False,
                "field_responses": [],
            }
        form["field_responses"].append({
            "id": old_fields.get(f.instance_id, {}).get("id") or prov(), "value": value,
            "visible_on_submit": True, "data_link_field_names": None, "events_id": event_id,
            "events_custom_id": f.field_id, "events_custom_responses_id": rid,
            "events_custom_forms_responses_id": form["id"], "events_custom_field_blocks_id": f.block_id,
            "events_custom_instances_id": f.instance_id, "document_uuids": uuids,
        })
    return {
        "id": rid, "staffing_users_id": uid, "users_id": None,
        "started_at": existing.get("started_at") or local.strftime("%Y-%m-%d %H:%M:%S"),
        "ended_at": local.strftime("%Y-%m-%d %H:%M:%S"),
        "started_at_tz": existing.get("started_at_tz") or local.isoformat(timespec="seconds"),
        "ended_at_tz": local.isoformat(timespec="seconds"),
        "start_lat": None, "start_lon": None, "end_lat": None, "end_lon": None,
        "events_id": event_id, "events_recaps_id": defn["id"],
        "events_recaps_external_links_views_id": None,
        "events_recaps_version": defn["version"], "events_recaps_updated_at": defn["updated_at"],
        "staff_app_version": defn.get("_app_version", "5.61.0"), "staff_app_version_full": "recap-mcp",
        "last_staff_app_data_sync_uuid": None, "status_id": None,
        "form_responses": list(forms.values()), "form_comments": [],
    }


def answers_from_draft(draft: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for form in draft.get("form_responses", []):
        for fr in form.get("field_responses", []):
            if fr.get("field_type") in ("FIELD_FORMULA", "FIELD_IMAGE_UPLOAD") or fr.get("document_uuids"):
                continue
            out[str(fr["events_custom_instances_id"])] = fr["value"]
    return out
