# Lead & Patient Intake Agent

A small agentic pipeline: two structurally different data sources
(LinkedIn-style leads CSV, FHIR patient records) get normalized into one
schema, reasoned over by an LLM agent (classify → decide → generate
message → log rationale), and pushed to a mock downstream CRM/CMS with a
fixed payload contract.

```
Source A: mock_leads.csv  
 ingest (normalize) ──> agent (reason) ──> deliver (push + audit)
Source B: FHIR R4 sandbox
```

## Quick start (zero config)

```bash
pip install -r requirements.txt
python src/main.py
```

That's it. With no env vars set, this:
- reads `data/mock_leads.csv` (12 sample leads, including one
  deliberately malformed row for the failure path)
- pulls 15 Patient resources from the live SMART Health IT sandbox
  (`https://r4.smarthealthit.org`), retrying on timeout/5xx, falling
  back to the bundled `data/fhir_fixture.json` if the sandbox is
  unreachable
- runs every record through the agent using a built-in, offline
  "mock" LLM client (deterministic, record-aware, zero API keys/network)
- pushes every processed record to `output/cms_log.jsonl` (and to a
  local mock CMS server if you run one — see below)
- writes `output/sample_output.json`: every record's input, agent
  reasoning, and final CMS payload in one file
- writes `logs/pipeline.log` with full run logs (what the agent decided
  and why, per record)

## Using a real LLM (one env var)

```bash
# Groq -- free tier, no credit card: https://console.groq.com/keys
LLM_PROVIDER=groq GROQ_API_KEY=your_key python src/main.py

# Gemini via AI Studio -- free tier, no credit card: https://aistudio.google.com/apikey
LLM_PROVIDER=gemini GEMINI_API_KEY=your_key python src/main.py

# Ollama -- fully local, no key (ollama pull llama3.1 first)
LLM_PROVIDER=ollama OLLAMA_MODEL=llama3.1 python src/main.py
```

See `.env.example` for the full list of options. Nothing else in the
code changes — `llm_client.get_client()` is the only place that reads
`LLM_PROVIDER`.

## Using a real delivery target

By default (`DELIVERY_TARGET=local`) the pipeline tries to POST each
record to a local mock CMS server. To see that live:

```bash
# terminal 1
python src/mock_cms_server.py

# terminal 2
python src/main.py
curl http://localhost:8000/cms/records   # see everything it received
```

If that server isn't running, delivery pushes fail fast (with retries),
get logged as a clear WARNING, and the pipeline still completes — every
payload is always mirrored to `output/cms_log.jsonl` regardless, which
is what `sample_output.json` is built from either way.

Or point at a real free webhook endpoint instead:

```bash
DELIVERY_TARGET=webhook WEBHOOK_URL=https://webhook.site/your-id python src/main.py
```

## Project layout

```
data/
  mock_leads.csv         Source A -- 12 sample leads (1 deliberately malformed)
  fhir_fixture.json      offline fallback for Source B (see ingest_fhir.py)
src/
  schema.py              the one internal record shape both sources map to
  ingest_leads.py         Stage 1a
  ingest_fhir.py          Stage 1b (live fetch + retry/backoff + fallback)
  llm_client.py           Stage 2 LLM abstraction (Groq/Gemini/Ollama/mock)
  agent.py                Stage 2 reasoning loop (classify/decide/generate/log)
  deliver.py              Stage 3 (payload contract, retries, dead-letter)
  mock_cms_server.py      tiny local Flask endpoint standing in for a real CRM
  main.py                 orchestrator + sample_output.json writer
output/                   generated: cms_log.jsonl, dead_letter.jsonl, sample_output.json
logs/                     generated: pipeline.log
WRITEUP.md               architecture rationale, scaling to 10k/day, PHI considerations
```

## Failure handling (what the assignment asks to see)

- **FHIR sandbox unreachable/slow:** `ingest_fhir.py` retries with
  exponential backoff, then falls back to `data/fhir_fixture.json` and
  logs a WARNING rather than crashing. (This path is easy to trigger
  on purpose — see "Forcing the fixture fallback" below.)
- **Malformed source records:** `lead-L011` in the CSV has no name and
  an invalid email on purpose; `smart-1011` in the fixture has no name.
  Both are flagged `is_valid=False` at ingest, skipped for LLM
  generation (no wasted call on unusable data), and routed to a
  `needs_review` action instead of silently vanishing or crashing the
  batch.
- **Unparseable/failed LLM output:** `agent.py` retries the call once,
  then falls back to a safe `needs_review` decision rather than
  propagating a bad record downstream.
- **Delivery failures:** `deliver.py` retries each push, and anything
  that still fails goes to `output/dead_letter.jsonl` instead of being
  dropped.

### Forcing the fixture fallback on purpose

```bash
FHIR_PATIENT_COUNT=15 python -c "
import os; os.environ['LLM_PROVIDER']='mock'
import sys; sys.path.insert(0,'src')
from ingest_fhir import ingest_fhir
# point at a bad host to force the retry/backoff/fallback path
import ingest_fhir as m; m.BASE_URL = 'https://this-host-does-not-exist.invalid'
records = ingest_fhir(count=5)
print(f'Got {len(records)} records via fallback fixture')
"
```

## Notes on this submission

- `mock_leads.csv` was not included as an attachment when this
  assignment doc was received, so a representative sample (matching the
  described LinkedIn Sales Navigator export shape) was authored for this
  repo — see `data/mock_leads.csv`.
- The build/test environment this repo was assembled in had outbound
  network access restricted to package registries (pip/npm/GitHub), so
  the live FHIR sandbox call in `ingest_fhir.py` could not be exercised
  against the real network from that environment — it was validated via
  the retry → fallback → fixture path instead (which is real code, not a
  stub). It will hit `https://r4.smarthealthit.org` for real the moment
  it's run somewhere with normal internet access — no code changes
  needed.
