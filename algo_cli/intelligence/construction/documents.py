"""Construction document, scope, flowdown, and dispute patterns B106-B108/B124-B126."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from .common import (
    ConstructionIssue,
    ReviewGate,
    RiskLevel,
    SourceRef,
    contains_any,
    make_issue,
    normalize_label,
    normalize_space,
    source_from_mapping,
)


class ScopeStatus(str, Enum):
    INCLUDED = "included"
    EXCLUDED = "excluded"
    BY_OTHERS = "by_others"
    ALLOWANCE = "allowance"
    UNIT_PRICE = "unit_price"
    AMBIGUOUS = "ambiguous"
    RISK = "scope_risk"


@dataclass(frozen=True)
class ContractDocument:
    id: str
    name: str
    document_type: str
    text: str = ""
    references: tuple[str, ...] = ()
    precedence: int | None = None
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class DocumentEdge:
    source_id: str
    target_id: str
    relation: str


@dataclass
class ContractDocumentGraph:
    pattern_id: str = "B106"
    documents: dict[str, ContractDocument] = field(default_factory=dict)
    edges: list[DocumentEdge] = field(default_factory=list)
    missing_references: list[str] = field(default_factory=list)
    precedence_map: dict[str, int] = field(default_factory=dict)
    conflicts: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW

    @property
    def requires_human_review(self) -> bool:
        return bool(self.missing_references or self.conflicts)


class ContractDocumentGraphCompiler:
    """B106: build a contract document graph and precedence/conflict map."""

    pattern_id = "B106"

    def compile(self, documents: Iterable[ContractDocument | Mapping[str, Any]]) -> ContractDocumentGraph:
        parsed = {doc.id: doc for doc in (self._coerce_document(item) for item in documents)}
        graph = ContractDocumentGraph(documents=parsed)
        for doc in parsed.values():
            if doc.precedence is not None:
                graph.precedence_map[doc.id] = doc.precedence
            for ref in doc.references:
                relation = self._relation_for_reference(ref)
                if ref in parsed:
                    graph.edges.append(DocumentEdge(doc.id, ref, relation))
                else:
                    graph.missing_references.append(ref)
                    graph.conflicts.append(
                        make_issue(
                            self.pattern_id,
                            "missing upstream document",
                            f"{doc.name} references {ref}, but that document was not supplied.",
                            risk_level=RiskLevel.HIGH,
                            review_gate=ReviewGate.ATTORNEY_REVIEW,
                            source_refs=doc.source_refs,
                            tags={"missing_document", relation},
                        )
                    )
        if len({value for value in graph.precedence_map.values()}) != len(graph.precedence_map):
            graph.conflicts.append(
                make_issue(
                    self.pattern_id,
                    "duplicate precedence rank",
                    "Multiple contract documents share the same precedence rank; confirm order of precedence.",
                    risk_level=RiskLevel.MEDIUM,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"precedence"},
                )
            )
        if not graph.precedence_map and len(parsed) > 1:
            graph.conflicts.append(
                make_issue(
                    self.pattern_id,
                    "precedence unknown",
                    "Multiple contract documents were supplied but no order-of-precedence map was found.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"precedence", "conflict"},
                )
            )
        return graph

    @staticmethod
    def _coerce_document(item: ContractDocument | Mapping[str, Any]) -> ContractDocument:
        if isinstance(item, ContractDocument):
            return item
        refs = item.get("source_refs") or ()
        source_refs = tuple(ref for ref in refs if isinstance(ref, SourceRef)) or (
            SourceRef(str(item.get("name") or item.get("id") or "document"), quote=item.get("text")),
        )
        return ContractDocument(
            id=str(item.get("id") or normalize_label(str(item.get("name") or "document"))),
            name=str(item.get("name") or item.get("id") or "document"),
            document_type=str(item.get("document_type") or item.get("type") or "unknown"),
            text=str(item.get("text") or ""),
            references=tuple(str(ref) for ref in item.get("references", ())),
            precedence=item.get("precedence"),
            source_refs=source_refs,
        )

    @staticmethod
    def _relation_for_reference(ref: str) -> str:
        lowered = ref.lower()
        if "prime" in lowered or "owner" in lowered:
            return "flow_down"
        if "addendum" in lowered or "change" in lowered or "mod" in lowered:
            return "modifies"
        return "incorporates_by_reference"


@dataclass(frozen=True)
class ScopeAtom:
    item: str
    status: ScopeStatus
    source_refs: tuple[SourceRef, ...] = ()
    notes: str = ""


@dataclass
class ScopeMatrix:
    pattern_id: str = "B107"
    atoms: list[ScopeAtom] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW

    @property
    def ambiguous_items(self) -> list[ScopeAtom]:
        return [atom for atom in self.atoms if atom.status in {ScopeStatus.AMBIGUOUS, ScopeStatus.RISK}]


class ScopeMatrixBuilder:
    """B107: classify inclusions/exclusions/by-others/scope-risk atoms."""

    pattern_id = "B107"
    _status_terms = {
        ScopeStatus.EXCLUDED: ("exclude", "excluded", "not included", "exclusion"),
        ScopeStatus.BY_OTHERS: ("by others", "owner provided", "gc provided", "others to provide"),
        ScopeStatus.ALLOWANCE: ("allowance",),
        ScopeStatus.UNIT_PRICE: ("unit price", "unit-price", "unit pricing"),
        ScopeStatus.INCLUDED: ("include", "included", "provide", "furnish", "install"),
    }

    def build(
        self,
        scope_items: Iterable[Mapping[str, Any] | ScopeAtom],
        drawing_items: Iterable[str] = (),
        *,
        precedence_supports_exclusion: bool = False,
    ) -> ScopeMatrix:
        matrix = ScopeMatrix()
        seen: set[str] = set()
        for item in scope_items:
            atom = item if isinstance(item, ScopeAtom) else self._coerce_atom(item)
            seen.add(normalize_label(atom.item))
            matrix.atoms.append(atom)
            if atom.status is ScopeStatus.AMBIGUOUS:
                matrix.issues.append(
                    make_issue(
                        self.pattern_id,
                        "ambiguous scope item",
                        f"Scope item '{atom.item}' is not clearly included, excluded, or by others.",
                        risk_level=RiskLevel.MEDIUM,
                        review_gate=ReviewGate.BUSINESS_REVIEW,
                        source_refs=atom.source_refs,
                        tags={"scope", "ambiguous"},
                    )
                )
        excluded = {normalize_label(atom.item) for atom in matrix.atoms if atom.status in {ScopeStatus.EXCLUDED, ScopeStatus.BY_OTHERS}}
        for drawing_item in drawing_items:
            key = normalize_label(drawing_item)
            if key in seen:
                continue
            status = ScopeStatus.EXCLUDED if precedence_supports_exclusion and key in excluded else ScopeStatus.RISK
            atom = ScopeAtom(drawing_item, status, notes="mentioned in drawings/specs but not priced in scope")
            matrix.atoms.append(atom)
            matrix.issues.append(
                make_issue(
                    self.pattern_id,
                    "scope risk",
                    f"'{drawing_item}' appears in drawings/specs but not in the priced scope matrix.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.BUSINESS_REVIEW,
                    tags={"scope", "drawing_reference"},
                )
            )
        if not any("permit" in atom.item.lower() for atom in matrix.atoms):
            matrix.issues.append(
                make_issue(
                    self.pattern_id,
                    "permit responsibility missing",
                    "Permit responsibility is not clearly assigned in the scope matrix.",
                    risk_level=RiskLevel.MEDIUM,
                    review_gate=ReviewGate.BUSINESS_REVIEW,
                    tags={"scope", "permits"},
                )
            )
        return matrix

    def _coerce_atom(self, item: Mapping[str, Any]) -> ScopeAtom:
        text = normalize_space(str(item.get("item") or item.get("description") or item.get("text") or ""))
        explicit = item.get("status")
        status = ScopeStatus(str(explicit)) if explicit in ScopeStatus._value2member_map_ else self._infer_status(text)
        return ScopeAtom(text, status, source_refs=source_from_mapping(item), notes=str(item.get("notes") or ""))

    def _infer_status(self, text: str) -> ScopeStatus:
        lowered = text.lower()
        for status, terms in self._status_terms.items():
            if any(term in lowered for term in terms):
                return status
        return ScopeStatus.AMBIGUOUS


@dataclass(frozen=True)
class FlowDownObligation:
    category: str
    upstream_clause: str
    subcontractor_obligation: str
    source_refs: tuple[SourceRef, ...] = ()
    recovery_limited: bool = False


@dataclass
class FlowDownResult:
    pattern_id: str = "B108"
    obligations: list[FlowDownObligation] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    missing_upstream_documents: list[str] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW


class FlowDownRiskMapper:
    """B108: map upstream obligations incorporated by flow-down language."""

    pattern_id = "B108"
    _category_terms = {
        "payment": ("payment", "paid", "retainage"),
        "schedule": ("schedule", "milestone", "delay", "time"),
        "changes": ("change", "extra work", "directive"),
        "claims": ("claim", "notice", "dispute"),
        "insurance": ("insurance", "additional insured", "bond"),
        "safety": ("safety", "osha", "hazard"),
        "indemnity": ("indemn", "hold harmless"),
    }

    def map(
        self,
        subcontract_text: str,
        upstream_clauses: Iterable[Mapping[str, Any]] | None = None,
        required_upstream_documents: Iterable[str] = (),
    ) -> FlowDownResult:
        result = FlowDownResult()
        has_flowdown = contains_any(subcontract_text, ("flow down", "flow-down", "bound to contractor", "same obligations", "prime contract"))
        clauses = list(upstream_clauses or [])
        if has_flowdown and not clauses:
            result.missing_upstream_documents.extend(required_upstream_documents or ["prime contract"])
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "missing upstream document",
                    "Subcontract contains flow-down language but upstream contract documents were not supplied.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"flowdown", "missing_document"},
                )
            )
            return result
        if not has_flowdown:
            return result
        for clause in clauses:
            text = normalize_space(str(clause.get("text") or clause.get("clause") or ""))
            category = self._category(text)
            recovery_limited = contains_any(text, ("condition precedent", "only to the extent", "no damages", "owner pays"))
            obligation = FlowDownObligation(
                category=category,
                upstream_clause=str(clause.get("id") or clause.get("name") or category),
                subcontractor_obligation=text,
                source_refs=source_from_mapping(clause),
                recovery_limited=recovery_limited,
            )
            result.obligations.append(obligation)
            if recovery_limited:
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "downstream recovery limit",
                        f"Upstream {category} clause may limit downstream recovery; attorney review required.",
                        risk_level=RiskLevel.HIGH,
                        review_gate=ReviewGate.ATTORNEY_REVIEW,
                        source_refs=obligation.source_refs,
                        tags={"flowdown", "recovery_limit", category},
                    )
                )
        return result

    def _category(self, text: str) -> str:
        for category, terms in self._category_terms.items():
            if contains_any(text, terms):
                return category
        return "general"


@dataclass(frozen=True)
class DelegatedDesignItem:
    item: str
    requires_professional_review: bool
    source_refs: tuple[SourceRef, ...] = ()


@dataclass
class DelegatedDesignResult:
    pattern_id: str = "B124"
    items: list[DelegatedDesignItem] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW


class DelegatedDesignGate:
    """B124: detect design-build/delegated design and professional-liability gaps."""

    pattern_id = "B124"

    def analyze(self, clauses: Iterable[Mapping[str, Any]], *, has_professional_liability: bool = False) -> DelegatedDesignResult:
        result = DelegatedDesignResult()
        for clause in clauses:
            text = normalize_space(str(clause.get("text") or ""))
            refs = source_from_mapping(clause)
            design_term = contains_any(text, ("delegated design", "shall design", "signed and sealed", "performance requirements", "calculations"))
            if not design_term:
                continue
            item = DelegatedDesignItem(str(clause.get("id") or clause.get("name") or "design obligation"), True, refs)
            result.items.append(item)
            if not has_professional_liability:
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "delegated design coverage review",
                        "Design/delegated-design language found but professional-liability coverage was not evidenced.",
                        risk_level=RiskLevel.HIGH,
                        review_gate=ReviewGate.INSURANCE_REVIEW,
                        source_refs=refs,
                        tags={"delegated_design", "insurance"},
                    )
                )
            if "performance requirements" in text.lower() and not contains_any(text, ("criteria", "basis of design", "load", "capacity")):
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "performance criteria unclear",
                        "Performance-spec language appears without clear measurable criteria; RFI/attorney review required.",
                        risk_level=RiskLevel.MEDIUM,
                        review_gate=ReviewGate.ATTORNEY_REVIEW,
                        source_refs=refs,
                        tags={"delegated_design", "rfi"},
                    )
                )
        return result


@dataclass(frozen=True)
class LowerTierGap:
    requirement: str
    upstream_value: str
    lower_tier_value: str | None
    message: str


@dataclass
class LowerTierFlowdownResult:
    pattern_id: str = "B125"
    gaps: list[LowerTierGap] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW


class LowerTierFlowdownPack:
    """B125: compare upstream requirements to lower-tier subcontract/supplier terms."""

    pattern_id = "B125"

    def compare(self, upstream_requirements: Mapping[str, Any], lower_tier_terms: Mapping[str, Any]) -> LowerTierFlowdownResult:
        result = LowerTierFlowdownResult()
        for key, upstream_value in upstream_requirements.items():
            lower_value = lower_tier_terms.get(key)
            if lower_value == upstream_value:
                continue
            if key.endswith("_deadline_days") and lower_value is not None:
                try:
                    if int(lower_value) <= int(upstream_value):
                        continue
                except (TypeError, ValueError):
                    pass
            gap = LowerTierGap(key, str(upstream_value), None if lower_value is None else str(lower_value), f"Lower-tier {key} does not satisfy upstream requirement.")
            result.gaps.append(gap)
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "lower-tier mismatch",
                    gap.message,
                    risk_level=RiskLevel.HIGH if "insurance" in key or "deadline" in key else RiskLevel.MEDIUM,
                    review_gate=ReviewGate.ATTORNEY_REVIEW if "indemn" in key or "venue" in key else ReviewGate.BUSINESS_REVIEW,
                    tags={"lower_tier", normalize_label(key)},
                )
            )
        return result


@dataclass(frozen=True)
class DisputeProtocol:
    path: tuple[str, ...]
    forum: str | None
    notice_recipients: tuple[str, ...]
    email_allowed: bool
    issues: tuple[ConstructionIssue, ...] = ()


class DisputeProtocolExtractor:
    """B126: extract dispute path, forum, and notice protocol."""

    pattern_id = "B126"

    def extract(self, dispute_clause: str, notice_clause: str = "") -> DisputeProtocol:
        text = f"{dispute_clause}\n{notice_clause}"
        lowered = text.lower()
        path: list[str] = []
        for step in ("negotiation", "initial decision", "mediation", "arbitration", "litigation"):
            if step in lowered:
                path.append(step)
        if not path and "court" in lowered:
            path.append("litigation")
        forum = None
        if "american arbitration association" in lowered or "aaa" in lowered:
            forum = "AAA"
        elif "district court" in lowered:
            forum = "district court"
        elif "state court" in lowered:
            forum = "state court"
        recipients = tuple(part.strip() for part in notice_clause.split(";") if "@" in part or "attn" in part.lower())
        email_allowed = "email" in lowered or "electronic" in lowered
        issues: list[ConstructionIssue] = []
        if "arbitration" in path and forum is None:
            issues.append(
                make_issue(
                    self.pattern_id,
                    "arbitration forum missing",
                    "Arbitration is selected but no forum/rules were identified.",
                    risk_level=RiskLevel.MEDIUM,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"dispute", "arbitration"},
                )
            )
        if email_allowed and not recipients:
            issues.append(
                make_issue(
                    self.pattern_id,
                    "email notice protocol incomplete",
                    "Electronic notice appears allowed but recipient/protocol is unclear.",
                    risk_level=RiskLevel.MEDIUM,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"notice", "email"},
                )
            )
        return DisputeProtocol(tuple(path), forum, recipients, email_allowed, tuple(issues))
