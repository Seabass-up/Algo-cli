# Native Decision Layer

Algo's main model can request a bounded judgment with `decision_decide`, consume
the validated result, and choose its next ordinary tool call. Open-ended planning,
coding, writing and explanations remain with that main model. Decision results
never authorize actions or count as verification after a workspace mutation.

## Coordinated Reasoning Checkpoints

The default coordinated flow is main thinking model -> Jev coordinator -> selected
decision provider -> validated typed answer -> main-model judgment -> normal
authorized action -> actual outcome verification. The main model can request a
decision while planning, before its final answer; it pauses the dependent step
until the tool result arrives. This uses the actual main and Agent Block tool
loops, not token-by-token interception or forwarding of private thought streams.

Jev receives the supplied question specifications and bounded provider/reference
metadata, not the raw evidence. The selected answer provider receives the evidence.
Question specifications themselves can contain sensitive facts: remote routing
still needs explicit `local_only=false` and the ordinary runtime approval.
No automatic conversation or memory-store upload is added.

### Automatic Task And Review Checks

With coordinated reasoning enabled, the runtime submits a bounded task check
before the first main-model request. For reviews it can submit an evidence check
after observed read-only source results, with a fixed per-turn call budget.
The main model does not have to remember to call `decision_decide` first. An
already-preapproved initial check waits at most `decision_initial_wait_seconds`
(default five seconds, configurable 0-30). If still pending, the request explicitly
reports that no validated answer exists and the main model continues. Evidence
follow-ups are polled without waiting, and validated results enter a subsequent
model request, not an in-flight private thinking stream. Advice remains bound to
its original snapshot; newer observations are marked separately. Uncertainty,
denial, pending work and cancellation are different states.

Ordinary configured tasks do not need exact action keywords or perfect spelling
to receive the initial checkpoint. Exact small talk/runtime-version questions,
slash controls and explicit opt-outs are excluded. Task/evidence classification
does not expand caller tool ceilings, signed budgets or provider authority.

Only existing preapproved model calls can use the background worker; interactive
approval stays on the main thread. The worker inherits live revocable authority,
cannot create a grant, and cannot replace a denied call with another route.
Explicit opt-out and signed Agent Block call budgets remain respected. An Agent
Block with a signed run contract receives no unbudgeted automatic model call.
Source evidence is limited to bounded read-only code excerpts; oversized results
are represented by metadata, not silently truncated or uploaded wholesale.

Closing a turn cancels publication and prevents a later specialist dispatch from
that checkpoint. It does not retract a request already transmitted or promise
remote cancellation. The observation worker is daemonized and closing does not
wait for its remote response. Signed Agent Block contracts still receive no
unbudgeted automatic call or partial-answer continuation.

### Bounded Readiness Reuse

Successful local companion readiness metadata can be reused for
`decision_readiness_ttl_seconds` (default five, configurable 0-30; zero disables
reuse). The in-memory cache is configuration-owned, bounded, and not serialized.
CLI configuration, executable identity/permissions, model identity, enablement
and provider settings bind its entries. Failed readiness is not cached. It never
contains task evidence, decisions, credentials, grants or verification receipts.

Ollama version, tags and loaded-model discovery share one snapshot only within a
single routing operation. Each later operation rediscovers installed identities;
inference still performs fresh before/after digest checks. Availability and
cached readiness do not establish inference success or action authority.

Automatic checkpoints honor the configured local-only setting and explicit task
privacy directives. Discussing local-only/offline policy, or quoting a historical
prohibition, does not itself change the request's locality. Packets include the
locality basis. This inference selects a constraint, not permission: online
requests still pass ordinary runtime authorization, and explicitly local-only
requests remain prohibited from remote routing. Natural-language recognition is
bounded; the typed configured privacy setting remains authoritative.

Next-investigation Choice asks one bounded question. When excerpts exist, an
independent relevance Noul is batched with it; relevance is not truth, permission
or testing. References stay in separate kernel/algorithm questions. See
[Jev brain research](jev-brain-runtime-research-20261008.md).

