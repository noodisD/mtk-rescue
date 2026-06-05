from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    check_id: str
    title: str
    severity: Severity
    summary: str
    evidence: str = ""
    suggested_recipe: str | None = None
