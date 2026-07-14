from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Intelligence Layer tunables
MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 2000
LOW_QUALITY_AVG_SCORE = 0.60
SHORT_CONTENT_CHARS = 20
LOW_QUALITY_STREAK_THRESHOLD = 3
DEFAULT_MAX_BLOCKS = 500
REINDEX_BATCH_SIZE = 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _date_slug(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).strftime("%Y%m%d")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()[:8]


@contextmanager
def _exclusive_index_lock(index_path: str, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    lock_path = f"{index_path}.lock"
    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with open(lock_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            locking = getattr(msvcrt, "locking")
            lock_nonblocking = getattr(msvcrt, "LK_NBLCK")
            lock_unlock = getattr(msvcrt, "LK_UNLCK")
            while True:
                try:
                    lock_file.seek(0)
                    locking(lock_file.fileno(), lock_nonblocking, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for intuition index lock: {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                locking(lock_file.fileno(), lock_unlock, 1)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for intuition index lock: {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


class IntuitionEngine:
    def __init__(self, index_path: str | None = None, config: dict[str, Any] | None = None):
        if index_path is None:
            try:
                from .config import CONFIG_DIR
                index_path = str(CONFIG_DIR / "intuition_index.json")
            except Exception:
                index_path = os.path.expanduser("~/.algo_cli/intuition_index.json")
        self.index_path = index_path
        self.config = {"recall_enabled": False, "max_blocks": DEFAULT_MAX_BLOCKS}
        if config:
            self.config.update(config)
        self.blocks: Dict[str, Dict] = {}
        self._load_index()
        self._low_quality_count = 0
        self._last_verdict: Dict[str, Any] = {}

    def _load_index(self):
        if not os.path.exists(self.index_path):
            self.blocks = {}
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                blocks = data.get("blocks", {}) if isinstance(data, dict) else {}
                self.blocks = blocks if isinstance(blocks, dict) else {}
        except Exception:
            pass

    def _save_index(self):
        os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
        payload = json.dumps({
            "blocks": self.blocks,
            "updated": _utc_now().isoformat()
        }, indent=2)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(self.index_path)}.",
            suffix=".tmp",
            dir=os.path.dirname(self.index_path),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(8):
                try:
                    os.replace(tmp_path, self.index_path)
                    break
                except PermissionError:
                    if attempt >= 7:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _duplicate_block_id(self, block_type: str, content_hash: str) -> str | None:
        for block_id, block in self.blocks.items():
            metadata = block.get("metadata")
            if (
                block.get("type") == block_type
                and isinstance(metadata, dict)
                and metadata.get("content_hash") == content_hash
            ):
                return block_id
        return None

    def index_blocks(self, blocks: List[Dict[str, Any]]) -> list[str]:
        """Index multiple blocks under one lock and one atomic write."""
        if not blocks:
            return []
        now = _utc_now()
        records: list[dict[str, Any]] = []
        for block in blocks:
            block_id = block.get("id") or f"{block.get('type', 'general')}:{_date_slug(now)}:{_content_hash(str(block.get('content', '')))}"
            records.append({
                "id": block_id,
                "content": block.get("content", ""),
                "type": block.get("type", "general"),
                "timestamp": block.get("timestamp") or now.isoformat(),
                "metadata": block.get("metadata", {}),
                "embedding": block.get("embedding"),
            })

        indexed_ids: list[str] = []
        with _exclusive_index_lock(self.index_path):
            self._load_index()
            for record in records:
                metadata = record.get("metadata")
                content_hash = metadata.get("content_hash") if isinstance(metadata, dict) else None
                duplicate_id = (
                    self._duplicate_block_id(str(record.get("type", "general")), str(content_hash))
                    if content_hash
                    else None
                )
                if duplicate_id is not None:
                    indexed_ids.append(duplicate_id)
                    continue
                block_id = str(record["id"])
                self.blocks[block_id] = record
                indexed_ids.append(block_id)
            self._prune()
            self._save_index()
        return indexed_ids

    def index_block(self, block: Dict[str, Any]):
        return self.index_blocks([block])[0]

    def _prune(self) -> None:
        max_blocks = max(1, int(self.config.get("max_blocks", DEFAULT_MAX_BLOCKS)))
        if len(self.blocks) <= max_blocks:
            return
        ordered = sorted(
            self.blocks.items(),
            key=lambda item: str(item[1].get("timestamp", "")),
            reverse=True,
        )
        self.blocks = dict(ordered[:max_blocks])

    def capture_block(
        self,
        block_type: str,
        content: str,
        *,
        source: str,
        embed_fn: Any | None = None,
        embedding_model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Capture a reusable block, embedding at capture time when possible."""
        clean_type = (block_type or "general").strip().lower() or "general"
        clean_content = (content or "").strip()
        if not clean_content:
            raise ValueError("Intuition block content was empty.")

        existing_hash = _content_hash(clean_content)
        duplicate_id = self._duplicate_block_id(clean_type, existing_hash)
        if duplicate_id is not None:
            # Re-check under the cross-process lock. Another process may have
            # forgotten the block since this engine last loaded its index.
            with _exclusive_index_lock(self.index_path):
                self._load_index()
                duplicate_id = self._duplicate_block_id(clean_type, existing_hash)
            if duplicate_id is not None:
                return duplicate_id

        meta = dict(metadata or {})
        meta.update({"source": source, "content_hash": existing_hash})
        embedding: list[float] | None = None
        if embed_fn is not None:
            try:
                vectors = embed_fn([clean_content])
                if vectors:
                    embedding = [float(value) for value in vectors[0]]
                    meta["embedding_status"] = "ready"
                    if embedding_model:
                        meta["embedding_model"] = embedding_model
            except Exception as exc:
                logger.debug("Intuition capture embedding failed: %s", exc)
                meta["embedding_status"] = "pending"
                if embedding_model:
                    meta["embedding_model"] = embedding_model
        else:
            meta["embedding_status"] = "pending"
            if embedding_model:
                meta["embedding_model"] = embedding_model

        now = _utc_now()
        block_id = f"{clean_type}:{_date_slug(now)}:{existing_hash}"
        return self.index_block(
            {
                "id": block_id,
                "type": clean_type,
                "content": clean_content,
                "timestamp": now.isoformat(),
                "metadata": meta,
                "embedding": embedding,
            }
        )

    def status(self) -> dict[str, Any]:
        embedded = 0
        pending = 0
        by_type: dict[str, int] = {}
        for block in self.blocks.values():
            by_type[str(block.get("type", "general"))] = by_type.get(str(block.get("type", "general")), 0) + 1
            if block.get("embedding"):
                embedded += 1
            else:
                pending += 1
        return {
            "index_path": self.index_path,
            "block_count": len(self.blocks),
            "embedded": embedded,
            "pending": pending,
            "by_type": by_type,
            "recall_enabled": bool(self.config.get("recall_enabled", False)),
            "max_blocks": int(self.config.get("max_blocks", DEFAULT_MAX_BLOCKS)),
        }

    def list_blocks(self) -> list[dict[str, Any]]:
        return sorted(
            (dict(block) for block in self.blocks.values()),
            key=lambda block: str(block.get("timestamp", "")),
            reverse=True,
        )

    def forget_block(self, block_id: str) -> dict[str, Any] | None:
        with _exclusive_index_lock(self.index_path):
            self._load_index()
            removed = self.blocks.pop(block_id, None)
            if removed is not None:
                self._save_index()
        return removed

    def reindex(
        self,
        embed_fn: Any,
        embedding_model: str | None = None,
        *,
        batch_size: int = REINDEX_BATCH_SIZE,
    ) -> dict[str, Any]:
        if embed_fn is None:
            return {"ok": False, "reason": "no_embed_fn", "updated": 0, "total": len(self.blocks)}
        with _exclusive_index_lock(self.index_path):
            self._load_index()
            snapshot = {block_id: dict(block) for block_id, block in self.blocks.items()}
        updated_blocks: dict[str, dict[str, Any]] = {}
        updated = 0
        failed = 0
        pending: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
        for block_id, block in snapshot.items():
            content = str(block.get("content", "")).strip()
            metadata = block.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            else:
                metadata = dict(metadata)
            block["metadata"] = metadata
            if not content:
                failed += 1
                metadata["embedding_status"] = "failed"
                updated_blocks[block_id] = block
                continue
            pending.append((block_id, block, metadata, content))

        batch_size = max(1, int(batch_size))
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            try:
                vectors = embed_fn([content for _block_id, _block, _metadata, content in batch])
                if len(vectors) != len(batch):
                    raise ValueError("embedding response count did not match batch")
            except Exception as exc:
                logger.debug("Intuition reindex batch failed: %s", exc)
                for block_id, block, metadata, _content in batch:
                    metadata["embedding_status"] = "failed"
                    failed += 1
                    updated_blocks[block_id] = block
                continue
            for (block_id, block, metadata, _content), vector in zip(batch, vectors):
                try:
                    block["embedding"] = [float(value) for value in vector]
                except (TypeError, ValueError) as exc:
                    logger.debug("Intuition reindex vector conversion failed for %s: %s", block_id, exc)
                    metadata["embedding_status"] = "failed"
                    failed += 1
                else:
                    metadata["embedding_status"] = "ready"
                    if embedding_model:
                        metadata["embedding_model"] = embedding_model
                    updated += 1
                updated_blocks[block_id] = block
        with _exclusive_index_lock(self.index_path):
            self._load_index()
            applied = 0
            for block_id, block in updated_blocks.items():
                current = self.blocks.get(block_id)
                if current is None:
                    continue
                current["embedding"] = block.get("embedding")
                current_meta = current.get("metadata")
                if not isinstance(current_meta, dict):
                    current_meta = {}
                    current["metadata"] = current_meta
                current_meta.update(block.get("metadata") or {})
                applied += 1
            self._save_index()
            total = len(self.blocks)
        return {
            "ok": failed == 0,
            "updated": updated,
            "failed": failed,
            "applied": applied,
            "total": total,
            "embedding_model": embedding_model,
        }

    def sync_from_squeezer(self, squeeze_engine: Any) -> int:
        if not squeeze_engine:
            return 0
        try:
            get_blocks = getattr(squeeze_engine, "get_new_blocks", None)
            if not get_blocks:
                return 0
            new_blocks = get_blocks() or []
            return len(self.index_blocks(list(new_blocks)))
        except Exception:
            return 0

    # ---------- Intelligence Layer ----------

    def _check_guardrails(self, query: str) -> Tuple[bool, str]:
        """Pre-recall validation. Returns (ok, reason)."""
        if not isinstance(query, str):
            return False, "query_not_string"
        stripped = query.strip()
        if not stripped:
            return False, "query_empty"
        if len(stripped) < MIN_QUERY_LEN:
            return False, "query_too_short"
        if len(stripped) > MAX_QUERY_LEN:
            return False, "query_too_long"
        if not self.blocks:
            return False, "no_blocks_indexed"
        return True, "ok"

    def _ground_block(self, block_id: str) -> Dict[str, Any] | None:
        """Confirm a block still exists and has minimum valid metadata."""
        block = self.blocks.get(block_id)
        if not block:
            return None
        content = block.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return None
        if not block.get("timestamp"):
            return None
        return block

    def _verify_results(self, results: List[Dict]) -> Dict[str, Any]:
        """Score recall quality and detect failure modes."""
        if not results:
            return {"ok": False, "reason": "no_results", "avg_score": 0.0}

        avg_score = sum(r["score"] for r in results) / len(results)

        seen: set[str] = set()
        duplicates = 0
        short = 0
        for r in results:
            content = r.get("content", "")
            digest = hashlib.sha1(content.strip().lower().encode("utf-8")).hexdigest()
            if digest in seen:
                duplicates += 1
            seen.add(digest)
            if len(content.strip()) < SHORT_CONTENT_CHARS:
                short += 1

        low_quality = (
            avg_score < LOW_QUALITY_AVG_SCORE
            or duplicates > 0
            or short == len(results)
        )
        return {
            "ok": not low_quality,
            "avg_score": round(avg_score, 3),
            "duplicates": duplicates,
            "short_content_hits": short,
            "count": len(results),
        }

    # ---------- Recall ----------

    def recall(
        self,
        query: str,
        top_k: int = 3,
        min_score: float = 0.65,
        *,
        enabled: bool | None = None,
        embed_fn: Any | None = None,
    ) -> List[Dict]:
        recall_enabled = self.config.get("recall_enabled", False) if enabled is None else enabled
        if not recall_enabled:
            logger.debug("Intuition recall disabled.")
            self._last_verdict = {"ok": False, "reason": "recall_disabled"}
            return []
        if embed_fn is None:
            self._last_verdict = {"ok": False, "reason": "no_embed_fn"}
            return []
        ok, reason = self._check_guardrails(query)
        if not ok:
            self._last_verdict = {"ok": False, "reason": reason}
            return []

        try:
            query_vectors = embed_fn([query])
        except Exception as exc:
            logger.debug("Intuition query embedding failed: %s", exc)
            self._last_verdict = {"ok": False, "reason": "query_embedding_failed"}
            return []
        if not query_vectors:
            self._last_verdict = {"ok": False, "reason": "query_embedding_missing"}
            return []
        query_vec = [float(value) for value in query_vectors[0]]

        results: List[Dict] = []
        for block_id, _ in self.blocks.items():
            block = self._ground_block(block_id)
            if not block:
                continue
            embedding = block.get("embedding")
            if not isinstance(embedding, list):
                continue

            score = round(_cosine_similarity(query_vec, [float(value) for value in embedding]), 3)
            if score < min_score:
                continue

            results.append({
                "id": block_id,
                "content": block.get("content", ""),
                "type": block.get("type", "general"),
                "score": score,
                "timestamp": block.get("timestamp"),
                "metadata": block.get("metadata", {}),
            })

        sorted_results = sorted(results, key=lambda x: x["score"], reverse=True)[:top_k]

        verdict = self._verify_results(sorted_results)
        self._last_verdict = verdict
        if not verdict.get("ok"):
            self._low_quality_count += 1
        else:
            self._low_quality_count = 0

        return sorted_results

    def format_for_injection(self, blocks: List[Dict]) -> str:
        lines = ["## Relevant Context (from memory)"]
        for block in blocks:
            block_type = str(block.get("type", "note")).upper()
            block_id = block.get("id", "?")
            content = str(block.get("content", ""))
            score = float(block.get("score", 0.0) or 0.0)
            snippet = content[:300].replace("\n", " ").strip()
            if len(content) > 300:
                snippet += "..."
            if snippet:
                lines.append(f"- [{block_type}] `{block_id}` ({score:.2f}): {snippet}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def run(self, query: str, *, enabled: bool | None = None, embed_fn: Any | None = None) -> str | None:
        """Public adapter called by the chat loop.
        """
        blocks = self.recall(query, enabled=enabled, embed_fn=embed_fn)
        if not blocks:
            return None
        injection = self.format_for_injection(blocks)
        if not injection:
            return None
        return f"{query}\n\n{injection}"

    def should_introspect(self) -> bool:
        """Returns True if recall quality has been consistently low."""
        return self._low_quality_count >= LOW_QUALITY_STREAK_THRESHOLD

    def last_verdict(self) -> Dict[str, Any]:
        """Return verification details from the most recent recall."""
        return dict(self._last_verdict)