The 2026-10-08 public synthetic review did not ask the main model to use Jev.
Both task and evidence checks reached its actual requests; their observed times
were 1,334.939 ms and 1,238.521 ms. The main Kimi K3 model read and diagnosed the
fixture in 7,457.447 ms total. This was read-only evidence, not a verified repair,
an exhaustive review, or a benchmark of production task quality.

Code removes incompatible/unready providers before Jev chooses. Typed route and
independent fit answers must pass explicit provisional probability, margin and
fit thresholds. Uncertain/no-match responses return to the main thinking model;
they do not dispatch another model until one happens to agree. These thresholds
are conservative operating policy, not calibrated correctness guarantees.

A completed router judgment may defer without answering the original question.
Notices distinguish that from failed inference and name rejected gate values.
Deferred receipts bind to request/policy/model/candidates before consumption.
Distinct new evidence permits a budgeted follow-up; repeated tool/path/content
does not. Authorization denials and malformed replies remain failures.

The coordinator also considers a bounded relevant shortlist of active kernel
metadata and eligible local ALGO patterns. The native catalog reader opens only
the fixed owner catalog (or empty shipped template), never arbitrary model paths
or custom implementation modules. Metadata is redacted and source-bound; a
catalog change during routing invalidates the result. Proposed/unknown patterns
remain references for research, not verified implementations. Recommendations
never activate kernels or authorize actions, and a shortlist is not global
optimality. Public releases contain no owner catalog or custom implementations.

### Online And Offline

Jev is the online coordinator. Clef-flash or Tev1 can coordinate only after
observed disconnection: a native OS disconnected state, or two public routes
returning explicit network-unreachable errors after a transport failure. A
timeout, refused service, authentication error, rate limit, malformed answer or
uncertain vote is not proof of internet loss. Inconclusive reachability stays
unknown. Local-only requests never perform external connectivity probes, remote
routing or remote inference. Offline coordination also restricts answer/fallback
providers to local adapters, even when the original request allowed remote use.
The main conversational model is not automatically replaced when it is offline.

```bash
algo-cli config decision coordinate jev
algo-cli config decision coordinate jev --offline-coordinators tev1 clef-flash
algo-cli config decision coordinate direct
algo-cli config decision select clef-flash
```

`coordinate jev` makes Jev the default coordinator; `select` chooses direct mode.
Explicit per-call providers remain visible user-requested overrides. Task pins
constrain the coordinator's eligible providers instead of granting new authority.
Optional `--candidates`, `--minimum-probability`, `--minimum-margin` and
`--minimum-fit` configure routing. Restart existing sessions to reload the tool
map and prompt; harness refresh does not replace an already loaded runtime.

### Qualification, 2026-10-08

The actual Kimi K3 main loop formulated a Choice, Jev routed it to Clef-flash,
the main model consumed the answer in a subsequent round, and an authorized
read matched the fixture's expected bytes/hash. Only synthetic decision-model
calls had explicit owner preapproval; file mutations and shell actions did not.
An eight-question online development fixture was routed to Jev and matched its
expected answers. Jev also selected a relevant synthetic proposed algorithm
without promoting its implementation status. First failed approval and routing
trials remain evidence, not discarded attempts.
The agent fixture used an explicit task pin to Clef-flash, so its route vote
checks fit and honors that pin; it is not a neutral model-quality comparison.

Clef-flash/Tev1 returned typed judgments in simulated-offline trials but did not
meet the dispatch thresholds. They returned control to the main model instead.
Offline transport/policy is tested; dependable offline coordinator selection,
real physical disconnection, calibration, hosted multi-OS delivery and production
reliability are not qualified. Tev1's upstream license issue remains unresolved.

## Providers

