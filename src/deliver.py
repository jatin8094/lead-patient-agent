from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from schema import UnifiedRecord
from agent import AgentDecision

logger = logging.getLogger("deliver")

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
CMS_LOG = OUTPUT_DIR / "cms_log.jsonl"
DEAD_LETTER = OUTPUT_DIR / "dead_letter.jsonl"


def build_payload(record: UnifiedRecord, decision: AgentDecision) -> dict:
    """The schema/contract every downstream record must respect."""
    return {
        "external_id": record.id,
        "source_system": record.source,
        "contact": {
            "name": record.name,
            "email": record.contact_info.get("email"),
            "phone": record.contact_info.get("phone"),
            "preferred_channel": record.contact_info.get("preferred_channel"),
        },
        "classification": decision.classification,
        "recommended_action": decision.action,
        "escalate": decision.escalate,
        "generated_message": decision.message,
        "agent_rationale": decision.rationale,
        "record_valid": record.is_valid,
        "validation_errors": record.validation_errors,
        "pushed_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": "1.0",
    }


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _push_webhook(payload: dict, url: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            wait = BACKOFF_BASE_SECONDS * attempt
            logger.warning("Webhook push failed for %s (attempt %d/%d): %s. Retrying in %.1fs",
                           payload["external_id"], attempt, MAX_RETRIES, e, wait)
            time.sleep(wait)
    return False


def _push_local(payload: dict, url: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=5)
            resp.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.warning("Local CMS push failed for %s (attempt %d/%d): %s",
                           payload["external_id"], attempt, MAX_RETRIES, e)
            time.sleep(0.5 * attempt)
    return False


def deliver(records: list[UnifiedRecord], decisions: list[AgentDecision]) -> dict:
    target = os.environ.get("DELIVERY_TARGET", "local").lower()
    decision_by_id = {d.record_id: d for d in decisions}

    stats = {"delivered": 0, "dead_lettered": 0, "target": target}

    for record in records:
        decision = decision_by_id.get(record.id)
        if decision is None:
            continue
        payload = build_payload(record, decision)

        delivered = False
        if target == "webhook":
            webhook_url = os.environ.get("WEBHOOK_URL")
            if webhook_url:
                delivered = _push_webhook(payload, webhook_url)
            else:
                logger.error("DELIVERY_TARGET=webhook but WEBHOOK_URL is not set.")
        elif target == "local":
            local_url = os.environ.get("LOCAL_CMS_URL", "http://localhost:8000/cms/records")
            delivered = _push_local(payload, local_url)

        _append_jsonl(CMS_LOG, payload)

        if delivered:
            stats["delivered"] += 1
        else:
            if target != "local" or os.environ.get("LOCAL_CMS_URL"):
                logger.error("Giving up on live delivery for %s after retries -- dead-lettering",
                            record.id)
                _append_jsonl(DEAD_LETTER, payload)
                stats["dead_lettered"] += 1
            else:
                stats["delivered"] += 1

    logger.info("Delivery complete: %s", stats)
    return stats
