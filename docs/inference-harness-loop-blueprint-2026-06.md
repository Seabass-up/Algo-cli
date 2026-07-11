# Inference Harness Loop Blueprint

Date: 2026-06-12

This note captures how EOSD, ALK-delta, AVEE, and CPDI should be wired into the Algo CLI agent loop.

## Architecture

These algorithms live below the normal hosted-API harness line.

- Algo CLI agent loop: orchestration, Agent Blocks, policy, routing metadata, telemetry display, and fallback decisions.
- Inference gateway: one OpenAI/Anthropic-compatible facade that accepts agent requests, chooses hosted or local routes, and returns normalized provenance.
- Local inference engine: vLLM/SGLang fork or adapter where decode, KV, forward-pass, and scheduler changes are implemented.

The agent loop should not pretend it can implement EOSD, ALK-delta, AVEE, or CPDI through an ordinary hosted model call. It should guide implementation plans toward the local engine and expose only stable policy knobs upward.

## Algorithm Placement

EOSD belongs in the speculative decoding loop. Before each draft round, compute prefix-visible draft entropy, map it through a calibrated `beta_hat = g(H)`, choose the speculative depth `k`, and record realized `k`, acceptance rate, and tokens/sec. EOSD remains lossless when `k` is chosen before drafting from prefix information.

ALK-delta belongs in the KV cache manager or PagedAttention layer. The engine stores anchors, low-rank factors, and sparse residuals, then exposes a per-request certified attention-output perturbation bound. The harness consumes that scalar for policy and fallback.

AVEE belongs in the model forward pass. It requires calibrated auxiliary exit heads and an e-process accumulator. The harness supplies `risk_delta`, while the engine returns exit-layer telemetry and measured agreement.

CPDI belongs in the serving scheduler. The harness supplies `priority_weight`; the scheduler allocates prefill/decode work using normalized remaining work and returns queue and latency telemetry.

## Gateway Contract

Request metadata should stay small:

- `model`
- `messages`
- `risk_delta`
- `priority_weight`
- `lossless_required`

Response provenance should include:

- engine route and fallback reason
- realized speedup
- EOSD acceptance stats
- ALK-delta KV certificate
- AVEE exit histogram
- CPDI queue and latency metrics

## Build Order

1. Stand up a vanilla local-engine route behind the gateway and verify routing, telemetry, and hosted fallback.
2. Add EOSD first because deterministic exact-output regression tests can check the losslessness contract.
3. Add CPDI scheduler experiments next because they are mostly scheduler logic.
4. Add ALK-delta and AVEE last because they require cache/kernel work, calibration, and stricter quality validation.

## Agent Loop Integration

`algo_cli.inference_harness` detects inference-engine tasks and injects this contract into each Agent Block system prompt. Normal agent tasks are unaffected.
