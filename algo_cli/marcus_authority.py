"""Fail-closed authority vocabulary for every Algo CLI action.

This module intentionally contains no tool imports.  It is the stable policy
source consumed by the registry and, later in the hardening sequence, every
dispatcher and control broker.  Unknown actions receive an unclassified,
handoff-only policy rather than inheriting read-only authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Iterable, Mapping


class EffectClass(str, Enum):
    OBSERVE = "observe"
    LOCAL_MUTATION = "local_mutation"
    EXTERNAL_MUTATION = "external_mutation"
    DESTRUCTIVE = "destructive"
    CODE_EXECUTION = "code_execution"
    ORCHESTRATION = "orchestration"
    CONFIGURATION = "configuration"
    UNCLASSIFIED = "unclassified"


class ConfirmationMode(str, Enum):
    NONE = "none"
    SESSION_PREAPPROVAL = "session_preapproval"
    ACTION_TIME = "action_time"
    HANDOFF_REQUIRED = "handoff_required"


class DataClass(str, Enum):
    PUBLIC = "public"
    LOCAL_CONTENT = "local_content"
    USER_PROFILE = "user_profile"
    TELEMETRY = "telemetry"
    SENSITIVE = "sensitive"
    CREDENTIAL = "credential"
    AUTHENTICATION = "authentication"


class IdempotencyClass(str, Enum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    AT_MOST_ONCE = "at_most_once"
    NON_IDEMPOTENT = "non_idempotent"
    COMPENSATABLE = "compensatable"


class OutcomeModel(str, Enum):
    DETERMINISTIC = "deterministic"
    EXTERNAL_EFFECT = "external_effect"
    EVENTUAL_CONSISTENCY = "eventual_consistency"
    UNKNOWN_POSSIBLE = "unknown_possible"


class VerificationRequirement(str, Enum):
    NONE = "none"
    STRUCTURED_RESULT = "structured_result"
    FRESH_OBSERVATION = "fresh_observation"
    INDEPENDENT_POSTCONDITION = "independent_postcondition"


class TargetScope(str, Enum):
    NONE = "none"
    WORKSPACE = "workspace"
    RUNTIME = "runtime"
    MEMORY_STORE = "memory_store"
    MODEL_STORE = "model_store"
    PROVIDER = "provider"
    EXTERNAL_ACCOUNT = "external_account"
    PLUGIN = "plugin"
    EXTERNAL_STATE = "external_state"


class RuntimeClass(str, Enum):
    ADAPTIVE = "adaptive"
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


class Capability(IntEnum):
    # Existing stable ABI bits.
    READ = 1 << 0
    WRITE = 1 << 1
    SHELL = 1 << 2
    NETWORK = 1 << 3
    MODEL = 1 << 4
    CREDENTIAL = 1 << 5
    MEMORY = 1 << 6
    EXTERNAL_PUBLISH = 1 << 7
    DESTRUCTIVE = 1 << 8
    # Hardening authority bits.  These are intentionally unused by released
    # tools until their dedicated milestones pass.
    BROWSER_READ = 1 << 9
    BROWSER_NAVIGATE = 1 << 10
    BROWSER_INPUT = 1 << 11
    DESKTOP_OBSERVE = 1 << 12
    DESKTOP_INPUT = 1 << 13
    SCREEN_CAPTURE = 1 << 14
    ACCESSIBILITY = 1 << 15
    APPLE_EVENTS = 1 << 16
    CLIPBOARD_READ = 1 << 17
    CLIPBOARD_WRITE = 1 << 18
    DOWNLOAD = 1 << 19
    UPLOAD = 1 << 20
    DATA_EGRESS = 1 << 21
    PLUGIN_LOAD = 1 << 22
    ORCHESTRATE = 1 << 23
    UNCLASSIFIED = 1 << 30


@dataclass(frozen=True)
class CapabilityMask:
    value: int = 0

    def has(self, capability: Capability) -> bool:
        return bool(self.value & capability.value)

    def add(self, capability: Capability) -> "CapabilityMask":
        return CapabilityMask(self.value | capability.value)

    def remove(self, capability: Capability) -> "CapabilityMask":
        return CapabilityMask(self.value & ~capability.value)

    def contains(self, required: "CapabilityMask") -> bool:
        return required.value & ~self.value == 0

    def names(self) -> tuple[str, ...]:
        return tuple(capability.name.lower() for capability in Capability if self.has(capability))

    def to_dict(self) -> dict[str, object]:
        return {"value": self.value, "capabilities": list(self.names())}


def capability_mask(capabilities: Iterable[Capability]) -> CapabilityMask:
    value = 0
    for capability in capabilities:
        value |= capability.value
    return CapabilityMask(value)


def mask_from_names(names: Iterable[str]) -> CapabilityMask:
    values: list[Capability] = []
    for name in names:
        key = str(name).strip().upper()
        if key not in Capability.__members__:
            raise ValueError(f"unknown capability name: {name}")
        values.append(Capability[key])
    return capability_mask(values)


LEGACY_TIER_MASKS: dict[str, int] = {
    "tier0": Capability.READ.value,
    "tier1": Capability.READ.value | Capability.NETWORK.value | Capability.MODEL.value,
    "tier2": (
        Capability.READ.value
        | Capability.WRITE.value
        | Capability.SHELL.value
        | Capability.MODEL.value
        | Capability.MEMORY.value
    ),
    "tier3": sum(capability.value for capability in Capability if capability is not Capability.UNCLASSIFIED),
}


def tier_mask(tier: str) -> int:
    return LEGACY_TIER_MASKS.get((tier or "").strip().lower(), 0)


@dataclass(frozen=True)
class CuratedActionPolicy:
    effect_class: EffectClass
    maximum_risk: str
    confirmation_mode: ConfirmationMode
    capabilities: tuple[Capability, ...]
    data_classes: tuple[DataClass, ...]
    target_scope: TargetScope
    idempotency: IdempotencyClass
    outcome_model: OutcomeModel
    verification: VerificationRequirement
    fallback_group: str = ""
    curated: bool = True
    dynamic_resolution: bool = False
    runtime_class: RuntimeClass = RuntimeClass.ADAPTIVE
    estimated_cost: float = 2.0
    suppress_logs: bool = False
    compensation_action: str = ""

    def __post_init__(self) -> None:
        compensation = str(self.compensation_action or "").strip()
        if self.idempotency is IdempotencyClass.COMPENSATABLE and not compensation:
            raise ValueError("compensatable actions require an explicit compensation action")
        if compensation and self.idempotency is not IdempotencyClass.COMPENSATABLE:
            raise ValueError("compensation action requires compensatable idempotency")

    @property
    def capability_mask(self) -> CapabilityMask:
        return capability_mask(self.capabilities)

    @property
    def mutates_state(self) -> bool:
        return self.effect_class not in {EffectClass.OBSERVE}

    @property
    def requires_approval(self) -> bool:
        return self.confirmation_mode is not ConfirmationMode.NONE

    @property
    def safe_retry(self) -> bool:
        return (
            self.idempotency in {IdempotencyClass.PURE, IdempotencyClass.IDEMPOTENT}
            and self.outcome_model is not OutcomeModel.UNKNOWN_POSSIBLE
        )


def _read(
    *capabilities: Capability,
    data: tuple[DataClass, ...] = (DataClass.LOCAL_CONTENT,),
    target: TargetScope = TargetScope.RUNTIME,
    confirmation: ConfirmationMode = ConfirmationMode.NONE,
    risk: str = "low",
    outcome: OutcomeModel = OutcomeModel.DETERMINISTIC,
    runtime_class: RuntimeClass = RuntimeClass.ADAPTIVE,
    estimated_cost: float = 1.0,
    suppress_logs: bool = False,
) -> CuratedActionPolicy:
    return CuratedActionPolicy(
        EffectClass.OBSERVE,
        risk,
        confirmation,
        (Capability.READ, *capabilities),
        data,
        target,
        IdempotencyClass.PURE,
        outcome,
        VerificationRequirement.STRUCTURED_RESULT,
        runtime_class=runtime_class,
        estimated_cost=estimated_cost,
        suppress_logs=suppress_logs,
    )


def _local(
    *capabilities: Capability,
    data: tuple[DataClass, ...] = (DataClass.LOCAL_CONTENT,),
    target: TargetScope = TargetScope.WORKSPACE,
    confirmation: ConfirmationMode = ConfirmationMode.ACTION_TIME,
    risk: str = "high",
    idempotency: IdempotencyClass = IdempotencyClass.AT_MOST_ONCE,
    effect: EffectClass = EffectClass.LOCAL_MUTATION,
    verification: VerificationRequirement = VerificationRequirement.FRESH_OBSERVATION,
    runtime_class: RuntimeClass = RuntimeClass.INTERACTIVE,
    estimated_cost: float = 2.0,
    suppress_logs: bool = False,
) -> CuratedActionPolicy:
    return CuratedActionPolicy(
        effect,
        risk,
        confirmation,
        (Capability.READ, Capability.WRITE, *capabilities),
        data,
        target,
        idempotency,
        OutcomeModel.UNKNOWN_POSSIBLE,
        verification,
        runtime_class=runtime_class,
        estimated_cost=estimated_cost,
        suppress_logs=suppress_logs,
    )


def _external(
    *capabilities: Capability,
    data: tuple[DataClass, ...] = (DataClass.SENSITIVE,),
    confirmation: ConfirmationMode = ConfirmationMode.ACTION_TIME,
    risk: str = "high",
    effect: EffectClass = EffectClass.EXTERNAL_MUTATION,
    idempotency: IdempotencyClass = IdempotencyClass.AT_MOST_ONCE,
    runtime_class: RuntimeClass = RuntimeClass.INTERACTIVE,
    estimated_cost: float = 2.0,
    suppress_logs: bool = True,
) -> CuratedActionPolicy:
    return CuratedActionPolicy(
        effect,
        risk,
        confirmation,
        (Capability.READ, Capability.NETWORK, Capability.DATA_EGRESS, *capabilities),
        data,
        TargetScope.EXTERNAL_ACCOUNT,
        idempotency,
        OutcomeModel.UNKNOWN_POSSIBLE,
        VerificationRequirement.INDEPENDENT_POSTCONDITION,
        runtime_class=runtime_class,
        estimated_cost=estimated_cost,
        suppress_logs=suppress_logs,
    )


# This table is deliberately exhaustive for the released TOOL_MAP.  Adding a
# runtime tool without adding a policy makes registry readiness BLOCKED.
CURATED_TOOL_POLICIES: dict[str, CuratedActionPolicy] = {
    "action_program": CuratedActionPolicy(
        EffectClass.ORCHESTRATION,
        "high",
        ConfirmationMode.SESSION_PREAPPROVAL,
        (Capability.READ, Capability.ORCHESTRATE),
        (DataClass.LOCAL_CONTENT, DataClass.SENSITIVE),
        TargetScope.RUNTIME,
        IdempotencyClass.NON_IDEMPOTENT,
        OutcomeModel.UNKNOWN_POSSIBLE,
        VerificationRequirement.INDEPENDENT_POSTCONDITION,
        runtime_class=RuntimeClass.BACKGROUND,
        estimated_cost=4.0,
    ),
    "action_search": _read(target=TargetScope.RUNTIME),
    "append_lesson": _local(
        Capability.MEMORY,
        data=(DataClass.USER_PROFILE, DataClass.SENSITIVE),
        target=TargetScope.MEMORY_STORE,
        idempotency=IdempotencyClass.AT_MOST_ONCE,
    ),
    "available_actions": _read(
        target=TargetScope.RUNTIME,
        runtime_class=RuntimeClass.INTERACTIVE,
    ),
    "batch_edit": _local(),
    "capability_mask_describe": _read(target=TargetScope.RUNTIME),
    "credential_helpers_get": _read(
        Capability.CREDENTIAL,
        data=(DataClass.CREDENTIAL,),
        target=TargetScope.RUNTIME,
        confirmation=ConfirmationMode.ACTION_TIME,
        risk="high",
        suppress_logs=True,
    ),
    "credential_helpers_store": _local(
        Capability.CREDENTIAL,
        data=(DataClass.CREDENTIAL,),
        target=TargetScope.RUNTIME,
        idempotency=IdempotencyClass.IDEMPOTENT,
        suppress_logs=True,
    ),
    "edit_file": _local(),
    "embed_text": _read(
        Capability.MODEL,
        data=(DataClass.LOCAL_CONTENT, DataClass.SENSITIVE),
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        risk="medium",
    ),
    "extensions_manifest_build": _read(target=TargetScope.PLUGIN),
    "find_unique_anchor": _read(target=TargetScope.WORKSPACE),
    "git_diff": _read(target=TargetScope.WORKSPACE),
    "git_status": _read(target=TargetScope.WORKSPACE),
    "harness_competitive_rating": _read(target=TargetScope.RUNTIME),
    "harness_read": _read(target=TargetScope.RUNTIME),
    "harness_refresh": _local(
        data=(DataClass.LOCAL_CONTENT,),
        target=TargetScope.RUNTIME,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        idempotency=IdempotencyClass.IDEMPOTENT,
        risk="medium",
        runtime_class=RuntimeClass.BACKGROUND,
        estimated_cost=8.0,
    ),
    "harness_scorecard": _read(target=TargetScope.RUNTIME),
    "harness_search": _read(target=TargetScope.RUNTIME),
    "harness_stats": _read(target=TargetScope.RUNTIME),
    "list_directory": _read(target=TargetScope.WORKSPACE),
    "model_copy": _local(
        Capability.MODEL,
        target=TargetScope.MODEL_STORE,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    "model_create": _local(
        Capability.MODEL,
        target=TargetScope.MODEL_STORE,
        idempotency=IdempotencyClass.AT_MOST_ONCE,
    ),
    "model_delete": _local(
        Capability.MODEL,
        Capability.DESTRUCTIVE,
        target=TargetScope.MODEL_STORE,
        effect=EffectClass.DESTRUCTIVE,
        idempotency=IdempotencyClass.IDEMPOTENT,
        verification=VerificationRequirement.INDEPENDENT_POSTCONDITION,
    ),
    "model_pull": _local(
        Capability.NETWORK,
        Capability.MODEL,
        target=TargetScope.MODEL_STORE,
        confirmation=ConfirmationMode.ACTION_TIME,
        idempotency=IdempotencyClass.IDEMPOTENT,
        runtime_class=RuntimeClass.BACKGROUND,
        estimated_cost=8.0,
    ),
    "model_show": _read(Capability.MODEL, target=TargetScope.MODEL_STORE),
    "plugins_discover": _read(target=TargetScope.PLUGIN),
    "plugins_load": CuratedActionPolicy(
        EffectClass.CODE_EXECUTION,
        "high",
        ConfirmationMode.HANDOFF_REQUIRED,
        (Capability.READ, Capability.PLUGIN_LOAD),
        (DataClass.LOCAL_CONTENT, DataClass.SENSITIVE),
        TargetScope.PLUGIN,
        IdempotencyClass.AT_MOST_ONCE,
        OutcomeModel.UNKNOWN_POSSIBLE,
        VerificationRequirement.FRESH_OBSERVATION,
    ),
    "query_knowledge_graph": _read(target=TargetScope.MEMORY_STORE),
    "read_file": _read(target=TargetScope.WORKSPACE),
    "read_pdf": _read(target=TargetScope.WORKSPACE),
    "reindex_knowledge_graph": _local(
        Capability.MEMORY,
        target=TargetScope.MEMORY_STORE,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        idempotency=IdempotencyClass.IDEMPOTENT,
        risk="medium",
        runtime_class=RuntimeClass.BACKGROUND,
        estimated_cost=8.0,
    ),
    "remember": _local(
        Capability.MEMORY,
        data=(DataClass.USER_PROFILE, DataClass.SENSITIVE),
        target=TargetScope.MEMORY_STORE,
        idempotency=IdempotencyClass.AT_MOST_ONCE,
    ),
    "render_pdf_pages": _read(target=TargetScope.WORKSPACE),
    "run_shell": _local(
        Capability.SHELL,
        effect=EffectClass.CODE_EXECUTION,
        idempotency=IdempotencyClass.NON_IDEMPOTENT,
        verification=VerificationRequirement.INDEPENDENT_POSTCONDITION,
    ),
    "runtime_qos_hint": _read(target=TargetScope.RUNTIME),
    "screenshot_description_verify": _read(
        Capability.MODEL,
        Capability.DATA_EGRESS,
        data=(DataClass.SENSITIVE,),
        confirmation=ConfirmationMode.ACTION_TIME,
        risk="high",
    ),
    "search_files": _read(target=TargetScope.WORKSPACE),
    "session_command": CuratedActionPolicy(
        EffectClass.ORCHESTRATION,
        "high",
        ConfirmationMode.NONE,
        (Capability.READ,),
        (DataClass.LOCAL_CONTENT, DataClass.USER_PROFILE),
        TargetScope.RUNTIME,
        IdempotencyClass.NON_IDEMPOTENT,
        OutcomeModel.UNKNOWN_POSSIBLE,
        VerificationRequirement.INDEPENDENT_POSTCONDITION,
        dynamic_resolution=True,
        runtime_class=RuntimeClass.INTERACTIVE,
        estimated_cost=1.0,
    ),
    "session_slash": CuratedActionPolicy(
        EffectClass.ORCHESTRATION,
        "high",
        ConfirmationMode.NONE,
        (Capability.READ,),
        (DataClass.LOCAL_CONTENT, DataClass.USER_PROFILE),
        TargetScope.RUNTIME,
        IdempotencyClass.NON_IDEMPOTENT,
        OutcomeModel.UNKNOWN_POSSIBLE,
        VerificationRequirement.INDEPENDENT_POSTCONDITION,
        dynamic_resolution=True,
        runtime_class=RuntimeClass.INTERACTIVE,
        estimated_cost=1.0,
    ),
    "small_context_ledger_preview": _read(
        data=(DataClass.LOCAL_CONTENT, DataClass.SENSITIVE),
        target=TargetScope.RUNTIME,
    ),
    "update_user_profile": _local(
        Capability.MEMORY,
        data=(DataClass.USER_PROFILE, DataClass.SENSITIVE),
        target=TargetScope.MEMORY_STORE,
        idempotency=IdempotencyClass.IDEMPOTENT,
    ),
    "url_scheme_parse": _read(data=(DataClass.PUBLIC,), target=TargetScope.RUNTIME),
    "version_manifest_build": _read(target=TargetScope.RUNTIME),
    "vision_describe": _read(
        Capability.MODEL,
        Capability.DATA_EGRESS,
        data=(DataClass.SENSITIVE,),
        confirmation=ConfirmationMode.ACTION_TIME,
        risk="high",
    ),
    "web_fetch": _read(
        Capability.NETWORK,
        Capability.DATA_EGRESS,
        data=(DataClass.PUBLIC, DataClass.SENSITIVE),
        target=TargetScope.PROVIDER,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        risk="medium",
        outcome=OutcomeModel.EXTERNAL_EFFECT,
    ),
    "web_search": _read(
        Capability.NETWORK,
        Capability.DATA_EGRESS,
        data=(DataClass.PUBLIC, DataClass.SENSITIVE),
        target=TargetScope.PROVIDER,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        risk="medium",
        outcome=OutcomeModel.EXTERNAL_EFFECT,
    ),
    "write_file": _local(),
    "write_knowledge_graph_note": _local(
        Capability.MEMORY,
        data=(DataClass.LOCAL_CONTENT, DataClass.SENSITIVE),
        target=TargetScope.MEMORY_STORE,
        idempotency=IdempotencyClass.AT_MOST_ONCE,
    ),
    "x_account_draft_post": _read(
        Capability.NETWORK,
        data=(DataClass.USER_PROFILE, DataClass.SENSITIVE),
        target=TargetScope.EXTERNAL_ACCOUNT,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        risk="medium",
        outcome=OutcomeModel.EXTERNAL_EFFECT,
    ),
    "x_account_draft_reply": _read(
        Capability.NETWORK,
        data=(DataClass.USER_PROFILE, DataClass.SENSITIVE),
        target=TargetScope.EXTERNAL_ACCOUNT,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        risk="medium",
        outcome=OutcomeModel.EXTERNAL_EFFECT,
    ),
    "x_account_post": _external(Capability.EXTERNAL_PUBLISH),
    "x_account_post_action": _external(Capability.EXTERNAL_PUBLISH),
    "x_account_reply": _external(Capability.EXTERNAL_PUBLISH),
    "x_account_status": _read(
        Capability.NETWORK,
        data=(DataClass.USER_PROFILE,),
        target=TargetScope.EXTERNAL_ACCOUNT,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        risk="medium",
        outcome=OutcomeModel.EXTERNAL_EFFECT,
    ),
    "x_search": _read(
        Capability.NETWORK,
        Capability.DATA_EGRESS,
        data=(DataClass.PUBLIC, DataClass.SENSITIVE),
        target=TargetScope.PROVIDER,
        confirmation=ConfirmationMode.SESSION_PREAPPROVAL,
        risk="medium",
        outcome=OutcomeModel.EXTERNAL_EFFECT,
    ),
}


def policy_for_action(name: str) -> CuratedActionPolicy:
    try:
        return CURATED_TOOL_POLICIES[name]
    except KeyError:
        return CuratedActionPolicy(
            EffectClass.UNCLASSIFIED,
            "high",
            ConfirmationMode.HANDOFF_REQUIRED,
            (Capability.UNCLASSIFIED,),
            (DataClass.SENSITIVE,),
            TargetScope.EXTERNAL_STATE,
            IdempotencyClass.NON_IDEMPOTENT,
            OutcomeModel.UNKNOWN_POSSIBLE,
            VerificationRequirement.INDEPENDENT_POSTCONDITION,
            curated=False,
            runtime_class=RuntimeClass.BACKGROUND,
            estimated_cost=8.0,
            suppress_logs=True,
        )


class CuratedToolRegistry(dict[str, Any]):
    """A runtime map that refuses tools without a curated authority policy."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        if initial:
            self.update(initial)

    @staticmethod
    def _validate(name: str, value: Any) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("runtime tool names must be non-empty strings")
        if not callable(value):
            raise TypeError(f"runtime tool {name!r} is not callable")
        if not policy_for_action(name).curated:
            raise ValueError(f"runtime tool {name!r} has no curated authority policy")

    def __setitem__(self, name: str, value: Any) -> None:
        self._validate(name, value)
        super().__setitem__(name, value)

    def update(self, other: Mapping[str, Any] | Iterable[tuple[str, Any]] = (), **kwargs: Any) -> None:  # type: ignore[override]
        pairs = dict(other, **kwargs)
        for name, value in pairs.items():
            self._validate(name, value)
        super().update(pairs)