| Provider | Native Interface | Identity And Limits |
| --- | --- | --- |
| `jev` | Existing TypeSafe companion and credential handling | Jev 1.13.0; existing 20-second companion deadline; 32 questions, 16,000 state bytes, 10 Score levels |
| `clef` | Ollama `POST /v1/systemone` | Compatible GGUF decision runner, Ollama 0.35.1+; installed manifest digest checked before/after inference |
| `clef-flash` | Ollama `POST /v1/systemone` | Separate 9B model; same compatibility checks, no substitution with CLM |
| `tev1` | Ollama `POST /v1/systemone` | Explicit experimental Together model; 0.35+; 24-level training-range limit |
| `clm` | Installed local CLM helper | Pinned native renderer/tokenizer admission rejects text above 2,048 tokens before inference; no truncation |
| `decider` | Installed authenticated local Decider helper | Decider 4B v2.1 at `eb5fbdfc9448473ec25e399882912863afbdb70e`; native request/revision checks and type-specific temperatures |

The framework deliberately starts with text and structured state. It does not
read image paths or transmit images. A provider's vision capability is not an
image-access or transmission grant.

The Ollama client and running server are separate compatibility evidence.
SDK 0.6.2 lacks a working decision method, so decision inference uses the native
endpoint directly. Generic chat-to-JSON generation is not used for decisions.
Status separately reports installation, compatibility and model loading; status
never claims inference succeeded. Native HTTP has a bounded response and absolute
deadline, including HTTP/1.0 drip bodies, with no redirects or environment proxies.

## Requests And Semantics

```json
{
  "provider": "clef-flash",
  "local_only": true,
  "state": {"observation": "A dispatched write timed out; target contents are unknown."},
  "questions": {
    "next_step": {
      "type": "choice",
      "instructions": "Which next step addresses the uncertain write?",
      "criteria": {
        "reconcile": "Observe the target before another mutation.",
        "defer": "No suitable observation is available."
      }
    }
  }
}
```

- Choice criteria map explicit option IDs to descriptions. Results retain the
  selected ID, available distribution and provider confidence.
- Noul is the probability of a condition, not a boolean. Optional criteria map
  `true` and `false` to descriptions. Only an explicit caller policy converts it
  to true/false; the gray band remains `null`.
- Score criteria are ordered descriptions. A weighted level can be fractional;
  it is not rounded into an ordinal category. Legend, distribution and native
  confidence are retained.

The validator checks request/answer IDs and types, finite ranges, distributions,
Choice membership and consistency, Score legend and semantics, and model identity.
It refuses invalid inputs before inference and never truncates a validated result
to fit the tool-return budget. Reduce the batch instead. Question order is retained
through the Jev companion, preserving provider tie behavior.

Decider's additional `certainty`, `x_p_max`, `level_fit` and `fit_mass` have separate
native semantics. The adapter validates and retains them in
`provider_metadata.native_uncertainty`; it does not relabel them as correctness
probabilities or discard them to imitate another provider.

## Configuration

The default is Jev, preserving the preexisting integration. Configuration does
not change the conversational model. The following commands make explicit changes:

```sh
algo-cli config decision status
algo-cli config decision configure clef-flash --model clef-flash:latest
algo-cli config decision configure clef --model clef:latest --model-digest DIGEST
algo-cli config decision configure clm --cli /absolute/path/to/clm-local
algo-cli config decision configure decider --cli /absolute/path/to/decider-local
algo-cli config decision select clef-flash
algo-cli config decision route decider --task triage
algo-cli config decision fallback clef-flash --fallback decider clef
algo-cli config decision fallback clef-flash --fallback
```

An empty explicit fallback list clears it. No fallback exists by default.
`decision_timeout_seconds` and `decision_keep_alive` control native/local calls;
Jev retains its existing shorter companion deadline. Configuration updates are
locked and touch only selected decision fields, preserving unknown owner settings.
Use the existing `algo-cli config jev enable --cli PATH` for Jev credentials/setup.

`local_only=true` blocks remote primary and fallback providers before submission.
An external Ollama endpoint is remote, requires HTTPS and explicit remote runtime
authority, and is not probed by the local status operation. Loopback connections
also verify the actual peer before transmitting a body. Jev requires
`local_only=false` plus existing inference/data-egress authority. All attempts and
configured fallback outcomes are visible; none grants execution authority.

No new slash command was added. Coordinated main-loop discovery keeps the decision
entry point available at reasoning checkpoints without evicting the previous tool
budget. Direct mode recognizes explicit decision/Clef-flash intent and permits
later discovery through
`action_search`. Agent Blocks can explicitly include the `decision` tool group;
their declared tool ceilings still apply.

