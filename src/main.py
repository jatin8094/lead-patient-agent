from __future__ import annotations
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ingest_leads import ingest_leads
from ingest_fhir import ingest_fhir
from agent import run_agent
from deliver import deliver, build_payload

LOG_DIR = Path(os.environ.get("OUTPUT_DIR", "output")) / ".."  # keep logs next to output
LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "pipeline.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


def main() -> None:
    leads_csv = os.environ.get("LEADS_CSV_PATH", "data/mock_leads.csv")
    fhir_count = int(os.environ.get("FHIR_PATIENT_COUNT", "15"))
    output_dir = Path(os.environ.get("OUTPUT_DIR", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=== Stage 1: Ingest ===")
    lead_records = ingest_leads(leads_csv)
    patient_records = ingest_fhir(count=fhir_count)
    all_records = lead_records + patient_records
    logger.info("Total unified records: %d (%d leads, %d patients)",
                len(all_records), len(lead_records), len(patient_records))

    logger.info("=== Stage 2: Agent (classify, decide, generate, log rationale) ===")
    decisions = run_agent(all_records)

    logger.info("=== Stage 3: Deliver ===")
    stats = deliver(all_records, decisions)

    
    decision_by_id = {d.record_id: d for d in decisions}
    sample_output = []
    for record in all_records:
        decision = decision_by_id[record.id]
        sample_output.append({
            "input_record": record.to_dict(),
            "agent_reasoning": decision.to_dict(),
            "cms_payload_sent": build_payload(record, decision),
        })

    sample_path = output_dir / "sample_output.json"
    with open(sample_path, "w", encoding="utf-8") as f:
        json.dump({
            "pipeline_run_summary": {
                "total_records": len(all_records),
                "leads": len(lead_records),
                "patients": len(patient_records),
                "invalid_records": sum(1 for r in all_records if not r.is_valid),
                "delivery_stats": stats,
            },
            "records": sample_output,
        }, f, indent=2, ensure_ascii=False)

    logger.info("Wrote sample output for %d records to %s", len(sample_output), sample_path)
    logger.info("Delivery stats: %s", stats)
    logger.info("Full pipeline log: %s", LOG_FILE)
    logger.info("CMS audit log: %s", output_dir / "cms_log.jsonl")


if __name__ == "__main__":
    main()
