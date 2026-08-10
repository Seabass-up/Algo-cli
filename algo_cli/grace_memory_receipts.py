"""Persistent, content-free Grace receipts for Echo-protected auxiliary state.

Grace is deliberately smaller than the action/run receipt systems.  It gives
memory-adjacent stores a single fail-closed HMAC authority and provides a
read-only legacy-tree inventory that startup migration can consult without
importing the runtime configuration module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping


ELSIE_RECEIPT_SCHEMA_VERSION = 1
ELSIE_RECEIPT_SCHEME = "hmac-sha256-v1"
_KDF_DOMAIN = b"algo-cli/elsie-memory-receipts/kdf/v1\x00"
_MESSAGE_DOMAIN = b"algo-cli/elsie-memory-receipts/message/v1\x00"
_RECEIPT_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_CANONICAL_BYTES = 1_048_576
_MAX_NAMESPACE_BYTES = 96
_MAX_INVENTORY_ENTRIES = 10_000
_CONFIG_READ_LIMIT = 1_048_576
_STORE_RECEIPT_NAMESPACES = frozenset(
    {
        "goal.store",
        "agent_thread.store",
        "memory.candidate_store",
        "skill.run_history_store",
    }
)
_STORE_ANCHOR_NAMES = {
    "goal.store": "elsie-goal-store-v1",
    "agent_thread.store": "elsie-agent-thread-store-v1",
    "memory.candidate_store": "elsie-memory-candidate-store-v1",
    "skill.run_history_store": "elsie-skill-run-history-store-v1",
}
_SANITIZED_CONFIG_KEYS = frozenset(
    {
        "algorithmic_tool_policy_enabled",
        "auto_cloud_connect",
        "auto_mode",
        "cloud",
        "code_rag_consent_version",
        "code_rag_enabled",
        "echo_veil_capacity",
        "echo_veil_embedding_context_length",
        "echo_veil_embedding_dimension",
        "echo_veil_embedding_gpu_layers",
        "echo_veil_embedding_keep_alive_seconds",
        "echo_veil_enabled",
        "echo_veil_profile",
        "echo_veil_protection",
        "echo_veil_scope",
        "embedding_backend",
        "external_harness_sources_enabled",
        "harness_embed_model",
        "index_compute_lab_auto_inject",
        "intuition_capture_enabled",
        "intuition_recall_enabled",
        "keep_alive",
        "max_tool_iterations",
        "memory_auto_capture_consent_version",
        "memory_auto_capture_enabled",
        "memory_auto_char_limit",
        "memory_auto_daily_limit",
        "memory_auto_entry_limit",
        "model",
        "model_adaptive",
        "num_ctx",
        "onboarded",
        "prune_after_messages",
        "prune_keep_recent",
        "reasoning_auto_reflexion",
        "reasoning_auto_verify",
        "reasoning_branches",
        "reasoning_chat_enabled",
        "reasoning_depth",
        "reasoning_mode",
        "reasoning_ns_rounds",
        "reasoning_qcr_samples",
        "reasoning_reflexion_attempts",
        "reflex_enabled",
        "safe_mode",
        "session_mode",
        "show_thinking",
        "skill_crystallize_enabled",
        "skill_crystallize_every",
        "temperature",
        "theme",
        "tool_think_every",
        "verify_mode",
    }
)


class ElsieReceiptError(RuntimeError):
    """Raised when protected auxiliary persistence cannot be authenticated."""


class ReceiptNamespace(str, Enum):
    """Closed domains prevent one sink's receipt from authenticating another."""

    SKILL_GOAL = "skill.goal"
    SKILL_TOOL_IDENTITY = "skill.tool_identity"
    SKILL_TOOL_ARGUMENTS = "skill.tool_arguments"
    SKILL_OUTCOME = "skill.outcome"
    SKILL_RUN_HISTORY_STORE = "skill.run_history_store"
    GOAL_REASON = "goal.reason"
    GOAL_HISTORY = "goal.history"
    GOAL_STORE = "goal.store"
    AGENT_BLOCK_OUTPUT = "display.agent_block_output"
    AGENT_THREAD_CONTEXT = "agent_thread.context"
    AGENT_THREAD_OUTPUT = "agent_thread.output"
    AGENT_THREAD_CHECKPOINT = "agent_thread.checkpoint"
    AGENT_THREAD_STORE = "agent_thread.store"
    MEMORY_CANDIDATE = "memory.candidate"
    MEMORY_CANDIDATE_STORE = "memory.candidate_store"