@dataclass(frozen=True)
class ConsentGrant:
    grant_id: str
    capability_mask: int
    allowed_actions: frozenset[str]
    allowed_targets: frozenset[str]
    expires_at: float
    maximum_action_count: int
    issued_at: float = 0.0
    source: str = "unspecified"

    def permits(self, action: str, required: CapabilityMask, target: str, now: float) -> bool:
        if now >= self.expires_at or self.maximum_action_count <= 0:
            return False
        if action not in self.allowed_actions or target not in self.allowed_targets:
            return False
        return CapabilityMask(self.capability_mask).contains(required)


@dataclass(frozen=True)
class ActionPermit:
    permit_id: str
    grant_id: str
    action_digest: str
    target: str
    snapshot_revision: str
    fencing_token: int
    expires_at: float
    maximum_action_count: int = 1


@dataclass(frozen=True)
class ResolvedAction:
    name: str
    target: str
    target_scope: TargetScope
    effect_class: EffectClass
    capability_mask: int
    data_classes: tuple[DataClass, ...]
    confirmation_mode: ConfirmationMode
    idempotency: IdempotencyClass
    outcome_model: OutcomeModel
    verification: VerificationRequirement
    action_digest: str
    snapshot_revision: str
    compensation_action: str = ""


@dataclass(frozen=True)
class ConfirmationReceipt:
    receipt_id: str
    action_digest: str
    confirmation_mode: ConfirmationMode
    confirmed_at: float
    expires_at: float


__all__ = [
    "ActionPermit",
    "Capability",
    "CapabilityMask",
    "ConfirmationMode",
    "ConfirmationReceipt",
    "ConsentGrant",
    "CURATED_TOOL_POLICIES",
    "CuratedActionPolicy",
    "CuratedToolRegistry",
    "DataClass",
    "EffectClass",
    "IdempotencyClass",
    "LEGACY_TIER_MASKS",
    "OutcomeModel",
    "ResolvedAction",
    "RuntimeClass",
    "TargetScope",
    "VerificationRequirement",
    "capability_mask",
    "mask_from_names",
    "policy_for_action",
    "tier_mask",
]
