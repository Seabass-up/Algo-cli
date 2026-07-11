"""B103. Evidence Binder / PBC Indexer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from .common import EvidenceItem, EvidenceStatus, SourceRef


@dataclass
class EvidenceRequest:
    id: str
    description: str
    required: bool = True
    evidence: list[EvidenceItem] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: datetime | None = None
    expected_hashes: set[str] = field(default_factory=set)


@dataclass
class EvidenceReport:
    total_requests: int
    complete_count: int
    missing_count: int
    stale_count: int
    open_ids: list[str]
    stale_ids: list[str]

    @property
    def complete(self) -> bool:
        return self.missing_count == 0 and self.stale_count == 0


class EvidenceBinder:
    """Indexes support for workpapers/PBC requests with source hashes."""

    def __init__(self) -> None:
        self.requests: dict[str, EvidenceRequest] = {}

    def add_request(self, request_id: str, description: str, required: bool = True) -> EvidenceRequest:
        request = EvidenceRequest(id=request_id, description=description, required=required)
        self.requests[request_id] = request
        return request

    def attach_evidence(
        self,
        request_id: str,
        item_id: str,
        description: str,
        source_refs: Iterable[SourceRef],
        prepared_by: str | None = None,
    ) -> EvidenceItem:
        request = self.requests[request_id]
        item = EvidenceItem(
            id=item_id,
            description=description,
            source_refs=list(source_refs),
            prepared_by=prepared_by,
            status=EvidenceStatus.RECEIVED,
        )
        request.evidence.append(item)
        return item

    def mark_reviewed(self, request_id: str, reviewer: str) -> None:
        request = self.requests[request_id]
        request.expected_hashes = {
            ref.document_hash
            for item in request.evidence
            for ref in item.source_refs
            if ref.document_hash
        }
        for item in request.evidence:
            item.reviewed_by = reviewer
            item.status = EvidenceStatus.REVIEWED
        request.reviewed_at = datetime.now(timezone.utc)

    def refresh_stale_status(self) -> list[str]:
        stale_ids: list[str] = []
        for request in self.requests.values():
            if not request.expected_hashes:
                continue
            current_hashes = {
                ref.document_hash
                for item in request.evidence
                for ref in item.source_refs
                if ref.document_hash
            }
            if current_hashes != request.expected_hashes:
                stale_ids.append(request.id)
                for item in request.evidence:
                    item.status = EvidenceStatus.STALE
        return stale_ids

    def completeness_report(self) -> EvidenceReport:
        stale_ids = self.refresh_stale_status()
        open_ids: list[str] = []
        complete_count = 0
        missing_count = 0
        for request in self.requests.values():
            has_support = any(item.has_support for item in request.evidence)
            reviewed_or_received = any(
                item.status in (EvidenceStatus.RECEIVED, EvidenceStatus.REVIEWED) for item in request.evidence
            )
            if request.required and not (has_support and reviewed_or_received):
                missing_count += 1
                open_ids.append(request.id)
            else:
                complete_count += 1
        return EvidenceReport(
            total_requests=len(self.requests),
            complete_count=complete_count,
            missing_count=missing_count,
            stale_count=len(stale_ids),
            open_ids=open_ids,
            stale_ids=stale_ids,
        )