@dataclass(frozen=True)
class ReceiptBinding:
    schema_version: int
    scheme: str
    key_id: str
    key_backend: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: object) -> ReceiptBinding:
        if not isinstance(value, Mapping):
            raise ElsieReceiptError("elsie receipt binding is missing")
        if set(value) != {"schema_version", "scheme", "key_id", "key_backend"}:
            raise ElsieReceiptError("elsie receipt binding fields are invalid")
        schema_version = value.get("schema_version")
        scheme = value.get("scheme")
        key_id = value.get("key_id")
        key_backend = value.get("key_backend")
        if schema_version != ELSIE_RECEIPT_SCHEMA_VERSION:
            raise ElsieReceiptError("unsupported elsie receipt schema")
        if scheme != ELSIE_RECEIPT_SCHEME:
            raise ElsieReceiptError("unsupported elsie receipt scheme")
        if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
            raise ElsieReceiptError("elsie receipt key id is invalid")
        if not isinstance(key_backend, str) or not key_backend or len(key_backend) > 96 or not key_backend.isascii():
            raise ElsieReceiptError("elsie receipt key backend is invalid")
        return cls(schema_version, scheme, key_id, key_backend)


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ElsieReceiptError("elsie receipt value is not canonical JSON") from exc
    if len(encoded) > _MAX_CANONICAL_BYTES:
        raise ElsieReceiptError("elsie receipt value exceeds the bounded size")
    return encoded


def _framed_message(namespace: ReceiptNamespace, value: Any) -> bytes:
    if not isinstance(namespace, ReceiptNamespace):
        raise ElsieReceiptError("elsie receipt namespace is not registered")
    namespace_bytes = namespace.value.encode("ascii")
    if not namespace_bytes or len(namespace_bytes) > _MAX_NAMESPACE_BYTES:
        raise ElsieReceiptError("elsie receipt namespace is invalid")
    payload = _canonical_bytes(value)
    return b"".join(
        (
            _MESSAGE_DOMAIN,
            len(namespace_bytes).to_bytes(2, "big"),
            namespace_bytes,
            len(payload).to_bytes(8, "big"),
            payload,
        )
    )


