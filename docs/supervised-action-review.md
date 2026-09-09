# Supervised One-Shot Action Review

One-shot mode can receive exact-action confirmations from a trusted local
supervisor while keeping its public stdout as NDJSON. This is an integration
interface, not unattended consent or a new automatic approval policy.

## Terminal Reviewer

From a POSIX terminal, select the operator-controlled reviewer explicitly:

```bash
algo-cli --oneshot --json --approval-mode interactive --review-actions \
  --cwd /path/to/workspace -- "Implement the requested change" > events.ndjson
```

The parent opens its controlling terminal directly and launches the agent with
only the connected client socket inherited. Private action frames are displayed
on that terminal, not copied into `events.ndjson` or stderr. The complete request
is JSON-escaped so embedded control characters cannot clear the screen or forge
terminal prompts. Large actions are not silently truncated for approval.

For each request, type `approve REQUEST_ID` with the displayed fresh request ID;
any other completed line denies that action. There is no session-wide approval.
Stale/reused IDs, malformed or pipelined requests, unavailable terminals,
disconnects, expiry, and cancellation fail closed. A reviewer accepts at most
1,024 distinct requests per invocation. Review and frame transfer share the
channel's 120-second deadline. Ctrl-C terminates and reaps the active child
process group; it does not automatically repeat the action.

This option requires `--oneshot --json --approval-mode interactive` and cannot
be combined with `--approval-fd`. It is a local terminal operator interface,
not independent reviewer attestation or a host-compromise boundary. Automated
PTY fixtures qualify the transport, not real operator approval of coding tasks.

## Launch Contract

The supervisor creates a connected Unix stream `socketpair`, retains one end,
and passes the other explicitly to the Algo process using `pass_fds`:

```python
child_socket, reviewer_socket = socket.socketpair()
process = subprocess.Popen(
    [
        algo_executable, "--oneshot", "--json",
        "--approval-mode", "interactive",
        "--approval-fd", str(child_socket.fileno()),
        "--cwd", reviewed_workspace,
        "--", prompt,
    ],
    stdin=subprocess.DEVNULL,
    pass_fds=(child_socket.fileno(),),
)
child_socket.close()
```

The supervisor must now service requests on `reviewer_socket` and obtain the
operator's decision for each concrete action. This snippet intentionally does
not approve requests. The supervisor must close its endpoint and reap the child
on failure/cancellation; it must not restart a timed-out action blindly.

- This transport requires POSIX and an inherited, connected `AF_UNIX` stream
  descriptor numbered at least 3. TCP, UDP, standard input/output, and unconnected
  sockets are rejected. Windows support is not qualified or silently substituted.
- `interactive` in one-shot mode requires this descriptor. `never` and `auto`
  reject it. Normal interactive terminal approval remains unchanged.
- The child takes ownership of the passed descriptor, marks it non-inheritable,
  and closes it on exit. It is not persisted in configuration, inherited by
  exec'd tools, or exposed as a model-callable tool.
- Missing approval, EOF, malformed input, timeout, and cancellation do not fall
  back to terminal input, session preapproval, or implicit consent.

## Wire Format

Each frame is a four-byte unsigned big-endian byte length followed by UTF-8 JSON.
Read the exact declared size, not one assumed-complete socket read. Request
frames are at most 262,144 bytes; response frames are at most 2,048 bytes.

Requests use `schema=algo-cli-approval-v1`, `type=approval_request`, and contain:

- Fresh `request_id` (128-bit random nonce) and the exact `action_digest`.
- `name`, resolved `target`, runtime-defaulted `arguments`, `effect_class`,
  `capabilities`, and `confirmation_mode`.
- `created_at` and `expires_at` for the reviewer. The child independently applies
  a 120-second monotonic deadline, including frame transmission/reception and
  waiting behind another pending review on the same channel.

The response must contain exactly these five fields:

```json
{
  "schema": "algo-cli-approval-v1",
  "type": "approval_response",
  "request_id": "copy-the-current-request-id",
  "action_digest": "copy-the-current-action-digest",
  "decision": "approve"
}
```

`decision` is exactly `approve` or `deny`. Unknown fields, duplicate keys,
nonfinite values, wrong request identity/digest, malformed frames, and expanded
decisions such as `always` invalidate the channel. A valid denial leaves the
channel usable; the next request has a new nonce. Approval cannot be replayed
for the next call, even when its tool and arguments are identical.

After review, the runtime rechecks the action arguments, workspace, approval and
safe-mode state, and execution guardrails. It then issues/consumes the existing
exact single-use grant and, where required, an action-time confirmation receipt.
Dispatcher cancellation and deadline checks still run before invocation.
Handoff-required actions remain unavailable through this transport.

Baseline read-only observations do not enter the review channel. Each in-flight
preflight receives its own one-use grant, even for overlapping reads of one
target. A consumed, revoked, or expired no-confirmation grant is denied without
prompting; it cannot poison the next action-time review. Directory listing
authority binds the requested filesystem path, not just the working directory.

## Trust and Evidence

The retained endpoint is an authority capability. Give it only to an
operator-controlled reviewer, not to a model, tool subprocess, arbitrary
network client, or automatic "approve everything" loop. A prompt, benchmark
fixture, or desired test result is not approval. Descriptor possession is the
trust basis; this transport does not attest the reviewer identity, isolate a
compromised same-user host, or satisfy independent-review/signing requirements.

Requests contain private paths and action content because review must bind the
actual operation. Keep them on the trusted channel and protected reviewer UI;
do not copy them into public NDJSON, telemetry, plaintext agent memory, or
benchmark artifacts. Public tool output keeps the existing privacy projection.

Deterministic coverage in `tests/test_nathan_approval_channel.py` includes exact
and stale replies, fragmentation, malformed/oversized frames, timeout/EOF,
non-inheritable descriptor lifecycle, mode isolation, authority/argument drift,
real fixture writes, cancellation, and one-shot JSON framing. Runtime and terminal
reviewer tests also exercise ordered concurrent reads followed by shell review,
consumed/revoked/expired baseline grants, and exact directory path scope. Synthetic test
approvals prove the transport contract, not real operator approval of a coding
benchmark. Cross-harness task results remain unqualified until reviewed actions
and independent checkers are actually observed.
