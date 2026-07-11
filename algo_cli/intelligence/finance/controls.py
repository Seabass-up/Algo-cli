"""B87. COSO Risk-Control Matrix Compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .common import RiskLevel, Severity, SourceRef, stable_exception_id
from .exceptions import make_exception


@dataclass(frozen=True)
class FinanceProcess:
    id: str
    name: str
    owner: str | None = None
    assertions: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Risk:
    id: str
    process_id: str
    description: str
    level: RiskLevel = RiskLevel.MEDIUM
    assertion: str | None = None


@dataclass(frozen=True)
class Control:
    id: str
    process_id: str
    description: str
    covers_risk_ids: set[str] = field(default_factory=set)
    owner: str | None = None
    control_type: str = "detective"  # preventive, detective, corrective
    frequency: str | None = None
    evidence_required: str | None = None
    automated: bool = False
    key_control: bool = False
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass(frozen=True)
class RCMRow:
    process: FinanceProcess
    risk: Risk
    controls: list[Control]
    residual_level: RiskLevel
    assertions: set[str]


@dataclass
class RiskControlMatrix:
    rows: list[RCMRow]
    control_count: int
    risk_count: int

    @property
    def coverage_ratio(self) -> Decimal:
        if not self.risk_count:
            return Decimal("1")
        covered = sum(1 for row in self.rows if row.controls)
        return Decimal(covered) / Decimal(self.risk_count)


@dataclass(frozen=True)
class ControlGap:
    id: str
    risk_id: str | None
    control_id: str | None
    severity: Severity
    message: str
    source_refs: list[SourceRef] = field(default_factory=list)


class RiskControlMatrixCompiler:
    """Compile process/risk/control metadata into a controller RCM."""

    def compile(
        self,
        processes: Iterable[FinanceProcess],
        risks: Iterable[Risk],
        controls: Iterable[Control],
    ) -> RiskControlMatrix:
        process_by_id = {process.id: process for process in processes}
        controls_by_risk: dict[str, list[Control]] = {}
        control_rows = list(controls)
        for control in control_rows:
            for risk_id in control.covers_risk_ids:
                controls_by_risk.setdefault(risk_id, []).append(control)

        rows: list[RCMRow] = []
        risk_rows = list(risks)
        for risk in risk_rows:
            process = process_by_id.get(risk.process_id) or FinanceProcess(id=risk.process_id, name=risk.process_id)
            covered_controls = sorted(controls_by_risk.get(risk.id, []), key=lambda c: c.id)
            residual = _residual_level(risk.level, covered_controls)
            assertions = set(process.assertions)
            if risk.assertion:
                assertions.add(risk.assertion)
            rows.append(RCMRow(
                process=process,
                risk=risk,
                controls=covered_controls,
                residual_level=residual,
                assertions=assertions,
            ))
        return RiskControlMatrix(rows=rows, control_count=len(control_rows), risk_count=len(risk_rows))

    def find_gaps(self, matrix: RiskControlMatrix) -> list[ControlGap]:
        gaps: list[ControlGap] = []
        for row in matrix.rows:
            if not row.controls:
                gaps.append(ControlGap(
                    id=stable_exception_id("B87", [row.risk.id, "no-control"]),
                    risk_id=row.risk.id,
                    control_id=None,
                    severity=Severity.HIGH if row.risk.level >= RiskLevel.HIGH else Severity.MEDIUM,
                    message=f"Risk {row.risk.id} has no mapped control",
                ))
                continue
            if not any(control.control_type.lower() == "preventive" for control in row.controls):
                gaps.append(ControlGap(
                    id=stable_exception_id("B87", [row.risk.id, "no-preventive"]),
                    risk_id=row.risk.id,
                    control_id=None,
                    severity=Severity.LOW,
                    message=f"Risk {row.risk.id} has no preventive control",
                ))
            for control in row.controls:
                if not control.owner:
                    gaps.append(ControlGap(
                        id=stable_exception_id("B87", [row.risk.id, control.id, "owner"]),
                        risk_id=row.risk.id,
                        control_id=control.id,
                        severity=Severity.MEDIUM,
                        message=f"Control {control.id} lacks an owner",
                        source_refs=list(control.source_refs),
                    ))
                if control.key_control and not control.evidence_required:
                    gaps.append(ControlGap(
                        id=stable_exception_id("B87", [row.risk.id, control.id, "evidence"]),
                        risk_id=row.risk.id,
                        control_id=control.id,
                        severity=Severity.HIGH,
                        message=f"Key control {control.id} lacks evidence requirement",
                        source_refs=list(control.source_refs),
                    ))
        return gaps

    def exceptions_for_gaps(self, gaps: Iterable[ControlGap]):
        return [
            make_exception(
                "B87",
                gap.message,
                severity=gap.severity,
                tags={"control-gap", f"risk:{gap.risk_id}"} if gap.risk_id else {"control-gap"},
                source_refs=gap.source_refs,
            )
            for gap in gaps
        ]


def _residual_level(level: RiskLevel, controls: list[Control]) -> RiskLevel:
    if not controls:
        return level
    reduction = 1 if any(control.key_control for control in controls) else 0
    if any(control.automated for control in controls):
        reduction += 1
    return RiskLevel(max(int(RiskLevel.LOW), int(level) - reduction))