## Additional Model Sources

Hugging Face supplies weights, not one universal inference API. Reviewed host code
can register a `DecisionProvider` factory with `register_provider`; each adapter
owns native serving, supported primitives and probability semantics. Existing
Jev/CLM/Decider adapters are not replaced.

An additional Ollama System One provider can be configured with a reviewed JSON
descriptor using `--descriptor PATH`. It requires a model name/digest, explicit
source repository, immutable 40-character revision, license, hardware, inference
interface and all capability limits/semantics. Runtime checks the served GGUF
decision capability and installed digest. Descriptor source assertions are
operator-reviewed metadata, not an attestation that arbitrary Hub weights were
loaded. Other serving protocols require their own registered adapter; a generic
Hugging Face chat or hosted-inference endpoint is not silently substituted.

Official references: [Ollama System One](https://docs.ollama.com/api/systemone),
[Clef](https://ollama.com/library/clef),
[Clef-flash](https://ollama.com/library/clef-flash),
[TypeSafe API](https://docs.typesafe.ai/api),
[CLM](https://github.com/Contrastive-LM/CLM),
[Decider](https://huggingface.co/Mapika/decider-4b),
[Tev1 native Ollama interface](https://ollama.com/library/tev1).

## Local Qualification, 2026-10-07

`scripts/decision_live_qualification.py` runs the same eight synthetic Algo
questions through every provider. Five providers matched all eight in this
development fixture; CLM matched two. This is not held-out qualification,
calibration or a basis for silently changing task defaults. Cold loading and
concurrent local work are included in wall timings, not isolated model throughput.
Failed first Decider normalization and malformed-agent-question receipts remain
separate from corrected successes.

The actual main agent loop used Kimi K3 through Ollama to formulate a Choice,
call local Clef-flash, consume its validated answer in a later model round, execute
an authorized `read_file`, and verify the observed text/hash. Normal baseline
permissions remained active; no permission override or normal-mode change was
needed. This demonstrates an observation action, not an approved mutation or
external side effect. Exact local paths, source hashes and raw synthetic receipts
are retained privately, not shipped as personal configuration or a kernel catalog.

The installed Tev1 GGUF advertises Apache 2.0, but its upstream Hub card says the
fine-tuned weight license is pending and its metadata has no license field. That
discrepancy remains unresolved; no redistribution eligibility or production
recommendation is claimed. Its native Ollama scoring API is distinct from the
upstream hosted single-letter generation recipe.

## Semantic Reliability Boundary

Schema validation establishes valid types, labels, bounds and distributions;
it does not establish that an answer is factually or mathematically correct.
Choice confidence measures distribution concentration, not calibrated accuracy.
A Noul probability and a Score are advisory estimates, not exact calculators.
Explicit probability-to-boolean policy does not turn an estimate into proof.

The runtime prompt and `decision_decide` guidance require authorized deterministic
checks for exact arithmetic, probability calculations, graph algorithms and
formal deductions. Decision models can compare evidence and computed candidates;
their votes never authorize actions or verify a workspace mutation. If permissions
prevent a required check, the main model must report the result as unverified,
without bypassing approval or lowering coordination thresholds.

`tests/test_decision_semantic_regressions.py` independently enumerates graph paths,
dice outcomes and modular examples. Synthetic confidently wrong replies exercise
the real decision validation and dispatch paths: valid advice remains observable,
but receives no completion credit. `tests/test_prompt_rules.py` checks that the
same guidance reaches interactive and one-shot prompts. These are runtime
regressions, not evidence of improved provider weights, calibration or accuracy.

An isolated live follow-up on 2026-10-09 matched the existing eight synthetic
runtime questions through Jev 1.13.0 and Clef-flash. An actual Kimi K3 agent review
received a validated initial Jev checkpoint before its authorized fixture read.
The later optional background checkpoint was not consumed before the turn ended.
These observations establish operation on that fixture, not mathematical
reliability, held-out accuracy or guaranteed consumption of every background call.