class ElsieReceiptAuthority:
    """Domain-separated HMAC authority backed only by persistent key material."""

    def __init__(
        self,
        *,
        derived_key: bytes,
        key_backend: str,
        anchor_store: Any | None = None,
    ) -> None:
        if not isinstance(derived_key, bytes) or len(derived_key) != 32:
            raise ElsieReceiptError("elsie derived key must be 32 bytes")
        if not isinstance(key_backend, str) or not key_backend or len(key_backend) > 96:
            raise ElsieReceiptError("elsie key backend is invalid")
        if key_backend == "volatile_process":
            raise ElsieReceiptError("elsie refuses volatile key material")
        self._derived_key = bytes(derived_key)
        self._anchor_store = anchor_store
        self.binding = ReceiptBinding(
            schema_version=ELSIE_RECEIPT_SCHEMA_VERSION,
            scheme=ELSIE_RECEIPT_SCHEME,
            key_id="sha256:" + hashlib.sha256(self._derived_key).hexdigest(),
            key_backend=key_backend,
        )

    @classmethod
    def from_key_store(cls, *, store: Any | None = None) -> ElsieReceiptAuthority:
        """Create/load the persistent receipt key for a protected write."""

        # Local imports keep the legacy inventory usable while config.py itself
        # is importing this module during pre-load migration.
        from .grace_key_store import (
            GraceReceiptAnchorStore,
            KeyringKeyStore,
            KeyStoreError,
            get_key_material,
        )
        from .irene_privacy_views import PRIVACY_KEY_LABEL

        selected = store if store is not None else KeyringKeyStore()
        try:
            material = get_key_material(
                PRIVACY_KEY_LABEL,
                length=32,
                require_persistent=True,
                store=selected,
            )
        except KeyStoreError as exc:
            raise ElsieReceiptError("persistent elsie key material is unavailable") from exc
        anchor_store = (
            GraceReceiptAnchorStore(selected)
            if type(selected) is KeyringKeyStore
            else selected
            if all(callable(getattr(selected, name, None)) for name in ("load", "compare_and_set"))
            else None
        )
        return cls._from_key_material(material, anchor_store=anchor_store)

    @classmethod
    def from_existing_key_store(
        cls,
        *,
        store: Any | None = None,
    ) -> ElsieReceiptAuthority:
        """Load an existing key without mutating credentials on a read path."""

        from .grace_key_store import GraceReceiptAnchorStore, KeyStoreError, KeyringKeyStore
        from .irene_privacy_views import PRIVACY_KEY_LABEL

        selected = store if store is not None else KeyringKeyStore()
        loader = getattr(selected, "get_existing", None)
        if not callable(loader):
            raise ElsieReceiptError("existing-only elsie key lookup is unsupported by this key store")
        try:
            material = loader(PRIVACY_KEY_LABEL, length=32)
        except KeyStoreError as exc:
            raise ElsieReceiptError("existing persistent elsie key is unavailable") from exc
        except Exception as exc:
            raise ElsieReceiptError("existing persistent elsie key lookup failed") from exc
        anchor_store = (
            GraceReceiptAnchorStore(selected)
            if type(selected) is KeyringKeyStore
            else selected
            if all(callable(getattr(selected, name, None)) for name in ("load", "compare_and_set"))
            else None
        )
        return cls._from_key_material(material, anchor_store=anchor_store)

    @classmethod
    def from_optional_existing_key_store(
        cls,
        *,
        store: Any | None = None,
    ) -> ElsieReceiptAuthority | None:
        """Load an existing key, returning None only for a proven absence."""

        from .grace_key_store import GraceReceiptAnchorStore, KeyStoreError, KeyringKeyStore
        from .irene_privacy_views import PRIVACY_KEY_LABEL

        selected = store if store is not None else KeyringKeyStore()
        loader = getattr(selected, "get_existing", None)
        if not callable(loader):
            raise ElsieReceiptError("existing-only elsie key lookup is unsupported by this key store")
        try:
            material = loader(PRIVACY_KEY_LABEL, length=32)
        except KeyStoreError as exc:
            if str(exc) == "required Algo CLI key material is absent":
                return None
            raise ElsieReceiptError("existing persistent elsie key is unavailable") from exc
        except Exception as exc:
            raise ElsieReceiptError("existing persistent elsie key lookup failed") from exc
        anchor_store = (
            GraceReceiptAnchorStore(selected)
            if type(selected) is KeyringKeyStore
            else selected
            if all(callable(getattr(selected, name, None)) for name in ("load", "compare_and_set"))
            else None
        )
        return cls._from_key_material(material, anchor_store=anchor_store)

    @classmethod
    def _from_key_material(
        cls,
        material: Any,
        *,
        anchor_store: Any | None = None,
    ) -> ElsieReceiptAuthority:
        if not getattr(material, "persistent", False) or getattr(material, "backend", "") == "volatile_process":
            raise ElsieReceiptError("elsie refuses volatile key material")
        key = getattr(material, "key", None)
        backend = getattr(material, "backend", None)
        if not isinstance(key, bytes) or len(key) != 32:
            raise ElsieReceiptError("elsie master key must be 32 bytes")
        if not isinstance(backend, str) or not backend:
            raise ElsieReceiptError("elsie key backend is invalid")
        derived = hmac.new(key, _KDF_DOMAIN, hashlib.sha256).digest()
        return cls(
            derived_key=derived,
            key_backend=backend,
            anchor_store=anchor_store,
        )

    def receipt(self, namespace: ReceiptNamespace, value: Any) -> str:
        digest = hmac.new(
            self._derived_key,
            _framed_message(namespace, value),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def verify(self, namespace: ReceiptNamespace, value: Any, receipt: str) -> bool:
        if not isinstance(receipt, str) or _RECEIPT_RE.fullmatch(receipt) is None:
            return False
        return hmac.compare_digest(self.receipt(namespace, value), receipt)

    def require_binding(self, value: object) -> None:
        observed = ReceiptBinding.from_mapping(value)
        if observed != self.binding:
            raise ElsieReceiptError("elsie receipt authority binding mismatch")

    def store_receipt(self, namespace: ReceiptNamespace, value: Any) -> str:
        """Authenticate an entire bounded auxiliary-store payload."""

        _require_store_namespace(namespace)
        return self.receipt(
            namespace,
            {"kind": "elsie_store_state", "value": value},
        )

    def anchor_store(self, override: Any | None = None) -> Any:
        selected = override if override is not None else self._anchor_store
        if selected is None or any(not callable(getattr(selected, name, None)) for name in ("load", "compare_and_set")):
            raise ElsieReceiptError("persistent elsie rollback anchor is unavailable")
        return selected


def _require_store_namespace(namespace: ReceiptNamespace) -> None:
    if not isinstance(namespace, ReceiptNamespace) or namespace.value not in _STORE_RECEIPT_NAMESPACES:
        raise ElsieReceiptError("elsie store receipt namespace is not registered")


def _receipt_hex(value: str) -> str:
    if not is_hmac_receipt(value):
        raise ElsieReceiptError("elsie store receipt is invalid")
    return value.removeprefix("hmac-sha256:")


def _store_subject(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 4_096:
        raise ElsieReceiptError("elsie store subject is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ElsieReceiptError("elsie store subject is invalid")
    return value


def _anchor_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _store_anchor_id(
    authority: ElsieReceiptAuthority,
    namespace: ReceiptNamespace,
    subject: str,
) -> str:
    _require_store_namespace(namespace)
    safe_subject = _store_subject(subject)
    digest = authority.receipt(
        namespace,
        {"kind": "elsie_store_anchor_id", "subject": safe_subject},
    )
    return "sha256:" + _receipt_hex(digest)


def _store_subject_digest(
    authority: ElsieReceiptAuthority,
    namespace: ReceiptNamespace,
    subject: str,
) -> str:
    safe_subject = _store_subject(subject)
    return _receipt_hex(
        authority.receipt(
            namespace,
            {"kind": "elsie_store_subject", "subject": safe_subject},
        )
    )


def build_elsie_store_head(
    authority: ElsieReceiptAuthority,
    namespace: ReceiptNamespace,
    *,
    subject: str,
    sequence: int,
    store_receipt: str,
) -> Any:
    """Build one authenticated, content-free external rollback head."""

    from .grace_key_store import ContentFreeReceiptHead

    _require_store_namespace(namespace)
    journal_id = _store_anchor_id(authority, namespace, subject)
    anchor_namespace = _STORE_ANCHOR_NAMES[namespace.value]
    subject_digest = _store_subject_digest(authority, namespace, subject)
    head_digest = _receipt_hex(store_receipt)
    unsigned = {
        "kind": ContentFreeReceiptHead.KIND,
        "schema_version": 1,
        "namespace": anchor_namespace,
        "journal_id": journal_id,
        "subject_digest": subject_digest,
        "sequence": sequence,
        "head_digest": head_digest,
    }
    authentication = _receipt_hex(
        authority.receipt(
            namespace,
            {"kind": "elsie_store_anchor_authentication", "head": unsigned},
        )
    )
    return ContentFreeReceiptHead(
        namespace=anchor_namespace,
        journal_id=journal_id,
        subject_digest=subject_digest,
        sequence=sequence,
        head_digest=head_digest,
        authentication=authentication,
    )


def load_elsie_store_anchor(
    authority: ElsieReceiptAuthority,
    namespace: ReceiptNamespace,
    *,
    subject: str,
    anchor_store: Any | None = None,
) -> Any | None:
    """Load and authenticate one external head without creating state."""

    from .grace_key_store import ContentFreeReceiptHead

    expected = build_elsie_store_head(
        authority,
        namespace,
        subject=subject,
        sequence=0,
        store_receipt=authority.store_receipt(namespace, {"anchor_probe": True}),
    )
    try:
        encoded = authority.anchor_store(anchor_store).load(expected.journal_id)
        if encoded is None:
            return None
        head = ContentFreeReceiptHead.from_bytes(encoded)
    except Exception as exc:
        raise ElsieReceiptError("persistent elsie rollback anchor is unavailable") from exc
    unsigned = head.unsigned_payload()
    expected_authentication = _receipt_hex(
        authority.receipt(
            namespace,
            {"kind": "elsie_store_anchor_authentication", "head": unsigned},
        )
    )
    if (
        head.namespace != expected.namespace
        or head.journal_id != expected.journal_id
        or head.subject_digest != expected.subject_digest
        or not hmac.compare_digest(head.authentication, expected_authentication)
    ):
        raise ElsieReceiptError("persistent elsie rollback anchor is invalid")
    return head


def require_elsie_store_anchor(
    authority: ElsieReceiptAuthority,
    namespace: ReceiptNamespace,
    *,
    subject: str,
    sequence: int,
    store_receipt: str,
    anchor_store: Any | None = None,
) -> None:
    """Verify that a protected store exactly matches its external head."""

    head = load_elsie_store_anchor(
        authority,
        namespace,
        subject=subject,
        anchor_store=anchor_store,
    )
    if (
        head is None
        or head.sequence != sequence
        or not hmac.compare_digest(head.head_digest, _receipt_hex(store_receipt))
    ):
        raise ElsieReceiptError("protected elsie store rollback or rewrite detected")


def advance_elsie_store_anchor(
    authority: ElsieReceiptAuthority,
    namespace: ReceiptNamespace,
    *,
    subject: str,
    sequence: int,
    previous_store_receipt: str,
    store_receipt: str,
    anchor_store: Any | None = None,
) -> None:
    """CAS the external head before publishing the matching local store."""

    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ElsieReceiptError("elsie store sequence is invalid")
    current = load_elsie_store_anchor(
        authority,
        namespace,
        subject=subject,
        anchor_store=anchor_store,
    )
    if current is None:
        if sequence != 1 or previous_store_receipt:
            raise ElsieReceiptError("protected elsie store anchor is missing")
        expected_digest = None
    else:
        current_receipt = "hmac-sha256:" + current.head_digest
        if sequence != current.sequence + 1 or not hmac.compare_digest(previous_store_receipt, current_receipt):
            raise ElsieReceiptError("protected elsie store update is stale")
        expected_digest = _anchor_digest(current.to_bytes())
    desired = build_elsie_store_head(
        authority,
        namespace,
        subject=subject,
        sequence=sequence,
        store_receipt=store_receipt,
    )
    try:
        changed = authority.anchor_store(anchor_store).compare_and_set(
            desired.journal_id,
            expected_digest=expected_digest,
            value=desired.to_bytes(),
        )
    except Exception as exc:
        raise ElsieReceiptError("persistent elsie rollback anchor is unavailable") from exc
    if not changed:
        raise ElsieReceiptError("protected elsie store anchor race detected")
    require_elsie_store_anchor(
        authority,
        namespace,
        subject=subject,
        sequence=sequence,
        store_receipt=store_receipt,
        anchor_store=anchor_store,
    )


def elsie_staging_path(target: Path | str) -> Path:
    """Return the fixed same-directory staging name for one Elsie store."""

    selected = Path(target)
    if not selected.name or selected.name in {".", ".."}:
        raise ElsieReceiptError("elsie store target is invalid")
    return selected.with_name(f".{selected.name}.elsie-pending")


def publish_elsie_staged_file(
    stage: Path | str,
    target: Path | str,
    *,
    expected_payload: bytes,
) -> None:
    """Atomically publish one exact, already-fsynced owner-only Elsie stage."""

    staged = Path(stage)
    selected = Path(target)
    if staged != elsie_staging_path(selected) or staged.parent != selected.parent:
        raise ElsieReceiptError("elsie store stage identity is invalid")
    if type(expected_payload) is not bytes or not expected_payload or len(expected_payload) > _MAX_CANONICAL_BYTES:
        raise ElsieReceiptError("elsie store expected payload is invalid")
    if os.name == "nt":
        from . import config as config_module

        try:
            with config_module._windows_pinned_directory_chain(staged.parent) as parent_chain:
                _publish_elsie_staged_file_bound(
                    staged,
                    selected,
                    expected_payload=expected_payload,
                    parent_chain=parent_chain,
                )
        except ElsieReceiptError:
            raise
        except OSError as exc:
            raise ElsieReceiptError("elsie store stage could not be published") from exc
        return
    _publish_elsie_staged_file_bound(
        staged,
        selected,
        expected_payload=expected_payload,
        parent_chain=(),
    )


def _publish_elsie_staged_file_bound(
    staged: Path,
    selected: Path,
    *,
    expected_payload: bytes,
    parent_chain: tuple[tuple[Path, tuple[int, ...]], ...],
) -> None:
    stage_descriptor: int | None = None
    try:
        from . import config as config_module

        if parent_chain:
            config_module._recheck_directory_chain(parent_chain)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        stage_path_info = staged.lstat()
        stage_descriptor = os.open(staged, flags)
        stage_info = os.fstat(stage_descriptor)
        if (
            config_module._path_is_reparse_point(staged, stage_path_info)
            or not stat.S_ISREG(stage_info.st_mode)
            or stage_info.st_nlink != 1
            or config_module._portable_state_identity(stage_path_info)
            != config_module._portable_state_identity(stage_info)
            or (hasattr(os, "getuid") and stage_info.st_uid != os.getuid())
            or stage_info.st_size != len(expected_payload)
            or (os.name == "nt" and not config_module._windows_private_dacl(staged))
        ):
            raise ElsieReceiptError("elsie store stage is unsafe")
        final_stage_path = config_module._windows_descriptor_final_path(stage_descriptor)
        if final_stage_path is not None and not os.path.samefile(final_stage_path, staged):
            raise ElsieReceiptError("elsie store stage descriptor path is unsafe")
        chunks: list[bytes] = []
        remaining = len(expected_payload) + 1
        while remaining > 0:
            chunk = os.read(stage_descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if not hmac.compare_digest(b"".join(chunks), expected_payload):
            raise ElsieReceiptError("elsie store stage payload changed")
        stage_after = os.fstat(stage_descriptor)
        stage_current = staged.lstat()
        if (
            config_module._path_is_reparse_point(staged, stage_current)
            or config_module._portable_state_identity(stage_after) != config_module._portable_state_identity(stage_info)
            or config_module._portable_state_identity(stage_current)
            != config_module._portable_state_identity(stage_info)
        ):
            raise ElsieReceiptError("elsie store stage changed while being read")
        try:
            target_info = selected.lstat()
        except FileNotFoundError:
            target_info = None
        if target_info is not None:
            if (
                config_module._path_is_reparse_point(selected, target_info)
                or not stat.S_ISREG(target_info.st_mode)
                or target_info.st_nlink != 1
                or (hasattr(os, "getuid") and target_info.st_uid != os.getuid())
            ):
                raise ElsieReceiptError("elsie store target is unsafe")
            if os.name == "nt" and not config_module._windows_private_dacl(selected):
                try:
                    target_info = config_module._windows_canonicalize_private_path(selected, directory=False)
                except OSError as exc:
                    raise ElsieReceiptError("elsie store target ACL is unsafe") from exc
        if os.name == "nt":
            # CRT file descriptors deny delete sharing on Windows. The parent
            # DACL excludes other principals from child creation/mutation, so
            # close only after binding exact bytes, then revalidate the same
            # stage immediately before the atomic rename.  An explicitly
            # configured store parent may remain readable; confidentiality is
            # enforced on the atomically-created stage and published target.
            if not config_module._windows_safe_creation_dacl(selected.parent):
                raise ElsieReceiptError("elsie store parent ACL is unsafe")
            config_module._recheck_directory_chain(parent_chain)
            os.close(stage_descriptor)
            stage_descriptor = None
            current_stage = staged.lstat()
            if config_module._path_is_reparse_point(staged, current_stage) or config_module._portable_state_identity(
                current_stage
            ) != config_module._portable_state_identity(stage_info):
                raise ElsieReceiptError("elsie store stage changed before publication")
            config_module._recheck_directory_chain(parent_chain)
            config_module._move_file_write_through(staged, selected, replace=True)
        else:
            os.replace(staged, selected)
        verification_flags = (
            flags
            if os.name != "nt"
            else (
                os.O_RDWR
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
        )
        descriptor = os.open(selected, verification_flags)
        try:
            published = os.fstat(descriptor)
            published_path = selected.lstat()
            if (
                config_module._path_is_reparse_point(selected, published_path)
                or not stat.S_ISREG(published.st_mode)
                or published.st_nlink != 1
                or config_module._portable_publication_identity(published)
                != config_module._portable_publication_identity(stage_info)
                or config_module._portable_publication_identity(published_path)
                != config_module._portable_publication_identity(stage_info)
                or (os.name == "nt" and not config_module._windows_private_dacl(selected))
            ):
                raise ElsieReceiptError("elsie store publication changed identity")
            final_selected_path = config_module._windows_descriptor_final_path(descriptor)
            if final_selected_path is not None and not os.path.samefile(final_selected_path, selected):
                raise ElsieReceiptError("elsie store publication descriptor path is unsafe")
            chunks = []
            remaining = len(expected_payload) + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if not hmac.compare_digest(b"".join(chunks), expected_payload):
                raise ElsieReceiptError("elsie store published payload changed")
            final_expected_identity = config_module._portable_publication_identity(stage_info)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
                final_expected_identity = config_module._portable_publication_identity(os.fstat(descriptor))
            os.fsync(descriptor)
            final_descriptor_info = os.fstat(descriptor)
            final_path_info = selected.lstat()
            if (
                config_module._path_is_reparse_point(selected, final_path_info)
                or final_descriptor_info.st_nlink != 1
                or config_module._portable_publication_identity(final_descriptor_info) != final_expected_identity
                or config_module._portable_publication_identity(final_path_info) != final_expected_identity
                or (os.name == "nt" and not config_module._windows_private_dacl(selected))
            ):
                raise ElsieReceiptError("elsie store publication changed during flush")
        finally:
            os.close(descriptor)
        if os.name == "posix":
            directory_fd = os.open(
                selected.parent,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        elif parent_chain:
            config_module._recheck_directory_chain(parent_chain)
    except ElsieReceiptError:
        raise
    except OSError as exc:
        raise ElsieReceiptError("elsie store stage could not be published") from exc
    finally:
        if stage_descriptor is not None:
            try:
                os.close(stage_descriptor)
            except OSError:
                pass


class LegacyArtifactClass(str, Enum):
    CONFIGURATION = "configuration"
    AUTH_SIDECAR = "auth_sidecar"
    MEMORY = "memory"
    RUN_HISTORY = "run_history"
    LAST_BLOCK = "last_block"
    CANDIDATE_STATE = "candidate_state"
    DERIVED_SKILL = "derived_skill"
    DERIVED_RUNTIME = "derived_runtime"
    UNKNOWN = "unknown"
    SYMLINK = "symlink"
    SPECIAL = "special"


@dataclass(frozen=True)
class LegacyArtifact:
    relative_path: str
    classification: LegacyArtifactClass
    automatic_copy_allowed: bool


@dataclass(frozen=True)
class LegacyMigrationInventory:
    root: Path
    echo_selected: bool
    artifacts: tuple[LegacyArtifact, ...]
    truncated: bool = False

    @property
    def safe_automatic_copy_paths(self) -> tuple[str, ...]:
        return tuple(item.relative_path for item in self.artifacts if item.automatic_copy_allowed)

    @property
    def blocked_paths(self) -> tuple[str, ...]:
        return tuple(item.relative_path for item in self.artifacts if not item.automatic_copy_allowed)


_AUTH_SIDECARS = frozenset(
    {
        ".env",
        "env",
        "chatgpt_auth.json",
        "google_workspace_auth.json",
        "google_workspace_pending_login.json",
        "xai_auth.json",
    }
)
_MEMORY_NAMES = frozenset(
    {
        "memory.json",
        "system_memory.json",
        "system_memory_index.json",
        "prompt_history.txt",
        "context_state.json",
    }
)
_DERIVED_RUNTIME_NAMES = frozenset(
    {
        "task_ledger.json",
        "agent_threads.json",
        "perf_history.jsonl",
        "embed_perf.jsonl",
    }
)


def _classify_legacy_path(relative_path: str) -> LegacyArtifactClass:
    path = PurePosixPath(relative_path)
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    if relative_path == "config.json":
        return LegacyArtifactClass.CONFIGURATION
    if relative_path in _AUTH_SIDECARS:
        return LegacyArtifactClass.AUTH_SIDECAR
    if name == "memory_candidate_state.json":
        return LegacyArtifactClass.CANDIDATE_STATE
    if name.startswith("last-block-") and name.endswith(".md"):
        return LegacyArtifactClass.LAST_BLOCK
    if name == "run_history.jsonl" or "saves" in parts or "context_archives" in parts:
        return LegacyArtifactClass.RUN_HISTORY
    if name in _MEMORY_NAMES or "identity" in parts:
        return LegacyArtifactClass.MEMORY
    if "skill_quarantine" in parts or "skills" in parts:
        return LegacyArtifactClass.DERIVED_SKILL
    if name in _DERIVED_RUNTIME_NAMES or "agent_runs" in parts or "private" in parts:
        return LegacyArtifactClass.DERIVED_RUNTIME
    return LegacyArtifactClass.UNKNOWN


def _open_pinned_file(root: Path, relative_path: str, *, max_bytes: int) -> bytes:
    """Read a regular descendant through no-follow directory descriptors."""

    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ElsieReceiptError("legacy artifact path is invalid")
    if os.name == "nt":
        from . import config as config_module

        selected = root.joinpath(*relative.parts)
        file_flags = (
            os.O_RDONLY
            | int(getattr(os, "O_BINARY", 0))
            | int(getattr(os, "O_CLOEXEC", 0))
            | int(getattr(os, "O_NOFOLLOW", 0))
            | int(getattr(os, "O_NONBLOCK", 0))
        )
        try:
            return config_module._state_payload_by_path(
                selected,
                relative=None,
                max_bytes=max_bytes,
                file_flags=file_flags,
                require_single_link=True,
            )
        except OSError as exc:
            raise ElsieReceiptError("legacy artifact identity could not be pinned") from exc
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | nofollow | cloexec | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    try:
        root_fd = os.open(root, directory_flags)
        descriptors.append(root_fd)
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise ElsieReceiptError("legacy root is not a directory")
        parent_fd = root_fd
        for part in relative.parts[:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            info = os.fstat(child_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise ElsieReceiptError("legacy artifact parent is not a directory")
            parent_fd = child_fd
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=parent_fd)
        descriptors.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 <= info.st_size <= max_bytes:
            raise ElsieReceiptError("legacy artifact is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise ElsieReceiptError("legacy artifact exceeds the bounded size")
        return payload
    except ElsieReceiptError:
        raise
    except OSError as exc:
        raise ElsieReceiptError("legacy artifact identity could not be pinned") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def legacy_config_selects_echo(root: Path | str) -> bool:
    """Content-bounded pre-load check; malformed configs fail closed."""

    try:
        encoded = _open_pinned_file(
            Path(root),
            "config.json",
            max_bytes=_CONFIG_READ_LIMIT,
        )
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
    except (ElsieReceiptError, UnicodeError, json.JSONDecodeError):
        return True
    if not isinstance(payload, Mapping):
        return True
    enabled = payload.get("echo_veil_enabled", False)
    protection = payload.get("echo_veil_protection", "optional")
    if type(enabled) is not bool or not isinstance(protection, str):
        return True
    return enabled or protection.strip().casefold() == "required"


def sanitized_legacy_config(root: Path | str) -> dict[str, Any]:
    """Return a strict content-free configuration projection for migration.

    Session summaries, attempt ledgers, context, prompts, paths, auth material,
    messages, and unknown future fields are omitted by construction.  The
    caller must atomically serialize this returned mapping; it must not copy
    the source bytes.
    """

    encoded = _open_pinned_file(
        Path(root),
        "config.json",
        max_bytes=_CONFIG_READ_LIMIT,
    )
    try:
        payload = json.loads(encoded.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ElsieReceiptError("legacy config is malformed") from exc
    if not isinstance(payload, Mapping):
        raise ElsieReceiptError("legacy config is malformed")
    enabled = payload.get("echo_veil_enabled", False)
    protection = payload.get("echo_veil_protection", "optional")
    if type(enabled) is not bool or not isinstance(protection, str):
        raise ElsieReceiptError("legacy Echo selection is malformed")
    if not (enabled or protection.strip().casefold() == "required"):
        raise ElsieReceiptError("sanitized migration requires Echo selection")
    projected: dict[str, Any] = {}
    for key in sorted(_SANITIZED_CONFIG_KEYS):
        value = payload.get(key)
        if isinstance(value, str):
            if len(value) <= 256 and "\n" not in value and "\r" not in value:
                projected[key] = value
        elif type(value) in {bool, int}:
            projected[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            projected[key] = value
    projected["echo_veil_enabled"] = bool(enabled)
    projected["echo_veil_protection"] = protection.strip().casefold()
    # Crystallization cannot consume protected history; preserve an explicit
    # opt-in only after the new runtime has established clean state.
    projected["skill_crystallize_enabled"] = False
    return projected


def inventory_legacy_tree(
    root: Path | str,
    *,
    echo_selected: bool | None = None,
    max_entries: int = _MAX_INVENTORY_ENTRIES,
) -> LegacyMigrationInventory:
    """Classify a legacy tree without following links or mutating any path.

    Echo-selected migrations allow only the bounded configuration file and
    recognized auth sidecars to be considered by the caller.  This function
    never copies or deletes anything; unknown and special paths stay blocked.
    """

    base = Path(root)
    from . import config as config_module

    try:
        root_info = base.lstat()
    except OSError as exc:
        raise ElsieReceiptError("legacy root is unavailable") from exc
    if config_module._path_is_reparse_point(base, root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ElsieReceiptError("legacy root must be a real directory")
    selected = legacy_config_selects_echo(base) if echo_selected is None else bool(echo_selected)
    bounded_limit = min(_MAX_INVENTORY_ENTRIES, max(0, int(max_entries)))
    artifacts: list[LegacyArtifact] = []
    truncated = False
    if not base.exists():
        return LegacyMigrationInventory(base, selected, (), False)
    for current, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        entries = sorted([*dirnames, *filenames])
        for name in entries:
            if len(artifacts) >= bounded_limit:
                truncated = True
                break
            path = current_path / name
            relative = path.relative_to(base).as_posix()
            try:
                descriptor = path.lstat()
            except OSError:
                artifacts.append(LegacyArtifact(relative, LegacyArtifactClass.SPECIAL, False))
                continue
            if config_module._path_is_reparse_point(path, descriptor):
                classification = LegacyArtifactClass.SYMLINK
                if name in dirnames:
                    dirnames.remove(name)
            elif not (stat.S_ISREG(descriptor.st_mode) or stat.S_ISDIR(descriptor.st_mode)):
                classification = LegacyArtifactClass.SPECIAL
            else:
                classification = _classify_legacy_path(relative)
            # Echo-selected trees are never copied automatically. The caller
            # may use sanitized_legacy_config() for a strict settings-only
            # projection after separately revalidating the source identity.
            allowed = False
            if not selected:
                # The helper still refuses links/special/unknown paths.  A
                # caller may explicitly migrate classified regular artifacts.
                allowed = stat.S_ISREG(descriptor.st_mode) and classification not in {
                    LegacyArtifactClass.SYMLINK,
                    LegacyArtifactClass.SPECIAL,
                    LegacyArtifactClass.UNKNOWN,
                }
            artifacts.append(LegacyArtifact(relative, classification, allowed))
        if truncated:
            break
    return LegacyMigrationInventory(base, selected, tuple(artifacts), truncated)


def read_pinned_legacy_artifact(
    root: Path | str,
    artifact: LegacyArtifact,
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> bytes:
    """Read one inventory-approved file while pinning every path component."""

    if not isinstance(artifact, LegacyArtifact) or not artifact.automatic_copy_allowed:
        raise ElsieReceiptError("legacy artifact is not approved for automatic copy")
    if not 0 < int(max_bytes) <= 16 * 1024 * 1024:
        raise ElsieReceiptError("legacy artifact size bound is invalid")
    if _classify_legacy_path(artifact.relative_path) != artifact.classification:
        raise ElsieReceiptError("legacy artifact classification changed")
    return _open_pinned_file(
        Path(root),
        artifact.relative_path,
        max_bytes=int(max_bytes),
    )


def is_hmac_receipt(value: object) -> bool:
    return isinstance(value, str) and _RECEIPT_RE.fullmatch(value) is not None


__all__ = [
    "ELSIE_RECEIPT_SCHEMA_VERSION",
    "ELSIE_RECEIPT_SCHEME",
    "ElsieReceiptAuthority",
    "ElsieReceiptError",
    "LegacyArtifact",
    "LegacyArtifactClass",
    "LegacyMigrationInventory",
    "ReceiptBinding",
    "ReceiptNamespace",
    "advance_elsie_store_anchor",
    "build_elsie_store_head",
    "elsie_staging_path",
    "inventory_legacy_tree",
    "is_hmac_receipt",
    "legacy_config_selects_echo",
    "load_elsie_store_anchor",
    "publish_elsie_staged_file",
    "read_pinned_legacy_artifact",
    "require_elsie_store_anchor",
    "sanitized_legacy_config",
]
