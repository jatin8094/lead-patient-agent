from __future__ import annotations
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

import requests

from schema import UnifiedRecord, new_id

logger = logging.getLogger("ingest.fhir")

BASE_URL = "https://r4.smarthealthit.org"
TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.5

URGENT_CONDITION_KEYWORDS = (
    "myocardial infarction", "heart failure", "stroke", "sepsis",
    "acute", "chronic kidney disease", "copd",
)


def _fetch_with_retry(url: str, params: dict) -> dict | None:
    """GET a FHIR endpoint with retry/backoff. Returns None on total failure
    so the caller can fall back, rather than raising and killing the run."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT_SECONDS,
                                 headers={"Accept": "application/fhir+json"})
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            wait = BACKOFF_BASE_SECONDS ** attempt
            logger.warning("FHIR fetch attempt %d/%d failed (%s). Retrying in %.1fs...",
                           attempt, MAX_RETRIES, e, wait)
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            logger.warning("FHIR fetch got HTTP error %s on attempt %d/%d",
                           e, attempt, MAX_RETRIES)
            time.sleep(BACKOFF_BASE_SECONDS ** attempt)
    logger.error("FHIR live endpoint unreachable after %d attempts.", MAX_RETRIES)
    return None


def _load_fixture(fixture_path: str | Path) -> dict:
    logger.warning("Falling back to offline FHIR fixture at %s", fixture_path)
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


def _fetch_bundle(count: int, fixture_path: str | Path) -> dict:
    live = _fetch_with_retry(f"{BASE_URL}/Patient", {"_count": count})
    if live is not None and live.get("entry"):
        logger.info("Fetched %d entries from live FHIR sandbox.", len(live["entry"]))
        return live
    return _load_fixture(fixture_path)


def _age_from_birthdate(birth_date: str | None) -> int | None:
    if not birth_date:
        return None
    try:
        bd = datetime.strptime(birth_date, "%Y-%m-%d").date()
        today = date.today()
        return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    except ValueError:
        return None


def _extract_name(patient: dict) -> str:
    names = patient.get("name") or []
    if not names:
        return ""
    n = names[0]
    given = " ".join(n.get("given", []) or [])
    family = n.get("family", "") or ""
    full = f"{given} {family}".strip()
    return full


def _extract_contact(patient: dict) -> dict:
    email, phone = None, None
    for t in patient.get("telecom", []) or []:
        if t.get("system") == "email" and not email:
            email = t.get("value")
        if t.get("system") == "phone" and not phone:
            phone = t.get("value")
    return {
        "email": email,
        "phone": phone,
        "preferred_channel": "email" if email else ("phone" if phone else None),
    }


def _priority_signal(conditions: list[dict], appointments: list[dict]) -> str:
    for c in conditions:
        text = (c.get("code", {}).get("text") or "").lower()
        if any(k in text for k in URGENT_CONDITION_KEYWORDS):
            return "urgent_care_gap"
    for a in appointments:
        if a.get("status") == "cancelled":
            return "missed_followup"
    if conditions:
        return "chronic_monitoring"
    return "routine"


def ingest_fhir(count: int = 15,
                fixture_path: str | Path = "data/fhir_fixture.json") -> list[UnifiedRecord]:
    bundle = _fetch_bundle(count, fixture_path)
    entries = [e["resource"] for e in bundle.get("entry", [])]

    patients = [r for r in entries if r.get("resourceType") == "Patient"]
    conditions = [r for r in entries if r.get("resourceType") == "Condition"]
    appointments = [r for r in entries if r.get("resourceType") == "Appointment"]

    records: list[UnifiedRecord] = []
    for patient in patients[:count]:
        pid = patient.get("id", "unknown")
        related_conditions = [
            c for c in conditions
            if c.get("subject", {}).get("reference", "").endswith(f"/{pid}")
        ]
        related_appts = [
            a for a in appointments
            if any(p.get("actor", {}).get("reference", "").endswith(f"/{pid}")
                   for p in a.get("participant", []))
        ]

        name = _extract_name(patient)
        contact = _extract_contact(patient)

        errors = []
        if not name:
            errors.append("missing patient name")
        if not contact["email"] and not contact["phone"]:
            errors.append("no email or phone on file — no contactable channel")

        record = UnifiedRecord(
            id=new_id("patient", pid),
            source="fhir_patient",
            name=name,
            contact_info=contact,
            context={
                "age": _age_from_birthdate(patient.get("birthDate")),
                "gender": patient.get("gender"),
                "city": (patient.get("address") or [{}])[0].get("city"),
                "conditions": [c.get("code", {}).get("text") for c in related_conditions],
                "upcoming_or_recent_appointments": [
                    {"status": a.get("status"), "description": a.get("description")}
                    for a in related_appts
                ],
            },
            priority_signal=_priority_signal(related_conditions, related_appts),
            raw_payload=patient,
            is_valid=len(errors) == 0,
            validation_errors=errors,
        )
        if not record.is_valid:
            logger.warning("Invalid patient record %s: %s",
                           record.id, "; ".join(errors))
        records.append(record)

    logger.info("Ingested %d patient records (%d invalid)",
                len(records), sum(1 for r in records if not r.is_valid))
    return records
