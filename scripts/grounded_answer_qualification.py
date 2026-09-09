#!/usr/bin/env python3
"""Run eight frozen public-document tasks through the installed Astra harness.

Use the installed interpreter with -I. Required Echo protection stays enabled;
the ordinary tool catalog is retained, but only public harness search/read may
execute. Output is a new directory of content-free qualification receipts.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import fields
import hashlib
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from algo_cli import harness
from algo_cli.config import Config
from algo_cli.evals import grounded_answers as evaluation
from algo_cli.evals.grounded_retrieval import digest, source_snapshot

MODEL = "gpt-6-astra"
ALLOWED = frozenset({"harness_search", "harness_read"})
DEADLINE_SECONDS = 120
MAX_ROUNDS = 8
MAX_CAPTURE_CHARS = 16 * 1024 * 1024
TRANSIENT_FIELDS = frozenset({"messages", "memories", "attempt_ledger", "context_state"})


class BoundedCapture(io.StringIO):
    def write(self, text: str) -> int:
        if self.tell() + len(text) > MAX_CAPTURE_CHARS:
            raise ValueError("qualification_capture_limit")
        return super().write(text)


def settings(cfg: Config) -> dict[str, Any]:
    # Compare in memory only. Neither values nor their hashes enter artifacts.
    return {
        field.name: deepcopy(getattr(cfg, field.name)) for field in fields(cfg) if field.name not in TRANSIENT_FIELDS
    }


def require_protection(cfg: Config) -> None:
    # Each model run overrides protection to required, then restores this setting.
    if not cfg.echo_veil_enabled or cfg.echo_veil_protection not in {"optional", "required"}:
        raise ValueError("required_echo_unavailable")
    if cfg.memory_auto_capture_enabled or cfg.intuition_capture_enabled:
        raise ValueError("automatic_capture_must_be_off")


def freeze_sources() -> tuple[dict[str, Any], frozenset[str]]:
    harness.configure_context_sources(external=False, index_compute_lab=False)
    harness.configure_protected_memory_authority(True)
    index = harness.retrieval_index(protected_memory=True)
    snapshot = source_snapshot(index)
    records = {row["id"]: row for row in index["records"]}
    bodies = {}
    for case in evaluation.CASES:
        for alternatives in case.claims.values():
            for support in alternatives:
                rid = support.record_id
                if rid not in records or harness.is_excluded_from_retrieval(records[rid]):
                    raise ValueError("source_label_unavailable")
                response = harness.read_record(rid, max_chars=64_000, protected_memory=True)
                parts = response.split("\n\n", 2)
                if response.startswith("Error:") or len(parts) != 3:
                    raise ValueError("source_label_unreadable")
                bodies[rid] = parts[2]
        if not evaluation.validate_labels(case, bodies):
            raise ValueError("source_label_changed")
    package = Path(harness.__file__).parent
    code = {
        str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest() for path in package.rglob("*.py")
    }
    return {
        "public_corpus": snapshot,
        "package_python_sources": digest(code),
        "package_python_files": len(code),
        "supporting_bodies": digest(bodies),
        "runner": "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version.split()[0],
    }, frozenset(records)


class ReadOnlyTrace:
    def __init__(self, run_tool, public_ids: frozenset[str]) -> None:
        self.run_tool = run_tool
        self.public_ids = public_ids
        self.rows: list[dict[str, Any]] = []
        self.bodies: dict[str, str] = {}

    def __call__(self, name: str, args: dict[str, Any], cfg: Config) -> str:
        rid = args.get("record_id")
        permitted = name in ALLOWED and (name != "harness_read" or (type(rid) is str and rid in self.public_ids))
        row: dict[str, Any] = {"name": name if name in ALLOWED else "disallowed", "blocked": not permitted}
        self.rows.append(row)
        if not permitted:
            return "Error: qualification permits only public harness_search and harness_read."
        result = self.run_tool(name, args, cfg)
        row["error"] = not isinstance(result, str) or result.startswith("Error:")
        if not isinstance(result, str):
            return "Error: invalid qualification tool result."
        if name == "harness_read":
            row["record_id"] = rid
            parts = result.split("\n\n", 2)
            if not row["error"] and len(parts) == 3:
                body = parts[2]
                previous = self.bodies.get(rid, "")
                # Reads are prefixes of a frozen source. Keep all text actually
                # observed, without inventing quotes by joining different views.
                if body.startswith(previous):
                    self.bodies[rid] = body
                elif not previous.startswith(body):
                    row["error"] = True
                    self.bodies.pop(rid, None)
        else:
            row["ranked_ids"] = [
                line[2:] for line in result.splitlines() if line.startswith("- ") and line[2:] in self.public_ids
            ]
        return result


def event_summary(text: str) -> tuple[str, dict[str, bool], dict[str, Any]]:
    try:
        events = [json.loads(line) for line in text.splitlines()]
        if not all(type(event) is dict for event in events):
            raise ValueError("invalid_event")
    except (ValueError, RecursionError):
        return "", {"valid_event_stream": False}, {}
    last_result = max((i for i, event in enumerate(events) if event.get("type") == "tool_result"), default=-1)
    content = [event.get("text") for event in events[last_result + 1 :] if event.get("type") == "content"]
    final = "".join(content).strip() if all(type(part) is str for part in content) else ""
    done = events[-1] if events and events[-1].get("type") == "done" else {}
    usage = done.get("usage", {})
    if type(usage) is not dict:
        usage = {}
    return (
        final,
        {
            "valid_event_stream": bool(events) and events[0].get("type") == "session_start" and bool(done),
            "model_turn_complete": done.get("status") == "complete",
            "requested_model_observed": bool(events) and events[0].get("model") == MODEL,
            "no_errors_or_denials": not any(event.get("type") in {"error", "tool_denied"} for event in events),
            "tool_results_successful": all(
                event.get("status") == "ok" for event in events if event.get("type") == "tool_result"
            ),
        },
        {
            "rounds": sum(event.get("type") == "model_round" for event in events),
            "usage": {
                key: usage[key]
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if type(usage.get(key)) is int and usage[key] >= 0
            },
        },
    )


def run_case(case: evaluation.AnswerCase, frozen: dict[str, Any], cwd: Path) -> dict[str, Any]:
    from algo_cli import main
    from algo_cli.oliver_oneshot import run_oneshot

    cfg = Config.load()
    require_protection(cfg)
    saved = settings(cfg)
    before, public_ids = freeze_sources()
    if before != frozen["sources"] or evaluation.protocol_digest() != frozen["case_protocol_digest"]:
        raise ValueError("protocol_source_changed")
    overrides = {
        "model": MODEL,
        "model_provider": "chatgpt",
        "cwd": str(cwd),
        "echo_veil_protection": "required",
        "show_thinking": False,
        "memory_auto_capture_enabled": False,
        "intuition_capture_enabled": False,
        "max_tool_iterations": MAX_ROUNDS,
        "session_mode": "execute",
        "code_rag_enabled": False,
        "external_harness_sources_enabled": False,
        "index_compute_lab_auto_inject": False,
        "chatgpt_reasoning_efforts": {**cfg.chatgpt_reasoning_efforts, MODEL: "low"},
    }
    trace = ReadOnlyTrace(main.run_tool, public_ids)
    previous_tool, previous_alarm = main.run_tool, signal.getsignal(signal.SIGALRM)
    expired = False

    def deadline(_signum, _frame):
        nonlocal expired
        expired = True
        raise KeyboardInterrupt

    main.run_tool = trace
    signal.signal(signal.SIGALRM, deadline)
    stream = BoundedCapture()
    started = time.monotonic()
    code = -1
    exception = False
    try:
        signal.alarm(DEADLINE_SECONDS)
        code = run_oneshot(
            prompt=evaluation.prompt(case), approval_mode="never", cfg_overrides=overrides, stream=stream
        )
    except (Exception, KeyboardInterrupt):
        exception = True
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        main.run_tool = previous_tool
    elapsed = round(time.monotonic() - started, 3)
    final, event_checks, metrics = event_summary(stream.getvalue())
    result = evaluation.evaluate_answer(final, case, trace.bodies)
    try:
        after, _ = freeze_sources()
        source_stable = before == after
    except (OSError, ValueError):
        source_stable = False
    result["checks"].update(event_checks)
    result["checks"].update(
        {
            "exit_zero": code == 0 and not exception,
            "deadline_not_expired": not expired,
            "searched_before_reading": bool(trace.rows) and trace.rows[0]["name"] == "harness_search",
            "no_guard_blocks": not any(row["blocked"] for row in trace.rows),
            "no_tool_errors": not any(row.get("error") for row in trace.rows),
            "source_stable": source_stable,
            "configuration_restored": settings(Config.load()) == saved,
        }
    )
    result.update({"passed": all(result["checks"].values()), "seconds": elapsed, "trace": trace.rows, **metrics})
    return result


def write_report(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", choices=[case.name for case in evaluation.CASES], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.flags.isolated or not hasattr(signal, "SIGALRM"):
        parser.error("run the installed interpreter with -I on a POSIX host")
    if not Path(harness.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        parser.error("use a non-editable Algo installation inside this interpreter's prefix")
    if args.worker:
        try:
            # Library diagnostics and raw runtime events are never persisted.
            with redirect_stdout(BoundedCapture()), redirect_stderr(BoundedCapture()):
                frozen = json.loads((args.output / "protocol.json").read_text())
                case = next(case for case in evaluation.CASES if case.name == args.worker)
                result = run_case(case, frozen, args.output / "workspace")
        except (Exception, KeyboardInterrupt):
            result = {"case": args.worker, "passed": False, "stage": "worker_exception", "model_output_included": False}
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result["passed"] else 1
    if args.output.exists() or args.output.is_symlink():
        parser.error("use a new output directory; previous results are never overwritten")
    args.output.mkdir(mode=0o700, parents=True)
    (args.output / "workspace").mkdir(mode=0o700)
    reports = []
    try:
        cfg = Config.load()
        require_protection(cfg)
        saved = settings(cfg)
        sources, _ = freeze_sources()
        frozen = {
            **evaluation.protocol_cases(),
            "case_protocol_digest": evaluation.protocol_digest(),
            "sources": sources,
            "model": MODEL,
            "reasoning": "low",
            "repetitions": 1,
            "deadline_seconds": DEADLINE_SECONDS,
            "process_timeout_seconds": DEADLINE_SECONDS + 30,
            "max_tool_iterations": MAX_ROUNDS,
            "normal_tool_catalog": True,
            "allowed_tools": sorted(ALLOWED),
            "required_echo": True,
            "raw_model_output_persisted": False,
        }
        write_report(args.output / "protocol.json", frozen)
        print(json.dumps({"protocol_digest": digest(frozen), "cases": len(evaluation.CASES)}), flush=True)
        for case in evaluation.CASES:
            print("START " + case.name, flush=True)
            try:
                child = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        "--output",
                        str(args.output.resolve()),
                        "--worker",
                        case.name,
                    ],
                    cwd=args.output / "workspace",
                    capture_output=True,
                    text=True,
                    timeout=DEADLINE_SECONDS + 30,
                )
                report = json.loads(child.stdout)
                if (
                    type(report) is not dict
                    or report.get("case") != case.name
                    or type(report.get("passed")) is not bool
                ):
                    raise ValueError("invalid_worker_report")
                report["process_code"] = child.returncode
                report["passed"] = report["passed"] and child.returncode == 0
            except (subprocess.TimeoutExpired, ValueError):
                report = {
                    "case": case.name,
                    "passed": False,
                    "stage": "worker_failed_or_timed_out",
                    "model_output_included": False,
                }
            write_report(args.output / f"{case.name}.json", report)
            reports.append(report)
            print(
                json.dumps({key: report[key] for key in ("case", "passed", "seconds", "checks") if key in report}),
                flush=True,
            )
            if settings(Config.load()) != saved or freeze_sources()[0] != sources:
                raise ValueError("runtime_configuration_or_source_drift")
        source_stable = sources == freeze_sources()[0]
        summary = {
            "schema": evaluation.SCHEMA,
            "protocol_digest": digest(frozen),
            "source_stable": source_stable,
            "passed": sum(report["passed"] for report in reports),
            "total": len(evaluation.CASES),
            "status": "pass" if source_stable and all(report["passed"] for report in reports) else "fail",
            "model_output_included": False,
        }
    except (Exception, KeyboardInterrupt):
        summary = {
            "schema": evaluation.SCHEMA,
            "status": "fail",
            "stage": "qualification_stopped",
            "completed_cases": len(reports),
            "model_output_included": False,
        }
    write_report(args.output / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
