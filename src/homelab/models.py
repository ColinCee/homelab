"""Pydantic models for health checks and security audits."""

from enum import StrEnum

from pydantic import BaseModel


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class CheckResult(BaseModel):
    """Result of a single check (health or security)."""

    name: str
    status: Status
    message: str
    details: str | None = None


class AuditReport(BaseModel):
    """Collection of check results with summary."""

    title: str
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(c.status != Status.FAIL for c in self.checks)

    @property
    def summary(self) -> str:
        counts = {s: 0 for s in Status}
        for c in self.checks:
            counts[c.status] += 1
        parts = []
        if counts[Status.PASS]:
            parts.append(f"{counts[Status.PASS]} passed")
        if counts[Status.FAIL]:
            parts.append(f"{counts[Status.FAIL]} failed")
        if counts[Status.WARN]:
            parts.append(f"{counts[Status.WARN]} warnings")
        if counts[Status.SKIP]:
            parts.append(f"{counts[Status.SKIP]} skipped")
        return ", ".join(parts)

    def print_report(self) -> None:
        icons = {
            Status.PASS: "✅",
            Status.FAIL: "❌",
            Status.WARN: "⚠️ ",
            Status.SKIP: "⏭️ ",
        }
        print(f"\n{'=' * 50}")
        print(f"  {self.title}")
        print(f"{'=' * 50}\n")
        for check in self.checks:
            print(f"  {icons[check.status]} {check.name}: {check.message}")
            if check.details:
                for line in check.details.strip().splitlines():
                    print(f"      {line}")
        print(f"\n  Summary: {self.summary}")
        status = "PASS ✅" if self.passed else "FAIL ❌"
        print(f"  Overall: {status}\n")
