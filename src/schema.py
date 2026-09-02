from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from datetime import datetime, timezone


@dataclass
class UnifiedRecord:
    id: str                      
    source: str                 
    name: str                   
    contact_info: dict           
    context: dict               
    priority_signal: str         
    raw_payload: dict           
    ingested_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    is_valid: bool = True        # False if the record failed validation
    validation_errors: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_id(source_prefix: str, natural_key: str) -> str:
    return f"{source_prefix}-{natural_key}"
