import json
from datetime import datetime

import pytest

from recap_mcp.recap import (AnswerError, brand_of, check_rules, coerce, find, match_events, missing,
                             needs_recap, parse_definition, visible_ids)
from recap_mcp.recap import PHOENIX, answers_from_draft, build_draft_payload

D = json.load(open("tests/fixtures/def1.json", encoding="utf-8"))


def test_parse_def1():
    f = parse_definition(D)
    assert len(f) == 31
    a = find(f, "Number of people in attendance")
    assert a.type == "FIELD_INTEGER" and a.required
    assert find(f, "Conversion Rate").type == "FIELD_FORMULA"
    assert find(f, "reasons why you view the event as successful").options["Right Account Type"] == "4287"


def test_visibility_yes_no():
    f = parse_definition(D)
    checkin = find(f, "check in with a manager")
    name = find(f, "what was the manager's name")
    assert name.instance_id not in visible_ids(f, {})
    assert name.instance_id in visible_ids(f, {checkin.instance_id: "t"})
    assert name.instance_id not in visible_ids(f, {checkin.instance_id: "f"})


def test_trade_spend_reveals_spend_tip_receipt():
    f = parse_definition(D)
    ts = find(f, "trade spend or a tip")
    v = visible_ids(f, {ts.instance_id: "t"})
    for lbl in ("Total Product Spend", "Tip (20%)", "itemized receipt"):
        assert find(f, lbl).instance_id in v


def test_dangling_logic_ref_keeps_field_visible_and_reported():
    from recap_mcp.recap import unknown_logic
    d = {"pages": [{"items": [{"id": "s", "items": [{
        "id": "1", "field": {"id": "9", "name": "Orphan", "defined_as": "FIELD_TEXT"},
        "logic_groups": [{"conditions": [{"logic": "EQUALS", "value": "1",
                                          "references_events_custom_instances_id": "999"}]}],
    }]}]}]}
    f = parse_definition(d)
    assert "1" in visible_ids(f, {})
    assert unknown_logic(f) == ["Orphan: unknown reference 999"]


def test_coerce_messy_voice_values():
    f = parse_definition(D)
    assert coerce(find(f, "Number of people in attendance"), " 40 ") == "40"
    assert coerce(find(f, "Number of people in attendance"), "40.0") == "40"
    assert coerce(find(f, "Total Product Spend"), "$49.5") == "49.50"
    assert coerce(find(f, "check in with a manager"), "Yeah") == "t"
    assert coerce(find(f, "check in with a manager"), False) == "f"
    with pytest.raises(AnswerError):
        coerce(find(f, "Number of people in attendance"), "about forty")
    with pytest.raises(AnswerError):
        coerce(find(f, "Number of people in attendance"), "-3")
    with pytest.raises(AnswerError):
        coerce(find(f, "Conversion Rate"), "50")
    with pytest.raises(AnswerError):
        coerce(find(f, "activation set up"), "x")
    with pytest.raises(AnswerError):
        coerce(find(f, "BA feedback - Highlights"), "  ")


def test_multiselect_names_to_ids():
    fld = find(parse_definition(D), "reasons why you view the event as successful")
    assert coerce(fld, ["right account type"]) == "4287"
    assert coerce(fld, "right account type").split("|") == ["4287"]
    with pytest.raises(AnswerError, match="Right Account Type"):
        coerce(fld, ["nonsense"])


def test_engaged_must_be_less_than_attendance():
    f = parse_definition(D)
    a, e = find(f, "in attendance"), find(f, "Consumers Engaged")
    assert e.instance_id in check_rules(f, {a.instance_id: "20", e.instance_id: "25"})
    assert check_rules(f, {a.instance_id: "20", e.instance_id: "15"}) == {}
    assert check_rules(f, {e.instance_id: "25"}) == {}


def test_missing_splits_answers_and_uploads():
    f = parse_definition(D)
    m = missing(f, {})
    assert find(f, "activation set up").instance_id in {x.instance_id for x in m["uploads"]}
    assert find(f, "in attendance").instance_id in {x.instance_id for x in m["answers"]}
    assert all(x.type != "FIELD_FORMULA" for x in m["answers"])
    up = find(f, "activation set up").instance_id
    assert up not in {x.instance_id for x in missing(f, {up: "photo"})["uploads"]}


def test_coerce_rejects_non_finite_numbers():
    f = parse_definition(D)
    for fld in (find(f, "Number of people in attendance"), find(f, "Total Product Spend")):
        for bad in ("nan", "inf", "-inf"):
            with pytest.raises(AnswerError):
                coerce(fld, bad)


NOW = datetime(2026, 11, 22, 18, 0, tzinfo=PHOENIX)
EV = {"id": "1", "start_timestamp": "2026-11-22 12:30:00", "events_recaps_id": "1", "recaps_locked": "f",
      "events_statuses_definition": "STATUS_STAFFED"}


def test_needs_recap_rule():
    pos = {"recap_on_portal": "t"}
    assert needs_recap(pos, EV, set(), NOW)
    assert not needs_recap({"recap_on_portal": "f"}, EV, set(), NOW)
    assert not needs_recap(pos, EV, {"1"}, NOW)
    assert not needs_recap(pos, {**EV, "events_recaps_id": "4"}, set(), NOW)
    assert not needs_recap(pos, {**EV, "start_timestamp": "2026-11-29 12:30:00"}, set(), NOW)
    assert not needs_recap(pos, {**EV, "recaps_locked": "t"}, set(), NOW)
    assert not needs_recap(pos, {**EV, "events_recaps_id": ""}, set(), NOW)
    assert not needs_recap(pos, {**EV, "events_statuses_definition": "STATUS_CANCELLED"}, set(), NOW)


