import json

from recap_mcp.recap import find, parse_definition, visible_ids

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
