from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field

from schema import UnifiedRecord
from llm_client import get_client, complete_with_retry

logger = logging.getLogger("agent")

SYSTEM_PROMPT_LEAD = """You are a sales development agent. Given a lead record, decide:
- classification: one of "hot", "warm", "cold"
- action: one of "email_personal_outreach", "email_low_touch_nurture", "escalate_to_ae", "needs_review"
- message: a short (2-4 sentence), personalized outreach message using the lead's actual name, role, and company
- rationale: one or two sentences on WHY you made this call, referencing specific fields from the record

Respond ONLY with a JSON object with keys: classification, action, message, rationale."""

SYSTEM_PROMPT_PATIENT = """You are a patient outreach coordination agent (NOT a clinician -- you do not
give medical advice). Given a normalized patient record, decide:
- classification: one of "urgent", "monitor", "routine"
- action: one of "flag_for_care_team_call", "send_routine_reminder", "schedule_followup", "needs_review"
- message: a short (2-4 sentence), plain, non-alarming patient-facing check-in/reminder message.
  Never state a diagnosis or give clinical instructions -- only ask them to contact the office.
- rationale: one or two sentences on WHY you made this call, referencing specific fields from the record
  (e.g. conditions on file, missed appointments)

Respond ONLY with a JSON object with keys: classification, action, message, rationale."""


@dataclass
class AgentDecision:
    record_id: str
    source: str
    classification: str
    action: str
    message: str
    rationale: str
    escalate: bool = False

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "source": self.source,
            "classification": self.classification,
            "action": self.action,
            "message": self.message,
            "rationale": self.rationale,
            "escalate": self.escalate,
        }


def _safe_default_decision(record: UnifiedRecord, reason: str) -> AgentDecision:
    return AgentDecision(
        record_id=record.id,
        source=record.source,
        classification="unclassified",
        action="needs_review",
        message="",
        rationale=f"Routed to manual review without generating outreach: {reason}",
        escalate=True,
    )


def _build_user_prompt(record: UnifiedRecord) -> str:
   
    payload = {
        "id": record.id,
        "source": record.source,
        "name": record.name,
        "priority_signal": record.priority_signal,
        "context": record.context,
    }
    return f"Record:\n{json.dumps(payload, ensure_ascii=False)}"


def _parse_llm_response(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


def run_agent(records: list[UnifiedRecord]) -> list[AgentDecision]:
    client = get_client()
    decisions: list[AgentDecision] = []

    for record in records:
        if not record.is_valid:
            reason = "; ".join(record.validation_errors) or "failed Stage-1 validation"
            logger.warning("Skipping generation for %s (%s) -- flagged needs_review",
                           record.id, reason)
            decisions.append(_safe_default_decision(record, reason))
            continue

        system = SYSTEM_PROMPT_LEAD if record.source == "linkedin_lead" else SYSTEM_PROMPT_PATIENT
        user = _build_user_prompt(record)

        raw = complete_with_retry(client, system, user)
        parsed = _parse_llm_response(raw)

        if parsed is None or not all(k in parsed for k in
                                     ("classification", "action", "message", "rationale")):
            logger.warning("Unparseable/incomplete LLM output for %s -- using safe default",
                           record.id)
            decisions.append(_safe_default_decision(
                record, "LLM returned unparseable output"))
            continue

        escalate = parsed["action"] in ("escalate_to_ae", "flag_for_care_team_call")
        decision = AgentDecision(
            record_id=record.id,
            source=record.source,
            classification=parsed["classification"],
            action=parsed["action"],
            message=parsed["message"],
            rationale=parsed["rationale"],
            escalate=escalate,
        )
        logger.info("Agent decision for %s: %s / %s -- %s",
                    record.id, decision.classification, decision.action,
                    decision.rationale)
        decisions.append(decision)

    return decisions