def test_brand_of():
    assert brand_of({"scheduling_fields": [{"name": "Brand Family", "print_value": "Bud Light"}]}) == "Bud Light"
    assert brand_of({"scheduling_fields": []}) is None


def test_match_normalises_names():
    rows = [{"venue_name": "ERNIE S INN", "start_ts": "2026-11-13 19:30:00"},
            {"venue_name": "Tacos & Craft", "start_ts": "2026-11-21 17:30:00"},
            {"venue_name": "Red Zone Sports Grill", "start_ts": "2026-11-15 12:30:00"},
            {"venue_name": "Red Zone Sports Grill", "start_ts": "2026-11-22 15:00:00"}]
    assert [r["venue_name"] for r in match_events(rows, "ernies inn", None, None)] == ["ERNIE S INN"]
    assert match_events(rows, "tacos and craft", None, None)[0]["venue_name"] == "Tacos & Craft"
    assert len(match_events(rows, "red zone", None, None)) == 2
    assert len(match_events(rows, "red zone", None, "15:00")) == 1
    assert len(match_events(rows, None, "2026-11-22", None)) == 1
    assert match_events(rows, "red zone", None, None)[0]["start_ts"] == "2026-11-22 15:00:00"


def test_payload_shape_and_visibility():
    f = parse_definition(D); a = find(f, "in attendance"); ts = find(f, "trade spend or a tip")
    p = build_draft_payload(D, f, "98033", "19985", {a.instance_id: "40", ts.instance_id: "f"}, {}, None, NOW)
    assert p["events_id"] == "98033" and p["events_recaps_id"] == D["id"] and p["status_id"] is None
    frs = [fr for form in p["form_responses"] for fr in form["field_responses"]]
    assert {fr["events_custom_instances_id"]: fr["value"] for fr in frs} == {a.instance_id: "40", ts.instance_id: "f"}
    assert all(str(fr["id"]).startswith("provisional_") for fr in frs)


def test_hidden_answers_not_sent():
    f = parse_definition(D); ts = find(f, "trade spend or a tip"); sp = find(f, "Total Product Spend")
    p = build_draft_payload(D, f, "1", "19985", {ts.instance_id: "f", sp.instance_id: "49.50"}, {}, None, NOW)
    sent = {fr["events_custom_instances_id"] for form in p["form_responses"] for fr in form["field_responses"]}
    assert sp.instance_id not in sent
    assert sp.instance_id not in {x.instance_id for x in missing(f, {ts.instance_id: "f"})["answers"]}


def test_image_docs_and_existing_ids_reused():
    f = parse_definition(D); su = find(f, "activation set up"); a = find(f, "in attendance")
    p1 = build_draft_payload(D, f, "1", "19985", {a.instance_id: "40", su.instance_id: "photo"}, {su.instance_id: [("111", "u1"), ("112", "u2")]}, None, NOW)
    fr = next(x for form in p1["form_responses"] for x in form["field_responses"] if x["events_custom_instances_id"] == su.instance_id)
    assert fr["value"] == "111|112" and fr["document_uuids"] == ["u1", "u2"]
    existing = json.loads(json.dumps(p1)); existing["id"] = "555"
    p2 = build_draft_payload(D, f, "1", "19985", {a.instance_id: "41"}, {}, existing, NOW)
    assert p2["id"] == "555"
    assert answers_from_draft(p2)[a.instance_id] == "41"
    assert su.instance_id not in answers_from_draft(p1)


def test_staff_app_photo_carried_through():  # fix 2
    f = parse_definition(D)
    up = next(x for x in f if x.type == "FIELD_IMAGE_UPLOAD" and "set up" in x.label)
    existing = {"id": "p1", "form_responses": [{"id": "f9", "events_custom_forms_id": up.form_id, "field_responses": [
        {"id": "r9", "events_custom_instances_id": up.instance_id, "value": "900", "document_uuids": ["u9"]}]}]}
    p = build_draft_payload({"id": "1", "version": "1", "updated_at": "x"}, f, "1", "7", {}, {}, existing,
                            datetime(2026, 1, 1, tzinfo=PHOENIX))
    frs = [fr for fo in p["form_responses"] for fr in fo["field_responses"]]
    fr = next(x for x in frs if x["events_custom_instances_id"] == up.instance_id)
    assert (fr["id"], fr["value"], fr["document_uuids"]) == ("r9", "900", ["u9"])


def test_match_events_tolerates_bad_rows():  # fix 4
    rows = [{"venue_name": "Red Zone", "start_ts": "2026-11-10 18:00:00"},
            {"venue_name": None, "start_ts": "2026-11-10 18:00:00"},
            {"venue_name": "Red Zone", "start_ts": "garbage"},
            {"venue_name": "Red Zone"}]
    assert match_events(rows, "red zone", "2026-11-10", "18:00") == rows[:1]
    assert len(match_events(rows, "red zone", None, None)) == 3  # no date/time: bad start_ts is not fatal
