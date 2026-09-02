# Write-up

## Architecture decisions and why

**Three clean stage boundaries, one shared schema.** Ingest, Agent, and
Deliver are separate modules that only talk to each other through
`UnifiedRecord` (Stage 1→2) and `AgentDecision` (Stage 2→3). Source A
(CSV) and Source B (FHIR) are structurally very different — flat rows vs.
nested resource bundles — but the agent and delivery code never see that
difference. This is the main design bet of the assignment: normalize
early, reason generically, specialize only at the edges (two ingest
modules, two system prompts).

**The agent is a decision function with structured I/O, not a chatbot
loop.** For this problem, "agentic" doesn't need multi-turn tool-calling
or a planner — it needs per-record judgment (classify → decide → act →
explain) with retries and a safe fallback path. I built that as one
retryable LLM call per record with a strict JSON contract
(`classification`, `action`, `message`, `rationale`), rather than
chaining several LLM calls, because a single well-scoped call is more
reliable, testable, and — at scale — much cheaper than a multi-step agent
loop. The trade-off: it can't ask itself follow-up questions or fetch
more context mid-decision. Given the record already contains everything
needed to decide (contact info, role/company or conditions/appointments),
that trade-off is fine here.

**Pluggable LLM provider, offline-by-default.** `llm_client.py` defines
one interface (`complete(system, user)`) with four implementations —
Groq, Gemini, Ollama, and a deterministic offline "mock" that's still
record-aware (it parses the same JSON prompt a real LLM would get and
derives a plausible response from it). Default provider is `mock` so the
whole pipeline runs end-to-end with zero API keys and zero network
dependency for grading — swapping in a real provider is one env var.

**Validation happens once, at ingest, and travels with the record.**
Rather than re-checking "is this record OK?" at every stage, each
`UnifiedRecord` carries `is_valid` / `validation_errors` from Stage 1
onward. The agent stage checks that flag and routes invalid records to
`needs_review` without ever calling the LLM on bad data (no wasted
tokens on unusable records); delivery still pushes them downstream
(with the errors attached) so nothing silently vanishes.

**Contract-first delivery.** `deliver.build_payload()` is the single
place that defines what a downstream CRM/CMS receives. Every record —
valid or not, lead or patient — goes through it, so the shape is
guaranteed even when the content underneath (empty message, escalate
flag, validation errors) varies.

## How I'd handle this at 10,000 records/day

- **Batch and parallelize Stage 2.** At 10k/day the LLM calls are the
  bottleneck. I'd move from sequential per-record calls to a bounded
  worker pool (e.g. `asyncio` + a semaphore, or a queue consumer with
  N workers) and batch where the provider supports it. Groq/Gemini free
  tiers are rate-limited, so at real volume this also means either a paid
  tier or a self-hosted model (Ollama/vLLM) to avoid being rate-limited
  mid-run.
- **Make ingest incremental, not full-reload.** Pull only new/changed
  records (FHIR `_lastUpdated` search param; a CSV delta or a proper
  source-system webhook instead of a flat file) so a daily run doesn't
  reprocess everything.
- **Move the JSONL audit log to a real store.** `cms_log.jsonl` /
  `dead_letter.jsonl` work for a take-home; at scale that's a database
  table (with `record_id`, status, retry count, timestamps) and the
  dead-letter path becomes a proper retry queue (SQS/Cloud Tasks/etc.)
  with alerting, not a file someone has to remember to check.
- **Idempotency.** Each record has a stable `id`; at scale I'd add a
  dedupe/upsert check before Stage 2 so a re-run or a retried delivery
  doesn't re-generate or double-push a message for the same record
  (note L010 in the sample CSV flags this exact scenario).
- **Observability.** Swap `logging` + a flat file for structured logs
  shipped somewhere queryable, plus basic metrics (records/sec,
  classification distribution, LLM error rate, delivery success rate) —
  the rationale field is gold for spot-checking agent quality over time,
  but at 10k/day nobody's reading every rationale, so I'd sample and
  alert on classification/action distributions drifting.

## If this were touching real PHI

This assignment is explicit that no real PHI is involved, and the code
reflects that: `data/fhir_fixture.json` and the sandbox are synthetic,
and there's no logic here designed to handle real patient data. If it
were:

- **De-identify before it ever reaches the LLM**, or use a
  BAA-covered/on-prem model — sending PHI to a third-party API (Groq,
  Gemini's free tier) without a signed Business Associate Agreement is
  a HIPAA violation regardless of intent. That likely means self-hosted
  inference (Ollama/vLLM on infrastructure you control) or a HIPAA-
  eligible hosted offering, not the free-tier providers this assignment
  points to.
- **Encrypt at rest and in transit everywhere** — the CSV/JSONL files
  this repo writes in plaintext (`cms_log.jsonl`, `pipeline.log`) would
  need to be encrypted, access-controlled, and access-logged instead.
- **Minimum necessary + audit trail.** Only pull the FHIR fields the
  agent actually needs (this repo already does this loosely via the
  unified schema, but I'd formalize it), and log *who/what* accessed a
  record, not just *that* the pipeline processed it.
- **No PHI in logs.** Right now `logger.info` lines include names and
  condition text for debugging convenience — with real data those log
  lines themselves become PHI and need the same protections as the
  primary data store, or should be redacted/tokenized before logging.
- **Retention and right-to-delete.** A dead-letter file that keeps a
  failed patient record around indefinitely is a real problem under
  HIPAA (and GDPR-style regimes) — retention policies and deletion
  workflows would need to be first-class, not an afterthought.
- **The generated patient message itself is a compliance surface.** The
  system prompt already tells the agent not to give medical advice or
  state a diagnosis in the outreach message — with real PHI, that
  constraint needs to be enforced (a validation pass on the LLM output,
  not just a prompt instruction), since prompts can be ignored by the
  model.
