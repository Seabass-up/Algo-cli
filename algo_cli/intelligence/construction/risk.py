"""Construction insurance, safety, delay, default, onboarding, and red-flag patterns B114-B123."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from .common import (
    ConstructionIssue,
    Jurisdiction,
    ProjectType,
    ReviewGate,
    RiskLevel,
    SourceRef,
    contains_any,
    decimalize,
    make_issue,
    normalize_label,
    parse_date,
    source_from_mapping,
)


class IndemnityScope(str, Enum):
    OWN_NEGLIGENCE = "own_negligence"
    PARTIAL_NEGLIGENCE = "partial_negligence"
    SOLE_NEGLIGENCE_OF_INDEMNITEE = "sole_negligence_of_indemnitee"
    BROAD_UNKNOWN = "broad_unknown"
    NONE = "none"


@dataclass(frozen=True)
class InsuranceRequirement:
    coverage: str
    limit: Decimal | None = None
    required: bool = True
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class CoverageGap:
    coverage: str
    message: str
    risk_level: RiskLevel = RiskLevel.MEDIUM


@dataclass
class InsuranceRiskResult:
    pattern_id: str = "B114"
    indemnity_scope: IndemnityScope = IndemnityScope.NONE
    requirements: list[InsuranceRequirement] = field(default_factory=list)
    gaps: list[CoverageGap] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.INSURANCE_REVIEW


class IndemnityInsuranceRiskSplitter:
    """B114: split indemnity, insurance requirements, COIs, and endorsement gaps."""

    pattern_id = "B114"

    def analyze(
        self,
        indemnity_clause: str = "",
        insurance_requirements: Iterable[Mapping[str, Any]] = (),
        evidence: Mapping[str, Any] | None = None,
    ) -> InsuranceRiskResult:
        evidence = evidence or {}
        result = InsuranceRiskResult(indemnity_scope=self._scope(indemnity_clause))
        if result.indemnity_scope is IndemnityScope.SOLE_NEGLIGENCE_OF_INDEMNITEE:
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "broad indemnity attorney review",
                    "Indemnity appears to include the indemnitee's sole negligence; anti-indemnity/enforceability review required.",
                    risk_level=RiskLevel.CRITICAL,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    source_refs=(SourceRef("indemnity_clause", quote=indemnity_clause),),
                    tags={"indemnity", "anti_indemnity"},
                )
            )
        for raw in insurance_requirements:
            req = InsuranceRequirement(
                coverage=str(raw.get("coverage") or raw.get("type") or "coverage"),
                limit=decimalize(raw.get("limit")) if raw.get("limit") is not None else None,
                required=bool(raw.get("required", True)),
                source_refs=source_from_mapping(raw, "insurance_requirement"),
            )
            result.requirements.append(req)
            key = normalize_label(req.coverage)
            supplied = evidence.get(key) or evidence.get(req.coverage)
            if req.required and not supplied:
                gap = CoverageGap(req.coverage, f"Missing evidence for required {req.coverage} coverage.", RiskLevel.HIGH)
                result.gaps.append(gap)
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "missing insurance evidence",
                        gap.message,
                        risk_level=RiskLevel.HIGH,
                        review_gate=ReviewGate.INSURANCE_REVIEW,
                        source_refs=req.source_refs,
                        tags={"insurance", key},
                    )
                )
        if evidence.get("coi") and not evidence.get("additional_insured_endorsement"):
            result.gaps.append(CoverageGap("additional insured endorsement", "COI exists but actual additional-insured endorsement was not evidenced.", RiskLevel.HIGH))
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "additional insured evidence incomplete",
                    "COI alone is not proof of additional-insured status; request endorsement evidence.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.INSURANCE_REVIEW,
                    tags={"insurance", "additional_insured"},
                )
            )
        if evidence.get("delegated_design") and not (evidence.get("professional_liability") or evidence.get("professional_liability_policy")):
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "professional liability gap",
                    "Delegated design/design-assist evidence exists but professional liability coverage was not evidenced.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.INSURANCE_REVIEW,
                    tags={"insurance", "professional_liability", "delegated_design"},
                )
            )
        return result

    @staticmethod
    def _scope(text: str) -> IndemnityScope:
        lowered = text.lower()
        if not lowered.strip():
            return IndemnityScope.NONE
        if "sole negligence" in lowered or "regardless of whether caused" in lowered:
            return IndemnityScope.SOLE_NEGLIGENCE_OF_INDEMNITEE
        if "to the extent" in lowered and ("negligence" in lowered or "fault" in lowered):
            return IndemnityScope.PARTIAL_NEGLIGENCE
        if "own negligence" in lowered or "subcontractor's negligence" in lowered or "subcontractor negligence" in lowered:
            return IndemnityScope.OWN_NEGLIGENCE
        if "indemn" in lowered or "hold harmless" in lowered:
            return IndemnityScope.BROAD_UNKNOWN
        return IndemnityScope.NONE


class OSHAEmployerRole(str, Enum):
    CREATING = "creating"
    EXPOSING = "exposing"
    CORRECTING = "correcting"
    CONTROLLING = "controlling"
    NONE = "none"


@dataclass(frozen=True)
class SafetyEvent:
    id: str
    hazard: str
    roles: tuple[OSHAEmployerRole, ...]
    authority_to_correct: bool = False
    requested_correction: bool = False
    warned_employees: bool = False
    alternative_protection: bool = False
    inspections: bool = False
    correction_system: bool = False
    graduated_enforcement: bool = False
    isolated_hazard: bool = False
    notified_controller: bool = False
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class SafetyRoleResult:
    event_id: str
    roles: tuple[OSHAEmployerRole, ...]
    required_actions: tuple[str, ...]
    issues: tuple[ConstructionIssue, ...]


class OSHAMultiEmployerClassifier:
    """B115: classify creating/exposing/correcting/controlling-employer role and actions."""

    pattern_id = "B115"

    def classify(self, events: Iterable[SafetyEvent | Mapping[str, Any]]) -> list[SafetyRoleResult]:
        results: list[SafetyRoleResult] = []
        for raw in events:
            event = raw if isinstance(raw, SafetyEvent) else self._coerce_event(raw)
            actions: list[str] = []
            issues: list[ConstructionIssue] = []
            if OSHAEmployerRole.EXPOSING in event.roles and not event.authority_to_correct:
                actions.extend(["request correction", "warn employees", "use feasible alternative protection"])
                if not (event.requested_correction and event.warned_employees and event.alternative_protection):
                    issues.append(
                        make_issue(
                            self.pattern_id,
                            "exposing employer action gap",
                            "Exposing employer without authority to correct should request correction, warn crew, and use feasible alternative protection.",
                            risk_level=RiskLevel.HIGH,
                            review_gate=ReviewGate.SAFETY_REVIEW,
                            source_refs=event.source_refs,
                            tags={"osha", "exposing"},
                        )
                    )
            if OSHAEmployerRole.CONTROLLING in event.roles:
                actions.extend(["periodic inspections", "prompt correction system", "graduated enforcement"])
                if not (event.inspections and event.correction_system and event.graduated_enforcement):
                    issues.append(
                        make_issue(
                            self.pattern_id,
                            "controlling employer reasonable-care gap",
                            "Controlling-employer evidence lacks inspections, correction system, or graduated enforcement.",
                            risk_level=RiskLevel.HIGH,
                            review_gate=ReviewGate.SAFETY_REVIEW,
                            source_refs=event.source_refs,
                            tags={"osha", "controlling"},
                        )
                    )
            if OSHAEmployerRole.CREATING in event.roles and not (event.isolated_hazard and event.notified_controller):
                actions.extend(["do not create hazard", "isolate hazard", "notify controlling employer"])
                issues.append(
                    make_issue(
                        self.pattern_id,
                        "creating employer hazard response gap",
                        "Employer appears to have created a hazard without complete isolation/notification evidence.",
                        risk_level=RiskLevel.HIGH,
                        review_gate=ReviewGate.SAFETY_REVIEW,
                        source_refs=event.source_refs,
                        tags={"osha", "creating"},
                    )
                )
            results.append(SafetyRoleResult(event.id, event.roles, tuple(dict.fromkeys(actions)), tuple(issues)))
        return results

    @staticmethod
    def _coerce_event(raw: Mapping[str, Any]) -> SafetyEvent:
        roles: list[OSHAEmployerRole] = []
        for role in raw.get("roles", (raw.get("role"),)):
            if role in OSHAEmployerRole._value2member_map_:
                roles.append(OSHAEmployerRole(str(role)))
        return SafetyEvent(
            id=str(raw.get("id") or raw.get("hazard") or "safety_event"),
            hazard=str(raw.get("hazard") or ""),
            roles=tuple(roles) or (OSHAEmployerRole.NONE,),
            authority_to_correct=bool(raw.get("authority_to_correct")),
            requested_correction=bool(raw.get("requested_correction")),
            warned_employees=bool(raw.get("warned_employees")),
            alternative_protection=bool(raw.get("alternative_protection")),
            inspections=bool(raw.get("inspections")),
            correction_system=bool(raw.get("correction_system")),
            graduated_enforcement=bool(raw.get("graduated_enforcement")),
            isolated_hazard=bool(raw.get("isolated_hazard")),
            notified_controller=bool(raw.get("notified_controller")),
            source_refs=source_from_mapping(raw, "safety_event"),
        )


@dataclass(frozen=True)
class DelayEvent:
    id: str
    event_date: date
    days_impacted: int
    notice_sent: bool = False
    baseline_schedule: bool = False
    daily_reports: bool = False
    cost_records: bool = False
    directed_acceleration: bool = False
    source_refs: tuple[SourceRef, ...] = ()


@dataclass
class DelayAnalysis:
    pattern_id: str = "B116"
    issues: list[ConstructionIssue] = field(default_factory=list)
    recovery_limited: bool = False
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW


class DelayAccelerationAnalyzer:
    """B116: surface delay/acceleration/no-damages-for-delay proof gaps."""

    pattern_id = "B116"

    def analyze(self, clauses: str, events: Iterable[DelayEvent | Mapping[str, Any]]) -> DelayAnalysis:
        result = DelayAnalysis(recovery_limited=contains_any(clauses, ("no damages for delay", "sole remedy is time", "non-compensable delay")))
        if result.recovery_limited:
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "delay recovery limitation",
                    "Delay clause may limit monetary recovery; attorney review required.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    source_refs=(SourceRef("delay_clause", quote=clauses),),
                    tags={"delay", "recovery_limit"},
                )
            )
        for raw in events:
            event = raw if isinstance(raw, DelayEvent) else self._coerce_delay_event(raw)
            missing = []
            if not event.notice_sent:
                missing.append("notice")
            if not event.baseline_schedule:
                missing.append("baseline schedule")
            if not event.daily_reports:
                missing.append("daily reports")
            if not event.cost_records:
                missing.append("cost records")
            if missing:
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "delay proof incomplete",
                        f"Delay event {event.id} is missing: {', '.join(missing)}.",
                        risk_level=RiskLevel.HIGH if "notice" in missing or "baseline schedule" in missing else RiskLevel.MEDIUM,
                        review_gate=ReviewGate.BUSINESS_REVIEW,
                        source_refs=event.source_refs,
                        tags={"delay", "proof"},
                    )
                )
            if event.directed_acceleration and not event.notice_sent:
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "directed acceleration notice gap",
                        f"Directed acceleration event {event.id} needs notice/change documentation.",
                        risk_level=RiskLevel.HIGH,
                        review_gate=ReviewGate.ATTORNEY_REVIEW,
                        source_refs=event.source_refs,
                        tags={"delay", "acceleration", "notice"},
                    )
                )
        return result

    @staticmethod
    def _coerce_delay_event(raw: Mapping[str, Any]) -> DelayEvent:
        return DelayEvent(
            id=str(raw.get("id") or raw.get("description") or "delay_event"),
            event_date=parse_date(raw.get("event_date") or raw.get("date")) or date.today(),
            days_impacted=int(raw.get("days_impacted") or raw.get("days") or 0),
            notice_sent=bool(raw.get("notice_sent")),
            baseline_schedule=bool(raw.get("baseline_schedule")),
            daily_reports=bool(raw.get("daily_reports")),
            cost_records=bool(raw.get("cost_records")),
            directed_acceleration=bool(raw.get("directed_acceleration")),
            source_refs=source_from_mapping(raw, "delay_event"),
        )


class TerminationMode(str, Enum):
    CONVENIENCE = "convenience"
    CAUSE = "cause"
    NONPAYMENT = "nonpayment"
    SUSPENSION = "suspension"
    SAFETY = "safety"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CureGate:
    mode: TerminationMode
    cure_deadline: date | None
    urgent: bool
    issues: tuple[ConstructionIssue, ...]


class TerminationDefaultGate:
    """B117: create cure calendar / stop gate for termination and suspension."""

    pattern_id = "B117"

    def analyze(self, clause_text: str, *, notice_date: date | str | None = None, mode: str | None = None) -> CureGate:
        notice = parse_date(notice_date)
        mode_enum = self._mode(mode or clause_text)
        cure_days = self._extract_cure_days(clause_text)
        deadline = notice + timedelta(days=cure_days) if notice and cure_days is not None else None
        urgent = deadline is not None and (deadline - date.today()).days <= 2
        issues: list[ConstructionIssue] = []
        if notice:
            issues.append(
                make_issue(
                    self.pattern_id,
                    "termination/default attorney review",
                    "Termination/default/suspension notice requires attorney review and evidence preservation before action.",
                    risk_level=RiskLevel.CRITICAL if mode_enum is TerminationMode.CAUSE else RiskLevel.HIGH,
                    review_gate=ReviewGate.STOP_EXTERNAL_ACTION,
                    source_refs=(SourceRef("termination_clause", quote=clause_text),),
                    tags={"termination", mode_enum.value},
                    deadline=deadline,
                )
            )
        if mode_enum is TerminationMode.CONVENIENCE and not contains_any(clause_text, ("demobilization", "stored materials", "profit", "unperformed work")):
            issues.append(
                make_issue(
                    self.pattern_id,
                    "termination-for-convenience pricing gap",
                    "Convenience termination clause lacks clear demobilization/stored-material/profit treatment.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"termination", "pricing"},
                )
            )
        return CureGate(mode_enum, deadline, urgent, tuple(issues))

    @staticmethod
    def _extract_cure_days(text: str) -> int | None:
        match = re.search(r"(\d+)\s*(?:calendar\s*)?(?:day|days|hour|hours)", text.lower())
        if not match:
            return None
        value = int(match.group(1))
        if "hour" in match.group(0):
            return 1 if value <= 24 else max(1, value // 24)
        return value

    @staticmethod
    def _mode(text: str) -> TerminationMode:
        lowered = text.lower()
        if "convenience" in lowered:
            return TerminationMode.CONVENIENCE
        if "nonpayment" in lowered or "non-payment" in lowered:
            return TerminationMode.NONPAYMENT
        if "suspend" in lowered:
            return TerminationMode.SUSPENSION
        if "cause" in lowered or "default" in lowered:
            return TerminationMode.CAUSE
        if "safety" in lowered:
            return TerminationMode.SAFETY
        return TerminationMode.UNKNOWN


@dataclass(frozen=True)
class BackchargeReview:
    clean: bool
    support_score: int
    missing: tuple[str, ...]
    issues: tuple[ConstructionIssue, ...]


class BackchargeDocumentationGate:
    """B118: score backcharge/setoff proof completeness."""

    pattern_id = "B118"

    def review(self, backcharge: Mapping[str, Any]) -> BackchargeReview:
        required = ("notice", "opportunity_to_cure", "photos", "invoices", "causation")
        present = {key for key in required if backcharge.get(key)}
        missing = tuple(key for key in required if key not in present)
        score = len(present)
        issues: list[ConstructionIssue] = []
        if missing:
            issues.append(
                make_issue(
                    self.pattern_id,
                    "backcharge support incomplete",
                    f"Backcharge is missing: {', '.join(missing)}.",
                    risk_level=RiskLevel.HIGH if "notice" in missing or "causation" in missing else RiskLevel.MEDIUM,
                    review_gate=ReviewGate.BUSINESS_REVIEW,
                    source_refs=source_from_mapping(backcharge, "backcharge"),
                    tags={"backcharge", "proof"},
                    amount=backcharge.get("amount"),
                )
            )
        if backcharge.get("unrelated_project_setoff"):
            issues.append(
                make_issue(
                    self.pattern_id,
                    "unrelated project setoff review",
                    "Setoff/backcharge against an unrelated project requires attorney review.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"backcharge", "setoff"},
                    amount=backcharge.get("amount"),
                )
            )
        return BackchargeReview(clean=not issues, support_score=score, missing=missing, issues=tuple(issues))


@dataclass(frozen=True)
class CloseoutItem:
    name: str
    required: bool = True
    complete: bool = False
    owner: str | None = None
    source_refs: tuple[SourceRef, ...] = ()


@dataclass
class CloseoutMatrix:
    pattern_id: str = "B119"
    items: list[CloseoutItem] = field(default_factory=list)
    warranty_start: date | None = None
    warranty_uncertain: bool = False
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW


class WarrantyCloseoutMatrix:
    """B119: track closeout blockers, warranty start, and final-payment issues."""

    pattern_id = "B119"

    def build(self, items: Iterable[CloseoutItem | Mapping[str, Any]], *, warranty_start: date | str | None = None) -> CloseoutMatrix:
        matrix = CloseoutMatrix(warranty_start=parse_date(warranty_start))
        for raw in items:
            item = raw if isinstance(raw, CloseoutItem) else CloseoutItem(
                name=str(raw.get("name") or raw.get("item") or "closeout item"),
                required=bool(raw.get("required", True)),
                complete=bool(raw.get("complete")),
                owner=raw.get("owner"),
                source_refs=source_from_mapping(raw, "closeout"),
            )
            matrix.items.append(item)
            if item.required and not item.complete:
                matrix.issues.append(
                    make_issue(
                        self.pattern_id,
                        "closeout blocker",
                        f"Required closeout item '{item.name}' is incomplete.",
                        risk_level=RiskLevel.MEDIUM,
                        review_gate=ReviewGate.BUSINESS_REVIEW,
                        source_refs=item.source_refs,
                        tags={"closeout", normalize_label(item.name)},
                    )
                )
        if matrix.warranty_start is None:
            matrix.warranty_uncertain = True
            matrix.issues.append(
                make_issue(
                    self.pattern_id,
                    "warranty start unclear",
                    "Warranty start trigger/date is not documented.",
                    risk_level=RiskLevel.MEDIUM,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"warranty"},
                )
            )
        return matrix


@dataclass(frozen=True)
class ClassificationFactor:
    name: str
    direction: str
    evidence: str


@dataclass
class WorkerClassificationResult:
    pattern_id: str = "B120"
    risk_level: RiskLevel = RiskLevel.MEDIUM
    factors: list[ClassificationFactor] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.TAX_HR_REVIEW


class IndependentContractorClassifier:
    """B120: IRS/DOL-style IC classification risk matrix. No final determination."""

    pattern_id = "B120"

    def classify(self, facts: Mapping[str, Any]) -> WorkerClassificationResult:
        result = WorkerClassificationResult()
        employee_score = 0
        contractor_score = 0
        def add(name: str, direction: str, evidence: str, points: int = 1) -> None:
            nonlocal employee_score, contractor_score
            result.factors.append(ClassificationFactor(name, direction, evidence))
            if direction == "employee":
                employee_score += points
            elif direction == "contractor":
                contractor_score += points

        if facts.get("company_controls_schedule") or facts.get("company_controls_methods"):
            add("behavioral control", "employee", "Company controls schedule/methods.", 2)
        if facts.get("company_tools"):
            add("tools/investment", "employee", "Company supplies core tools/equipment.")
        if facts.get("exclusive") or facts.get("ongoing_indefinite"):
            add("permanence/exclusivity", "employee", "Relationship appears exclusive or indefinite.")
        if facts.get("core_business_work"):
            add("integral work", "employee", "Work is core/integral to business.")
        if facts.get("own_business"):
            add("business entity", "contractor", "Worker has separate business.")
        if facts.get("own_insurance") and facts.get("own_tools"):
            add("investment", "contractor", "Worker carries insurance and tools.")
        if facts.get("multiple_clients"):
            add("market availability", "contractor", "Worker markets to multiple clients.")
        if facts.get("negotiated_scope_price"):
            add("profit/loss", "contractor", "Worker can negotiate scope/price and manage profit/loss.")
        if employee_score >= contractor_score + 2:
            result.risk_level = RiskLevel.HIGH
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "high-risk independent contractor classification",
                    "Facts lean toward employee/economic-dependence treatment despite any IC label; final classification requires CPA/attorney/HR review.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.TAX_HR_REVIEW,
                    tags={"independent_contractor", "misclassification"},
                )
            )
        elif contractor_score >= employee_score + 2:
            result.risk_level = RiskLevel.LOW
        else:
            result.risk_level = RiskLevel.MEDIUM
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "insufficient or mixed IC evidence",
                    "Worker classification evidence is mixed or incomplete; do not make final classification without review.",
                    risk_level=RiskLevel.MEDIUM,
                    review_gate=ReviewGate.TAX_HR_REVIEW,
                    tags={"independent_contractor", "evidence"},
                )
            )
        return result


@dataclass(frozen=True)
class OnboardingRequirement:
    name: str
    status: str
    expires: date | None = None


@dataclass
class OnboardingPackResult:
    pattern_id: str = "B121"
    requirements: list[OnboardingRequirement] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    complete: bool = False
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW


class OnboardingCompliancePack:
    """B121: verify sub/IC onboarding docs before work/payment."""

    pattern_id = "B121"
    required_docs = ("signed_agreement", "w9", "coi", "license")

    def check(self, vendor_file: Mapping[str, Any], *, mobilization_date: date | str | None = None, project_end: date | str | None = None) -> OnboardingPackResult:
        result = OnboardingPackResult()
        mobilize = parse_date(mobilization_date)
        end = parse_date(project_end)
        for doc in self.required_docs:
            value = vendor_file.get(doc)
            expires = parse_date(vendor_file.get(f"{doc}_expires"))
            status = "complete" if value else "missing"
            if expires and end and expires < end:
                status = "expired_before_project_end"
            req = OnboardingRequirement(doc, status, expires)
            result.requirements.append(req)
            if status != "complete":
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "onboarding document issue",
                        f"{doc} status is {status}.",
                        risk_level=RiskLevel.HIGH if doc in {"coi", "license", "signed_agreement"} else RiskLevel.MEDIUM,
                        review_gate=ReviewGate.BUSINESS_REVIEW,
                        tags={"onboarding", doc},
                    )
                )
        signed_date = parse_date(vendor_file.get("signed_agreement_date"))
        if mobilize and (not signed_date or signed_date > mobilize):
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "work before signed agreement",
                    "Mobilization appears before signed agreement/work order evidence.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.BUSINESS_REVIEW,
                    tags={"onboarding", "work_before_contract"},
                    deadline=mobilize,
                )
            )
        result.complete = not result.issues
        return result


@dataclass(frozen=True)
class OverlayRoute:
    name: str
    source: str
    review_gate: ReviewGate


@dataclass
class JurisdictionOverlayResult:
    pattern_id: str = "B122"
    overlays: list[OverlayRoute] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    low_confidence: bool = False
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW


class JurisdictionOverlayRouter:
    """B122: route state/project-type overlays and block generic legal assumptions."""

    pattern_id = "B122"

    def route(self, jurisdiction: Jurisdiction) -> JurisdictionOverlayResult:
        result = JurisdictionOverlayResult()
        if not jurisdiction.state or jurisdiction.project_type is ProjectType.UNKNOWN:
            result.low_confidence = True
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "jurisdiction facts missing",
                    "Project state and public/private/federal status are required before lien/payment/deadline confidence.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"jurisdiction"},
                )
            )
            return result
        if jurisdiction.is_federal:
            result.overlays.append(OverlayRoute("Miller Act payment bond", "40 U.S.C. 3133", ReviewGate.ATTORNEY_REVIEW))
        if jurisdiction.is_kansas_private:
            result.overlays.extend([
                OverlayRoute("Kansas prompt pay", "K.S.A. 16-1803", ReviewGate.ATTORNEY_REVIEW),
                OverlayRoute("Kansas retainage", "K.S.A. 16-1904", ReviewGate.ATTORNEY_REVIEW),
                OverlayRoute("Kansas mechanic lien", "K.S.A. 60-1103", ReviewGate.ATTORNEY_REVIEW),
            ])
        if jurisdiction.normalized_state == "MO" and jurisdiction.project_type is ProjectType.PRIVATE:
            result.overlays.append(OverlayRoute("Missouri private lien/payment overlay", "state-specific research required", ReviewGate.ATTORNEY_REVIEW))
        return result


@dataclass(frozen=True)
class RedFlag:
    issue: ConstructionIssue
    priority: Decimal
    posture: str


@dataclass
class RedFlagQueueResult:
    pattern_id: str = "B123"
    flags: list[RedFlag] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW


class ContractRedFlagQueue:
    """B123: rank construction contract issues into practical negotiation posture."""

    pattern_id = "B123"
    severity_points = {
        RiskLevel.LOW: Decimal("1"),
        RiskLevel.MEDIUM: Decimal("3"),
        RiskLevel.HIGH: Decimal("7"),
        RiskLevel.CRITICAL: Decimal("12"),
    }

    def rank(self, issues: Iterable[ConstructionIssue]) -> RedFlagQueueResult:
        flags: list[RedFlag] = []
        for issue in issues:
            priority = self.severity_points[issue.risk_level]
            if issue.amount:
                priority += min(Decimal("10"), abs(issue.amount) / Decimal("10000"))
            if issue.deadline:
                days = (issue.deadline - date.today()).days
                if days <= 2:
                    priority += Decimal("5")
                elif days <= 7:
                    priority += Decimal("3")
            if issue.review_gate is ReviewGate.STOP_EXTERNAL_ACTION:
                posture = "stop"
                priority += Decimal("10")
            elif issue.review_gate is ReviewGate.ATTORNEY_REVIEW:
                posture = "attorney_review"
                priority += Decimal("4")
            elif issue.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
                posture = "negotiate"
            elif "ambiguous" in issue.tags or "scope" in issue.tags:
                posture = "clarify"
            else:
                posture = "accept_or_monitor"
            flags.append(RedFlag(issue, priority, posture))
        flags.sort(key=lambda flag: (flag.priority, flag.issue.title), reverse=True)
        return RedFlagQueueResult(flags=flags)
