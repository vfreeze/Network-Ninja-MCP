import json

import pytest

from recap_mcp.recap import AnswerError, check_rules, coerce, find, missing, parse_definition, visible_ids

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
