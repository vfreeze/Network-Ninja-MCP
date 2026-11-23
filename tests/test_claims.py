from datetime import datetime

import pytest

from recap_mcp import server
from recap_mcp.claims import build_claim_payload, claim_rows, parse_time, primary_types
from recap_mcp.recap import PHOENIX, AnswerError

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=PHOENIX)
EVENT = {"id": "97721", "venue_name": "Red Zone", "city": "Peoria", "claims_locked": "f",
         "claim_types": [{"id": 2594, "claim_categories_id": 1, "name": "Event Pay", "active": True,
                          "allow_primary_claim": True, "position_access": [334, 335],
                          "description": "Actual times must match scheduled"},
                         {"id": 2595, "claim_categories_id": 1, "name": "Admin", "active": True,
                          "allow_primary_claim": True, "position_access": [999]},
                         {"id": 7, "claim_categories_id": 2, "name": "Mileage", "position_access": [335]}]}


def pos(**kw):
    return {"id": "148947", "events_id": "97721", "real_events_id": "97721", "start_time": "2026-11-21 20:00:00",
            "end_time": "2026-11-21 22:30:00", "allow_claims": "t", "staffing_positions_id": "335",
            "claim_date": None, "claim_types_id": None, "primary_claim": None, **kw}


def test_claim_rows_statuses():
    ev = {"97721": EVENT, "2": {**EVENT, "id": "2", "claims_locked": "t"}, "3": {**EVENT, "id": "3"}}
    rows = claim_rows([pos(), pos(events_id="2"), pos(events_id="3", primary_claim={"claim_statuses_name": "Pending",
                       "units": "2.5", "amount": "87.5", "claim_start_time": "20:00:00", "claim_end_time": "22:30:00"}),
                       pos(events_id="97721", start_time="2026-11-23 20:00:00"),  # future
                       pos(events_id="404")], ev, NOW)
    assert [(r["events_id"], r["claim_status"]) for r in rows] == [("97721", "needs_claim"), ("2", "locked"), ("3", "Pending")]
    assert rows[2]["claimed_times"] == "20:00-22:30" and rows[0]["scheduled"] == "20:00-22:30"


def test_primary_types_filters_category_and_position():
    assert [t["name"] for t in primary_types(EVENT, pos())] == ["Event Pay"]


def test_parse_time_formats():
    day = datetime(2026, 11, 21)
    assert parse_time(day, "20:15").hour == 20 and parse_time(day, "8:15 pm").minute == 15
    assert parse_time(day, "8 p.m.").hour == 20
    with pytest.raises(AnswerError):
        parse_time(day, "late")


def test_payload_matches_app_shape_and_handles_midnight():
    s, e = datetime(2026, 11, 21, 22, 0), datetime(2026, 11, 21, 1, 30)
    p = build_claim_payload(pos(), EVENT, "19985", "2594", s, e)
    c, q = p["primary_claim"], p["position"]
    assert c["claim_start_time"] == "10:00 pm" and c["claim_end_time"] == "01:30 am"
    assert c["hours"] == 3.5 and c["claim_types_id"] == 2594 and c["primary_claim"] == "t"
    assert c["claim_date"] == "2026-11-21 00:00:00" and c["events_staffing_days_positions_id"] == "148947"
    assert q["actual_start"] == "2026-11-21 22:00:00" and q["actual_end"] == "2026-11-22 01:30:00"


class FakeSite:
    def __init__(self, positions):
        self._p, self.posted = positions, []

    def positions(self):
        return self._p

    def events(self):
        return [EVENT]

    def uid(self):
        return "19985"

    def submit_primary_claim(self, pid, payload):
        self.posted.append((pid, payload))
        return {"primary_claim": {"claim_statuses_name": "Pending", "amount": "87.5"}}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(server, "_now", lambda: NOW)
    def make(positions):
        server.Deps.site = FakeSite(positions)
        return server.Deps.site
    return make


def test_submit_claim_previews_then_posts_only_on_confirm(fake):
    site = fake([pos()])
    r = server.submit_claim("97721")
    assert r["submitted"] is False and r["preview"]["hours"] == 2.5 and "warning" not in r["preview"]
    assert site.posted == []
    r = server.submit_claim("97721", confirm=True)
    assert r["submitted"] is True and r["claim_status"] == "Pending" and site.posted[0][0] == "148947"


def test_submit_claim_warns_on_off_schedule_times_and_refuses_existing(fake):
    fake([pos()])
    r = server.submit_claim("97721", actual_end="11 pm")
    assert "must match" in r["preview"]["warning"]
    site = fake([pos(primary_claim={"claim_statuses_name": "Approved"})])
    assert "Approved" in server.submit_claim("97721", confirm=True)["error"] and site.posted == []


def test_check_claims_hides_approved_by_default(fake):
    fake([pos(), pos(events_id="97721", id="2", primary_claim={"claim_statuses_name": "Approved"})])
    r = server.check_claims()
    assert [s["claim_status"] for s in r["shifts"]] == ["needs_claim"] and r["needs_claim"] == 1
    assert len(server.check_claims(include_all=True)["shifts"]) == 2
