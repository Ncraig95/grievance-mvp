from __future__ import annotations

from datetime import date, datetime, timedelta
import json


_CONTRACT_DEADLINE_DAYS = {
    "core southeast": 60,
    "wire tech": 60,
    "mobility": 45,
    "city of jacksonville": 10,
    "construction": 60,
    "contruction": 60,
}


def _canonical_contract_key(contract: str | None) -> str:
    return " ".join(str(contract or "").strip().lower().split())


def deadline_days_for_contract(contract: str | None) -> int | None:
    key = _canonical_contract_key(contract)
    if not key:
        return None
    if key in _CONTRACT_DEADLINE_DAYS:
        return _CONTRACT_DEADLINE_DAYS[key]
    if key == "core southest":
        return 60
    return None


def parse_incident_date(value: str | None) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None

    date_part = text.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_part, fmt).date()
        except ValueError:
            continue
    return None


def resolve_contract_and_incident_date(intake_payload_json: str | None) -> tuple[str | None, date | None]:
    if not intake_payload_json:
        return None, None
    try:
        payload = json.loads(intake_payload_json)
    except Exception:
        return None, None
    if not isinstance(payload, dict):
        return None, None

    contract = None
    for key in ("contract", "contract_type", "contractType"):
        val = payload.get(key)
        if val and str(val).strip():
            contract = str(val).strip()
            break

    incident = None
    for key in ("incident_date", "incidentDate", "date_of_incident", "incident"):
        val = payload.get(key)
        parsed = parse_incident_date(str(val) if val is not None else None)
        if parsed:
            incident = parsed
            break

    return contract, incident


def calculate_deadline(incident: date | None, deadline_days: int | None) -> date | None:
    if incident is None or deadline_days is None:
        return None
    return incident + timedelta(days=deadline_days)
