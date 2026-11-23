"""Pay claims: the "actual times" primary claim the Staff App files per shift (mirrors ClaimService.serializePrimaryClaim)."""
from datetime import datetime, timedelta

from .recap import PHOENIX, AnswerError

TS = "%Y-%m-%d %H:%M:%S"


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s or "", TS)
    except ValueError:
        return None


def claim_rows(positions: list[dict], events: dict[str, dict], now: datetime) -> list[dict]:
    """One row per past, claimable shift: needs_claim / locked / the site's claim status."""
    rows = []
    for p in positions:
        e = events.get(str(p.get("events_id")))
        start = _ts(p.get("start_time"))
        if (not e or not start or start.replace(tzinfo=PHOENIX) > now.astimezone(PHOENIX)
                or p.get("allow_claims") == "f" or "CANCEL" in (e.get("events_statuses_definition") or "")):
            continue
        c = p.get("primary_claim") or None
        status = (c.get("claim_statuses_name") or "Submitted") if c else (
            "locked" if e.get("claims_locked") == "t" else "needs_claim")
        rows.append({"events_id": str(e["id"]), "venue_name": e.get("venue_name") or "", "city": e.get("city"),
                     "start_ts": p["start_time"], "scheduled": f"{p['start_time'][11:16]}-{(p.get('end_time') or '')[11:16]}",
                     "claim_status": status, "hours": c and c.get("units"), "amount": c and c.get("amount"),
                     "claimed_times": c and f"{(c.get('claim_start_time') or '')[:5]}-{(c.get('claim_end_time') or '')[:5]}"})
    return rows


def parse_time(day: datetime, s: str) -> datetime:
    s = s.strip().lower().replace(".", "")
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt)
            return day.replace(hour=t.hour, minute=t.minute, second=0)
        except ValueError:
            pass
    raise AnswerError(f"Could not read the time {s!r}; use HH:MM (24h) or like 8:30 pm.")


def primary_types(event: dict, position: dict) -> list[dict]:
    """Claim types the app offers for this shift's primary claim (time-based, allowed, for this position type)."""
    pt = int(position.get("staffing_positions_id") or 0)
    return [t for t in event.get("claim_types") or []
            if str(t.get("claim_categories_id")) == "1" and t.get("allow_primary_claim") not in (False, "f")
            and t.get("active") not in (False, "f") and pt in (t.get("position_access") or [])]


def build_claim_payload(position: dict, event: dict, uid: str, claim_type_id: str,
                        start: datetime, end: datetime) -> dict:
    if end <= start:  # past midnight (normalizeSingleDayStartEndDates)
        end += timedelta(days=1)
    hours = round((end - start).total_seconds() / 3600, 4)
    claim_date = position.get("claim_date") or position["start_time"][:10] + " 00:00:00"
    return {
        "primary_claim": {
            "id": "provisional_1", "claim_categories_id": 1, "claim_types_id": int(claim_type_id),
            "staffing_users_id": str(uid), "events_id": str(event["id"]),
            "events_staffing_days_positions_id": str(position["id"]), "claim_date": claim_date,
            "claim_start_time": start.strftime("%I:%M %p").lower(), "claim_end_time": end.strftime("%I:%M %p").lower(),
            "primary_claim": "t", "hours": hours,
        },
        "position": {
            "id": str(position["id"]), "real_events_id": position.get("real_events_id") or str(event["id"]),
            "actual_hours": hours, "shift_segments_id": None,
            "meal_break_acknowledgement": None, "rest_break_acknowledgement": None,
            "actual_start": start.strftime(TS), "actual_end": end.strftime(TS),
            "meal_break_start": None, "meal_break_end": None, "rest_break_start": None, "rest_break_end": None,
        },
    }
