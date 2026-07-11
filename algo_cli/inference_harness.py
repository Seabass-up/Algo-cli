"""Inference-engine guidance injected into relevant Agent Block runs."""

from __future__ import annotations

import re


_INFERENCE_TERMS = (
    "eosd",
    "entropy-optimal speculative depth",
    "speculative decoding",
    "draft length",
    "alk",
    "alk-delta",
    "alk-d",
    "low-rank kv",
    "kv cache",
    "certified attention",
    "avee",
    "early exit",
    "e-process",
    "cpdi",
    "prefill",
    "decode interleaving",
    "vllm",
    "sglang",
    "inference gateway",
    "inference engine",
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def should_inject(task: str) -> bool:
    """Return True when a task needs inference-harness architecture guidance."""

    lowered = (task or "").lower()
    if not lowered.strip():
        return False
    if any(term in lowered for term in _INFERENCE_TERMS):
        return True
    words = set(_WORD_RE.findall(lowered))
    return bool({"decoding", "scheduler", "kv", "attention"} & words and {"harness", "agent", "loop"} & words)


def context_block() -> str:
    """Prompt block for routing inference-level algorithm work correctly."""

    return """## Inference Harness Integration Contract
EOSD, ALK-delta, and AVEE are inference-engine changes, not ordinary API-layer agent behavior. CPDI is a serving scheduler change. Wire them through a two-tier contract:

- Harness / agent loop: orchestration, policies, request metadata, telemetry consumption, fallback decisions.
- Inference gateway: OpenAI/Anthropic-compatible facade that routes to hosted APIs or a local engine and normalizes provenance.
- Local engine: vLLM/SGLang fork or adapter where decode loop, KV manager, model forward pass, and scheduler changes live.

Algorithm placement:
- EOSD: decode loop before each speculative draft round. Choose k from prefix-observed entropy via calibrated beta_hat = g(H). Expose realized k, acceptance rate, tokens/sec. It is the only lossless optimization here when k is chosen before drafting.
- ALK-delta: KV cache manager / PagedAttention layer. Keep anchors, low-rank factors, sparse residuals, and expose a per-request certified attention-output perturbation bound. Harness policy may fall back or disable compression when the bound is too high.
- AVEE: model forward pass with calibrated auxiliary exit heads. Harness supplies risk_delta; engine returns exit-layer telemetry. Use only for open-weight models with calibration.
- CPDI: serving scheduler. Harness supplies priority_weight; scheduler allocates prefill/decode work using normalized remaining work and emits latency/queue telemetry.

Gateway request fields should stay small: model alias, messages, risk_delta, priority_weight, and lossless_required. Gateway response provenance should include realized speedup, EOSD acceptance stats, KV certificate, AVEE exit histogram, scheduler queue/latency data, engine route, and fallback reason when used.

Build order for implementation plans:
1. Stand up a vanilla local-engine route behind the gateway and prove routing, telemetry, and hosted fallback.
2. Add EOSD first with exact-output regression tests against fixed speculative sampling for deterministic seeds.
3. Add CPDI scheduler experiments.
4. Add ALK-delta and AVEE last because they require cache/kernel work and calibration.

Do not claim these algorithms can be implemented through hosted Anthropic/OpenAI API calls alone. For user-facing or high-stakes tasks, prefer hosted or uncompressed/full-depth routes unless the local certificate and risk settings satisfy policy."""
