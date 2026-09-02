from __future__ import annotations
import csv
import logging
import re
from pathlib import Path

from schema import UnifiedRecord, new_id

logger = logging.getLogger("ingest.leads")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HOT_KEYWORDS = ("pricing", "demo", "booked", "replied", "downloaded")
COLD_SIGNALS = ("no activity", "bounced", "unverified")


def _guess_priority_signal(notes: str, last_activity: str) -> str:
    text = f"{notes} {last_activity}".lower()
    if any(k in text for k in HOT_KEYWORDS):
        return "high_intent"
    if any(k in text for k in COLD_SIGNALS):
        return "cold"
    return "warm"


def _validate_row(row: dict) -> list[str]:
    errors = []
    if not row.get("full_name", "").strip():
        errors.append("missing full_name")
    email = row.get("email", "").strip()
    phone = row.get("phone", "").strip()
    if not email and not phone:
        errors.append("no email or phone — no contactable channel")
    if email and not EMAIL_RE.match(email):
        errors.append(f"malformed email: '{email}'")
    return errors


def ingest_leads(csv_path: str | Path) -> list[UnifiedRecord]:
    csv_path = Path(csv_path)
    records: list[UnifiedRecord] = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            errors = _validate_row(row)
            is_valid = len(errors) == 0

            record = UnifiedRecord(
                id=new_id("lead", row.get("lead_id", "UNKNOWN")),
                source="linkedin_lead",
                name=row.get("full_name", "").strip(),
                contact_info={
                    "email": row.get("email", "").strip() or None,
                    "phone": row.get("phone", "").strip() or None,
                    "preferred_channel": "email" if row.get("email") else "phone",
                },
                context={
                    "job_title": row.get("job_title", "").strip(),
                    "company": row.get("company", "").strip(),
                    "company_size": row.get("company_size", "").strip(),
                    "industry": row.get("industry", "").strip(),
                    "location": row.get("location", "").strip(),
                    "last_activity": row.get("last_activity", "").strip(),
                    "connection_degree": row.get("connection_degree", "").strip(),
                    "notes": row.get("notes", "").strip(),
                },
                priority_signal=_guess_priority_signal(
                    row.get("notes", ""), row.get("last_activity", "")
                ),
                raw_payload=dict(row),
                is_valid=is_valid,
                validation_errors=errors,
            )

            if not is_valid:
                logger.warning(
                    "Invalid lead record %s: %s", record.id, "; ".join(errors)
                )
            records.append(record)

    logger.info("Ingested %d lead records (%d invalid)",
                len(records), sum(1 for r in records if not r.is_valid))
    return records
