#!/usr/bin/env python3
"""Unified diagnostic status enum for all geomask checks."""
from enum import Enum
import json
import sys
from typing import Any, Dict, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


class CheckStatus(Enum):
    """Result of a single diagnostic check."""
    OK = "ok"              # проверка прошла, нарушений нет
    FAIL = "fail"          # нарушение обнаружено
    UNKNOWN = "unknown"    # не удалось определить (timeout, 401, parse error, empty)
    SKIPPED = "skipped"    # проверка не применима (нет конфига, отключено)

    def __lt__(self, other):
        if not isinstance(other, CheckStatus):
            return NotImplemented
        return CheckStatus._ORDER[self] < CheckStatus._ORDER[other]

    def __gt__(self, other):
        if not isinstance(other, CheckStatus):
            return NotImplemented
        return CheckStatus._ORDER[self] > CheckStatus._ORDER[other]

    def __le__(self, other):
        if not isinstance(other, CheckStatus):
            return NotImplemented
        return CheckStatus._ORDER[self] <= CheckStatus._ORDER[other]

    def __ge__(self, other):
        if not isinstance(other, CheckStatus):
            return NotImplemented
        return CheckStatus._ORDER[self] >= CheckStatus._ORDER[other]

    def is_negative(self) -> bool:
        """True if this status indicates a problem or uncertainty."""
        return self in (CheckStatus.FAIL, CheckStatus.UNKNOWN)

    def is_positive(self) -> bool:
        """True only for definitive OK."""
        return self == CheckStatus.OK

    def __str__(self):
        return self.value


# Set ordering after enum members are created
CheckStatus._ORDER = {
    CheckStatus.OK: 0,
    CheckStatus.SKIPPED: 1,
    CheckStatus.UNKNOWN: 2,
    CheckStatus.FAIL: 3,
}


@dataclass
class CheckResult:
    """Structured result of a diagnostic check."""
    name: str                    # human-readable check name
    status: CheckStatus
    message: str = ""            # details for operator
    evidence: Dict[str, Any] = None  # raw data: http_code, loc, latency_ms, etc.
    ts: str = ""                 # RFC3339 timestamp
    duration_ms: int = 0

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = {}
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    def to_json(self) -> str:
        return json.dumps({
            'name': self.name,
            'status': self.status.value,
            'message': self.message,
            'evidence': self.evidence,
            'ts': self.ts,
            'duration_ms': self.duration_ms
        }, ensure_ascii=False)

    @classmethod
    def ok(cls, name: str, message: str = "", **evidence) -> 'CheckResult':
        return cls(name=name, status=CheckStatus.OK, message=message, evidence=evidence)

    @classmethod
    def fail(cls, name: str, message: str = "", **evidence) -> 'CheckResult':
        return cls(name=name, status=CheckStatus.FAIL, message=message, evidence=evidence)

    @classmethod
    def unknown(cls, name: str, message: str = "", **evidence) -> 'CheckResult':
        return cls(name=name, status=CheckStatus.UNKNOWN, message=message, evidence=evidence)

    @classmethod
    def skipped(cls, name: str, message: str = "") -> 'CheckResult':
        return cls(name=name, status=CheckStatus.SKIPPED, message=message)


@dataclass
class DiagnosticSummary:
    """Aggregated result of multiple checks."""
    checks: list[CheckResult]
    overall: CheckStatus = CheckStatus.UNKNOWN
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        # Overall: FAIL if any FAIL, UNKNOWN if any UNKNOWN, OK only if all OK
        has_fail = any(c.status == CheckStatus.FAIL for c in self.checks)
        has_unknown = any(c.status == CheckStatus.UNKNOWN for c in self.checks)
        if has_fail:
            self.overall = CheckStatus.FAIL
        elif has_unknown:
            self.overall = CheckStatus.UNKNOWN
        else:
            self.overall = CheckStatus.OK

    def to_json(self) -> str:
        return json.dumps({
            'overall': self.overall.value,
            'ts': self.ts,
            'checks': [
                {
                    'name': c.name,
                    'status': c.status.value,
                    'message': c.message,
                    'evidence': c.evidence,
                    'ts': c.ts,
                    'duration_ms': c.duration_ms
                }
                for c in self.checks
            ]
        }, ensure_ascii=False, indent=2)


def main():
    """CLI for testing: python diag_status.py <check_name> <status> [message]"""
    if len(sys.argv) < 3:
        print("Usage: diag_status.py <name> <ok|fail|unknown|skipped> [message]")
        sys.exit(1)
    name = sys.argv[1]
    status = CheckStatus(sys.argv[2].lower())
    message = sys.argv[3] if len(sys.argv) > 3 else ""
    result = CheckResult(name=name, status=status, message=message)
    print(result.to_json())


if __name__ == '__main__':
    main()