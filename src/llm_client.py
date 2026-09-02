from __future__ import annotations
import json
import logging
import os
import time

import requests

logger = logging.getLogger("llm")

DEFAULT_TIMEOUT = 20
MAX_RETRIES = 2


class LLMError(Exception):
    pass


class BaseLLMClient:
    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class GroqClient(BaseLLMClient):
    """Free tier: https://console.groq.com -- no credit card required."""

    def __init__(self, model: str = "llama-3.1-8b-instant"):
        self.api_key = os.environ.get("GROQ_API_KEY")
        if not self.api_key:
            raise LLMError("GROQ_API_KEY not set")
        self.model = model
        self.url = "https://api.groq.com/openai/v1/chat/completions"

    def complete(self, system: str, user: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                        {"role": "user", "content": user}],
            "temperature": 0.4,
        }
        resp = requests.post(self.url, headers=headers, json=payload,
                             timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class GeminiClient(BaseLLMClient):
    """Free tier via AI Studio: https://aistudio.google.com -- no credit card."""

    def __init__(self, model: str = "gemini-1.5-flash"):
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY not set")
        self.model = model
        self.url = (f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{model}:generateContent?key={self.api_key}")

    def complete(self, system: str, user: str) -> str:
        payload = {
            "contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
        }
        resp = requests.post(self.url, json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


class OllamaClient(BaseLLMClient):
    """Fully local, free, no key: `ollama pull llama3.1` then run this."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1")
        self.url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                        {"role": "user", "content": user}],
            "stream": False,
        }
        resp = requests.post(self.url, json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["message"]["content"]


class MockClient(BaseLLMClient):
    """
    Deterministic, offline, zero-dependency 'LLM' so the pipeline runs
    end-to-end without any API key. Produces plausible, template-driven
    output derived from the actual record fields it's given -- it reads
    the prompt content and extracts what it needs, so results still vary
    per-record rather than being static text.

    Swap LLM_PROVIDER to groq/gemini/ollama for real generations.
    """

    def complete(self, system: str, user: str) -> str:
       
        try:
            start = user.index("{")
            record = json.loads(user[start:])
        except Exception:
            record = {}

        name = record.get("name") or "there"
        source = record.get("source", "")
        signal = record.get("priority_signal", "routine")

        if source == "linkedin_lead":
            title = record.get("context", {}).get("job_title", "your role")
            company = record.get("context", {}).get("company", "your company")
            classification = "hot" if signal == "high_intent" else (
                "cold" if signal == "cold" else "warm")
            action = "email_personal_outreach" if classification != "cold" else "email_low_touch_nurture"
            message = (
                f"Hi {name.split()[0] if name != 'there' else 'there'}, noticed your "
                f"activity around {company} -- given your work as {title}, thought it's "
                f"worth a quick conversation about where we could help. Open to 15 minutes "
                f"this week?"
            )
            rationale = (
                f"Classified as {classification} based on priority_signal='{signal}' "
                f"derived from recent activity/notes. Chose {action} as the lowest-friction "
                f"channel given available contact info."
            )
        else:
            conditions = record.get("context", {}).get("conditions", [])
            classification = "urgent" if signal in ("urgent_care_gap", "missed_followup") else (
                "monitor" if signal == "chronic_monitoring" else "routine")
            action = "flag_for_care_team_call" if classification == "urgent" else "send_routine_reminder"
            cond_text = conditions[0] if conditions else "your recent visit"
            message = (
                f"Hello {name.split()[0] if name != 'there' else 'there'}, this is a check-in "
                f"regarding {cond_text}. Please call our office to schedule your next "
                f"appointment or let us know if you need anything before then."
            )
            rationale = (
                f"Classified as {classification} based on priority_signal='{signal}' "
                f"(conditions on file: {conditions or 'none'}). Chose {action} "
                f"to route this to the appropriate follow-up path."
            )

        return json.dumps({
            "classification": classification,
            "action": action,
            "message": message,
            "rationale": rationale,
        })


def get_client() -> BaseLLMClient:
    provider = os.environ.get("LLM_PROVIDER", "mock").lower()
    try:
        if provider == "groq":
            return GroqClient()
        if provider == "gemini":
            return GeminiClient()
        if provider == "ollama":
            return OllamaClient()
        if provider == "mock":
            return MockClient()
        logger.warning("Unknown LLM_PROVIDER '%s', falling back to mock.", provider)
        return MockClient()
    except LLMError as e:
        logger.warning("Could not init provider '%s' (%s) -- falling back to mock.",
                       provider, e)
        return MockClient()


def complete_with_retry(client: BaseLLMClient, system: str, user: str) -> str:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.complete(system, user)
        except Exception as e:  # noqa: BLE001 - genuinely want to catch any provider error
            last_err = e
            logger.warning("LLM call failed (attempt %d/%d): %s",
                           attempt, MAX_RETRIES, e)
            time.sleep(1.0 * attempt)
    logger.error("LLM call failed after %d attempts, falling back to mock. Last error: %s",
                MAX_RETRIES, last_err)
    return MockClient().complete(system, user)
