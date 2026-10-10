"""Typed advisory decisions; execution and verification remain in the agent runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit


MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 524_288
MAX_TOOL_RESULT_BYTES = 18_000
ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
SCHEMA = "algo.decision-result.v1"


class DecisionError(RuntimeError):
    """A fixed diagnostic code, never a provider body or submitted evidence."""


def encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise DecisionError("invalid_json") from exc


def digest(value: Any) -> str:
    # Order is meaningful to providers, including tie-breaking and ordinal criteria.
    return "sha256:" + hashlib.sha256(encoded(value)).hexdigest()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DecisionError("duplicate_response_key")
        result[key] = value
    return result


def decode(body: bytes) -> dict[str, Any]:
    def invalid(_value: str) -> Any:
        raise DecisionError("nonfinite_response")

    try:
        result = json.loads(body, object_pairs_hook=_object, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DecisionError("invalid_response") from exc
    if type(result) is not dict:
        raise DecisionError("invalid_response")
    return result


def _number(value: Any, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise DecisionError("invalid_answer_number")
    return float(value)


@dataclass(frozen=True)
class Capabilities:
    question_types: tuple[str, ...] = ("choice", "noul", "score")
    max_questions: int = 32
    max_levels: int = 26
    max_score_levels: int = 26
    max_state_bytes: int = 16_000
    max_request_bytes: int = 48_000
    images: bool = False
    score_semantics: str = "probability_weighted_level"
    confidence_semantics: str = "distribution_concentration_not_correctness"
    distributions: bool = True
    confidence_available: bool = True


class DecisionProvider(Protocol):
    name: str
    local: bool
    model: str
    capabilities: Capabilities

    def status(self) -> dict[str, Any]: ...
    def infer(self, state: Any, questions: dict[str, Any], timeout: float) -> dict[str, Any]: ...


_PROVIDER_FACTORIES: dict[str, Callable[[Any], DecisionProvider]] = {}


def register_provider(name: str, factory: Callable[[Any], DecisionProvider]) -> None:
    """Register reviewed host code, not model-emitted code or an arbitrary HF API."""
    if not ID_RE.fullmatch(name) or name in BUILTIN_PROVIDERS or name in _PROVIDER_FACTORIES:
        raise DecisionError("invalid_provider_registration")
    _PROVIDER_FACTORIES[name] = factory


def _endpoint(value: str) -> tuple[Any, bool]:
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError
        _ = url.port
        local = url.hostname == "localhost"
        if not local:
            try:
                local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                local = False
        if not local and url.scheme != "https":
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise DecisionError("invalid_endpoint") from exc
    return url, local


def _http_json(base: str, route: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    url, _local = _endpoint(base)
    kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = kind(url.hostname, url.port, timeout=timeout)
    expired = threading.Event()
    active_socket: list[Any] = []

    def expire() -> None:
        expired.set()
        if active_socket:
            try:
                active_socket[0].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        body = None if payload is None else encoded(payload)
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            raise DecisionError("request_too_large")
        connection.connect()
        active_socket.append(connection.sock)
        if _local and connection.sock is not None and not ipaddress.ip_address(connection.sock.getpeername()[0]).is_loopback:
            raise DecisionError("local_only_forbids_remote_peer")
        connection.request("GET" if payload is None else "POST", route, body,
                           {"Content-Type": "application/json", "Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if expired.is_set():
            raise DecisionError("timeout")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DecisionError("response_too_large")
        if response.status != 200:
            code = "model_unavailable" if response.status == 404 else "provider_error"
            raise DecisionError(code)
        return decode(raw)
    except (OSError, http.client.HTTPException) as exc:
        raise DecisionError("timeout" if expired.is_set() or isinstance(exc, TimeoutError) else "unavailable") from exc
    finally:
        timer.cancel()
        timer.join()
        connection.close()


def _cli_json(argv: list[str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Bound a configured local companion's stdin, stdout, lifetime and diagnostics."""
    body = encoded(payload)
    if len(body) > MAX_REQUEST_BYTES:
        raise DecisionError("request_too_large")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    reply = bytearray()
    errors: list[str] = []
    try:
        with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              cwd=str(Path(argv[0]).parent), env=environment, bufsize=0,
                              start_new_session=os.name == "posix") as process:
            assert process.stdin is not None and process.stdout is not None
            writer, reader = process.stdin, process.stdout

            def send() -> None:
                try:
                    remaining = memoryview(body)
                    while remaining:
                        size = writer.write(remaining)
                        if not size:
                            raise OSError
                        remaining = remaining[size:]
                    writer.close()
                except OSError:
                    errors.append("companion_io_error")

            def receive() -> None:
                try:
                    while True:
                        chunk = reader.read(min(8192, MAX_RESPONSE_BYTES + 1 - len(reply)))
                        if not chunk:
                            return
                        reply.extend(chunk)
                        if len(reply) > MAX_RESPONSE_BYTES:
                            errors.append("response_too_large")
                            process.kill()
                            return
                except OSError:
                    errors.append("companion_io_error")

            workers = [threading.Thread(target=send, daemon=True), threading.Thread(target=receive, daemon=True)]
            for worker in workers:
                worker.start()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                errors.append("timeout")
                process.kill()
                process.wait(timeout=5)
            finally:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for worker in workers:
                    worker.join(timeout=1)
            if any(worker.is_alive() for worker in workers) or errors:
                raise DecisionError(errors[0] if errors else "companion_io_error")
            if process.returncode != 0:
                raise DecisionError("provider_error")
            return decode(bytes(reply))
    except OSError as exc:
        raise DecisionError("companion_unavailable") from exc


def validate_request(state: Any, questions: Any, caps: Capabilities) -> None:
    if type(state) not in (str, dict, list) or not state or (type(state) is str and not state.strip()):
        raise DecisionError("invalid_state")
    if len(encoded(state)) > caps.max_state_bytes:
        raise DecisionError("state_too_large")
    if type(questions) is not dict or not 1 <= len(questions) <= caps.max_questions:
        raise DecisionError("invalid_questions")
    for key, question in questions.items():
        if type(key) is not str or not ID_RE.fullmatch(key) or type(question) is not dict:
            raise DecisionError("invalid_question")
        if set(question) - {"type", "instructions", "criteria"}:
            raise DecisionError("unsupported_question_field")
        if question.get("type") not in caps.question_types:
            raise DecisionError("unsupported_question_type")
        if type(question.get("instructions")) is not str or not question["instructions"].strip():
            raise DecisionError("invalid_instructions")
        criteria = question.get("criteria")
        if question["type"] == "choice":
            if (type(criteria) is not dict or not 2 <= len(criteria) <= caps.max_levels
                    or any(type(label) is not str or not ID_RE.fullmatch(label)
                           or type(description) is not str or not description.strip()
                           for label, description in criteria.items())):
                raise DecisionError("invalid_choice_criteria")
        elif question["type"] == "score":
            if (type(criteria) is not list or not 2 <= len(criteria) <= caps.max_score_levels
                    or any(type(level) is not str or not level.strip() for level in criteria)):
                raise DecisionError("invalid_score_criteria")
        elif criteria is not None:
            if (type(criteria) is not dict or set(criteria) - {"true", "false"}
                    or any(type(value) is not str or not value.strip() for value in criteria.values())):
                raise DecisionError("invalid_noul_criteria")
    if len(encoded({"state": state, "questions": questions})) > caps.max_request_bytes:
        raise DecisionError("request_too_large")


def validate_answers(questions: dict[str, Any], result: dict[str, Any], caps: Capabilities) -> dict[str, Any]:
    answers = result.get("answers")
    if type(answers) is not dict or set(answers) != set(questions):
        raise DecisionError("answer_ids_mismatch")
    for key, question in questions.items():
        answer = answers[key]
        kind = question["type"]
        if type(answer) is not dict or answer.get("type") != kind:
            raise DecisionError("answer_type_mismatch")
        if kind == "noul":
            _number(answer.get("noul"), 0, 1)
            if set(answer) != {"type", "noul"}:
                raise DecisionError("invalid_noul_answer")
            continue
        labels = set(question["criteria"]) if kind == "choice" else {str(i) for i in range(len(question["criteria"]))}
        probabilities = answer.get("probabilities")
        if probabilities is None and not caps.distributions:
            values = None
        else:
            if type(probabilities) is not dict or set(probabilities) != labels:
                raise DecisionError("probability_labels_mismatch")
            values = {label: _number(p, 0, 1) for label, p in probabilities.items()}
            if abs(sum(values.values()) - 1) > 0.02:
                raise DecisionError("probabilities_not_normalized")
        if caps.confidence_available or "confidence" in answer:
            _number(answer.get("confidence"), 0, 1)
        if kind == "choice":
            if type(answer.get("choice")) is not str or answer["choice"] not in labels:
                raise DecisionError("invalid_choice")
            if values is not None and values[answer["choice"]] + 0.001 < max(values.values()):
                raise DecisionError("choice_distribution_mismatch")
            allowed = {"type", "choice", "probabilities", "confidence"}
        else:
            score = _number(answer.get("score"), 0, len(labels) - 1)
            if caps.score_semantics == "ordinal_category" and not score.is_integer():
                raise DecisionError("invalid_ordinal_score")
            if caps.score_semantics == "probability_weighted_level" and values is not None:
                # Rounded probability fields may have accumulated error across levels.
                tolerance = max(0.02, 0.005 * sum(range(len(labels))))
                if abs(score - sum(int(label) * p for label, p in values.items())) > tolerance:
                    raise DecisionError("score_distribution_mismatch")
            if answer.get("legend") != {str(i): level for i, level in enumerate(question["criteria"])}:
                raise DecisionError("score_legend_mismatch")
            allowed = {"type", "score", "legend", "probabilities", "confidence"}
        if set(answer) - allowed:
            raise DecisionError("unsupported_answer_field")
    return answers


class JevProvider:
    name = "jev"
    model = "jev-1.13.0"
    local = False
    capabilities = Capabilities(max_score_levels=10)

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    def status(self) -> dict[str, Any]:
        from .jev_kernel import kernel_status
        return kernel_status(self.cfg)

    def infer(self, state: Any, questions: dict[str, Any], timeout: float) -> dict[str, Any]:
        from .jev_kernel import JevKernelError, _invoke
        if getattr(self.cfg, "jev_kernel_enabled", False) is not True:
            raise DecisionError("jev_disabled")
        try:
            result = _invoke(self.cfg, "decide", {"state": state, "questions": questions},
                             preserve_order=True, timeout_seconds=timeout)
        except JevKernelError as exc:
            raise DecisionError(str(exc)) from exc
        if result.get("ok") is not True:
            error = result.get("error")
            if error in {"provider_unavailable", "provider_timeout", "deadline_exceeded", "provider_http_401", "provider_http_403", "provider_http_429", "provider_http_500", "provider_http_503"}:
                raise DecisionError(error)
            raise DecisionError("jev_unavailable")
        if result.get("model") != self.model:
            raise DecisionError("jev_unavailable")
        result["provider_metadata"] = {key: result[key] for key in (
            "request_sha256", "elapsed_ms", "observed_at", "estimated_cost_usd", "billed_input_tokens", "cached",
        ) if key in result}
        return result


class OllamaProvider:
    def __init__(self, name: str, cfg: Any, settings: dict[str, Any]) -> None:
        self.name, self.cfg = name, cfg
        self.model = settings.get("model", "tev1:4b" if name == "tev1" else name + ":latest")
        self.base = settings.get("base_url", getattr(cfg, "host", "http://localhost:11434"))
        if type(self.model) is not str or not self.model.strip() or len(self.model) > 256 or type(self.base) is not str:
            raise DecisionError("invalid_provider_configuration")
        _url, self.local = _endpoint(self.base)
        self.pin = settings.get("model_digest", "")
        if type(self.pin) is not str or (self.pin and not re.fullmatch(r"[0-9a-f]{64}", self.pin)):
            raise DecisionError("invalid_model_digest")
        self.capabilities = Capabilities(max_questions=64, max_levels=24 if name == "tev1" else 26,
                                         max_score_levels=24 if name == "tev1" else 26,
                                         max_state_bytes=48_000, max_request_bytes=60_000)
        self.source = settings.get("source", {})

    def status(self) -> dict[str, Any]:
        # Readiness never discloses evidence or probes an external configured server.
        if not self.local:
            return {"configured": True, "availability": "not_probed", "local": False}
        return self._inspect()

    def _inspect(self, snapshots: dict | None = None) -> dict[str, Any]:
        def discovery(route: str) -> dict:
            key = (self.base, route)
            if snapshots is not None and key in snapshots:
                return snapshots[key]
            result = _http_json(self.base, route, None, 5)
            if snapshots is not None:
                snapshots[key] = result
            return result

        version = discovery("/api/version").get("version", "")
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", version)
        minimum = (0, 35, 0) if self.name == "tev1" else (0, 35, 1)
        if match is None or tuple(int(part) for part in match.groups()) < minimum:
            raise DecisionError("incompatible_ollama_server")
        tags = discovery("/api/tags").get("models", [])
        if type(tags) is not list or any(type(model) is not dict for model in tags):
            raise DecisionError("invalid_ollama_discovery")
        tag = next((m for m in tags if type(m) is dict and m.get("name") == self.model), None)
        if tag is None:
            raise DecisionError("model_unavailable")
        show = _http_json(self.base, "/api/show", {"model": self.model}, 5)
        capabilities, details = show.get("capabilities"), show.get("details")
        if (type(capabilities) is not list or "decision" not in capabilities
                or type(details) is not dict or details.get("format") != "gguf"):
            raise DecisionError("incompatible_decision_model")
        if self.pin and self.pin != tag.get("digest"):
            raise DecisionError("model_digest_mismatch")
        loaded = discovery("/api/ps").get("models", [])
        if type(loaded) is not list or any(type(model) is not dict for model in loaded):
            raise DecisionError("invalid_ollama_discovery")
        return {"available": True, "loaded": any(m.get("name") == self.model for m in loaded),
                "server_version": version, "model_digest": tag.get("digest"), "local": self.local,
                "source": self.source, "inference": "not_probed_by_status", "images_enabled": False}

    def infer(self, state: Any, questions: dict[str, Any], timeout: float) -> dict[str, Any]:
        before = self._inspect()
        keep_alive = getattr(self.cfg, "decision_keep_alive", "5m")
        if type(keep_alive) not in (str, int) or type(keep_alive) is bool:
            raise DecisionError("invalid_keep_alive")
        result = _http_json(self.base, "/v1/systemone", {
            "model": self.model, "state": state, "questions": questions, "keep_alive": keep_alive,
        }, timeout)
        if result.get("model") != self.model:
            raise DecisionError("response_model_mismatch")
        after = self._inspect()
        if after.get("model_digest") != before.get("model_digest"):
            raise DecisionError("model_changed_during_inference")
        result["provider_metadata"] = {"available": before.get("available"),
                                       "loaded_before": before.get("loaded"), "loaded_after": after.get("loaded"),
                                       "model_digest": before.get("model_digest"),
                                       "server_version": before.get("server_version")}
        return result


class CompanionProvider:
    local = True

    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        self.name = name
        self.cli = settings.get("cli", "")
        self.model = "clm-latest" if name == "clm" else "decider-4b-v2.1"
        self.capabilities = Capabilities(max_levels=26)

    def _path(self) -> Path:
        if type(self.cli) is not str or not self.cli or not Path(self.cli).is_absolute():
            raise DecisionError("companion_not_configured")
        try:
            path = Path(self.cli).resolve(strict=True)
        except OSError as exc:
            raise DecisionError("companion_unavailable") from exc
        if not path.is_file() or not os.access(path, os.X_OK):
            raise DecisionError("companion_unavailable")
        if path.name != {"clm": "clm_local.py", "decider": "decider_local.py"}[self.name]:
            raise DecisionError("incompatible_local_companion")
        return path

    def status(self) -> dict[str, Any]:
        status = _cli_json([str(self._path()), "status"], {}, 10)
        ready = status.get("clm_ready") if self.name == "clm" else status.get("ready")
        return {"configured": True, "ready": ready is True, "inference": "not_probed_by_status",
                "endpoint": status.get("endpoint"), "encoder_ready": status.get("encoder_ready")}

    def infer(self, state: Any, questions: dict[str, Any], timeout: float) -> dict[str, Any]:
        path = self._path()
        request = {"state": state, "questions": questions}
        if self.name == "clm":
            # The stock CLI can truncate encoder text. Use its pinned tokenizer admission first.
            python = path.parent / ".venv" / "bin" / "python"
            bridge = Path(__file__).with_name("decision_clm_bridge.py")
            result = _cli_json([str(python), str(bridge), str(path)], request, timeout)
        else:
            result = _cli_json([str(path), "decide"], request, timeout)
        if result.get("ok") is not True or type(result.get("response")) is not dict:
            raise DecisionError("invalid_companion_response")
        response = result["response"]
        if self.name == "decider":
            runtime = result.get("runtime")
            if type(runtime) is not dict:
                raise DecisionError("invalid_companion_response")
            if runtime.get("model_revision") != "eb5fbdfc9448473ec25e399882912863afbdb70e":
                raise DecisionError("model_revision_mismatch")
            if result.get("input_sha256") != digest(request).removeprefix("sha256:"):
                raise DecisionError("companion_request_mismatch")
        if response.get("model") != self.model:
            raise DecisionError("response_model_mismatch")
        metadata = {key: result[key] for key in ("runtime", "token_budget", "elapsed_ms") if key in result}
        if self.name == "decider":
            extensions = {}
            canonical = {}
            for key, answer in response["answers"].items():
                kind = questions[key]["type"]
                fields = {"type", "noul"} if kind == "noul" else {"type", "choice", "confidence", "probabilities"} if kind == "choice" else {"type", "score", "legend", "confidence", "probabilities"}
                extras = {"certainty", "x_p_max"} if kind == "choice" else {"certainty", "x_p_max", "fit_mass", "level_fit"} if kind == "score" else set()
                if set(answer) - fields - extras:
                    raise DecisionError("unsupported_decider_answer_field")
                supplemental = {field: answer[field] for field in extras if field in answer}
                for field in ("certainty", "x_p_max"):
                    if field in supplemental:
                        _number(supplemental[field], 0, 1)
                if kind == "score" and "level_fit" in supplemental:
                    fits = supplemental["level_fit"]
                    labels = {str(i) for i in range(len(questions[key]["criteria"]))}
                    if type(fits) is not dict or set(fits) != labels:
                        raise DecisionError("invalid_decider_level_fit")
                    total = sum(_number(value, 0, 1) for value in fits.values())
                    mass = _number(supplemental.get("fit_mass"), 0, len(labels))
                    if abs(total - mass) > 0.02:
                        raise DecisionError("invalid_decider_fit_mass")
                canonical[key] = {field: answer[field] for field in fields if field in answer}
                extensions[key] = supplemental
            response["answers"] = canonical
            metadata["native_uncertainty"] = extensions
        response["provider_metadata"] = metadata
        return response


BUILTIN_PROVIDERS = ("jev", "clef", "clef-flash", "clm", "decider", "tev1")


def provider_for(name: str, cfg: Any) -> DecisionProvider:
    settings = getattr(cfg, "decision_provider_settings", {})
    if type(settings) is not dict or type(settings.get(name, {})) is not dict:
        raise DecisionError("invalid_provider_configuration")
    options = settings.get(name, {})
    if name == "jev":
        return JevProvider(cfg)
    if name in {"clef", "clef-flash", "tev1"}:
        return OllamaProvider(name, cfg, options)
    if name in {"clm", "decider"}:
        return CompanionProvider(name, options)
    if name in _PROVIDER_FACTORIES:
        return _PROVIDER_FACTORIES[name](cfg)
    # Extra HF weights require a reviewed System One serving interface and source pin.
    if options.get("adapter") == "ollama-systemone":
        source = options.get("source", {})
        if (type(source) is not dict or any(type(source.get(key)) is not str or not source[key].strip()
                                           for key in ("repository", "revision", "license", "hardware", "interface"))
                or not re.fullmatch(r"[0-9a-f]{40}", source["revision"])
                or not re.fullmatch(r"[0-9a-f]{64}", str(options.get("model_digest", "")))):
            raise DecisionError("unverified_model_registration")
        declared = options.get("capabilities")
        required = {"question_types", "max_questions", "max_levels", "max_score_levels", "max_state_bytes",
                    "max_request_bytes", "score_semantics", "confidence_semantics", "distributions", "confidence_available"}
        if type(declared) is not dict or set(declared) != required:
            raise DecisionError("missing_provider_capabilities")
        types = declared["question_types"]
        if type(types) is not list or not types or set(types) - {"choice", "noul", "score"}:
            raise DecisionError("invalid_provider_capabilities")
        if any(type(declared[key]) is not int or not 1 <= declared[key] <= maximum for key, maximum in (
            ("max_questions", 64), ("max_levels", 26), ("max_score_levels", 26),
            ("max_state_bytes", 48_000), ("max_request_bytes", 60_000),
        )) or any(type(declared[key]) is not bool for key in ("distributions", "confidence_available")):
            raise DecisionError("invalid_provider_capabilities")
        if declared["score_semantics"] not in {"probability_weighted_level", "ordinal_category"}:
            raise DecisionError("invalid_provider_capabilities")
        if type(declared["confidence_semantics"]) is not str or not declared["confidence_semantics"].strip():
            raise DecisionError("invalid_provider_capabilities")
        adapter = OllamaProvider(name, cfg, options)
        adapter.capabilities = Capabilities(**{**declared, "question_types": tuple(types)})
        return adapter
    raise DecisionError("unknown_provider")


def selected_providers(cfg: Any, provider: str | None, task: str) -> list[str]:
    routes = getattr(cfg, "decision_routes", {})
    fallbacks = getattr(cfg, "decision_fallbacks", {})
    if type(routes) is not dict or type(fallbacks) is not dict or type(task) is not str:
        raise DecisionError("invalid_routing_configuration")
    primary = provider if provider is not None else routes.get(task, getattr(cfg, "decision_default_provider", "jev"))
    if type(primary) is not str or not ID_RE.fullmatch(primary):
        raise DecisionError("invalid_provider")
    fallback = fallbacks.get(primary, [])
    if (type(fallback) is not list or len(fallback) > 4 or any(type(p) is not str or not ID_RE.fullmatch(p) for p in fallback)
            or len(set([primary, *fallback])) != len(fallback) + 1):
        raise DecisionError("invalid_fallback_configuration")
    return [primary, *fallback]


def decision_status(provider: str | None = None, cfg: Any = None) -> str:
    """Inspect decision providers, model availability and loading locally; no decision inference."""
    names = [provider] if provider is not None else list(dict.fromkeys([
        *BUILTIN_PROVIDERS, *_PROVIDER_FACTORIES, *getattr(cfg, "decision_provider_settings", {}),
    ]))
    statuses = {}
    for name in names:
        try:
            adapter = provider_for(name, cfg)
            statuses[name] = {"model": adapter.model, "local": adapter.local,
                              "capabilities": asdict(adapter.capabilities), "status": adapter.status()}
        except (DecisionError, OSError, ValueError, TypeError) as exc:
            statuses[name] = {"ok": False, "error": str(exc) if isinstance(exc, DecisionError) else "unavailable"}
    return encoded({"ok": True, "default_provider": getattr(cfg, "decision_default_provider", "jev"),
                    "routing_mode": getattr(cfg, "decision_routing_mode", "direct"),
                    "online_coordinator": "jev", "offline_coordinators": getattr(cfg, "decision_offline_coordinators", []),
                    "coordinator_is_not_conversational_model": True,
                    "automatic_checkpoints": {"enabled": getattr(cfg, "decision_checkpoints_enabled", True),
                                              "local_only": getattr(cfg, "decision_checkpoints_local_only", False),
                                              "review_interval": getattr(cfg, "decision_review_checkpoint_interval", 8),
                                              "maximum_per_task": 3},
                    "routes": getattr(cfg, "decision_routes", {}), "providers": statuses,
                    "advisory_only": True, "inference": "not_probed_by_status"}).decode()


def _policy_answers(answers: dict[str, Any], policy: dict[str, Any] | None) -> dict[str, Any]:
    if policy is None:
        return {}
    if type(policy) is not dict or set(policy) - {"noul_thresholds", "minimum_choice_probability"}:
        raise DecisionError("invalid_decision_policy")
    derived = {}
    thresholds = policy.get("noul_thresholds")
    if thresholds is not None:
        if type(thresholds) is not dict or set(thresholds) != {"false_at_or_below", "true_at_or_above"}:
            raise DecisionError("invalid_decision_policy")
        low = _number(thresholds["false_at_or_below"], 0, 1)
        high = _number(thresholds["true_at_or_above"], 0, 1)
        if low >= high:
            raise DecisionError("invalid_decision_policy")
        for key, answer in answers.items():
            if answer["type"] == "noul":
                p = answer["noul"]
                derived[key] = False if p <= low else True if p >= high else None
    if "minimum_choice_probability" in policy:
        floor = _number(policy["minimum_choice_probability"], 0, 1)
        for key, answer in answers.items():
            if answer["type"] == "choice":
                probabilities = answer.get("probabilities")
                p = probabilities.get(answer["choice"]) if type(probabilities) is dict else None
                derived[key] = answer["choice"] if p is not None and p >= floor else None
    return derived


def check_cancelled(cfg: Any) -> None:
    event = getattr(cfg, "_decision_cancel_event", None)
    if event is not None and event.is_set():
        raise DecisionError("decision_cancelled")


def decide(cfg: Any, state: Any, questions: dict[str, Any], *, provider: str | None = None,
           task: str = "", local_only: bool = True, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    routing: dict[str, Any] = {"mode": "explicit_override" if provider is not None else "direct"}
    try:
        check_cancelled(cfg)
        if type(local_only) is not bool:
            raise DecisionError("invalid_local_only")
        _policy_answers({}, policy)
        request_digest = digest({"state": state, "questions": questions, "provider": provider,
                                 "task": task, "local_only": local_only, "policy": policy})
        mode = getattr(cfg, "decision_routing_mode", "direct")
        if mode not in {"jev", "direct"}:
            raise DecisionError("invalid_routing_mode")
        if provider is None and mode == "jev":
            from .decision_coordinator import coordinate
            provider, local_only, routing = coordinate(cfg, state, questions, task=task, local_only=local_only)
        names = selected_providers(cfg, provider, task)
        timeout = _number(getattr(cfg, "decision_timeout_seconds", 120), 1, 300)
        for name in names:
            check_cancelled(cfg)
            started = time.perf_counter()
            try:
                adapter = provider_for(name, cfg)
                if local_only and not adapter.local:
                    raise DecisionError("local_only_forbids_remote_provider")
                validate_request(state, questions, adapter.capabilities)
                check_cancelled(cfg)
                response = adapter.infer(state, questions, timeout)
                check_cancelled(cfg)
                if response.get("model") != adapter.model:
                    raise DecisionError("response_model_mismatch")
                answers = validate_answers(questions, response, adapter.capabilities)
                completed_attempt = {"provider": name, "status": "validated", "elapsed_ms": round((time.perf_counter()-started)*1000, 3)}
                result = {"schema_version": SCHEMA, "ok": True, "advisory_only": True,
                          "action_authority": False, "real_world_verified": False,
                          "provider": name, "model": adapter.model, "local": adapter.local,
                          "request_sha256": request_digest, "answers": answers,
                          "derived": _policy_answers(answers, policy), "applied_policy": policy,
                          "capabilities": asdict(adapter.capabilities), "attempts": [*attempts, completed_attempt],
                          "usage": response.get("usage", {}),
                          "provider_metadata": response.get("provider_metadata", {})}
                result["routing"] = {**routing, "actual_provider": name}
                if len(encoded(result)) > MAX_TOOL_RESULT_BYTES:
                    raise DecisionError("result_exceeds_runtime_budget")
                return result
            except (DecisionError, OSError, ValueError, TypeError, KeyError) as exc:
                code = str(exc) if isinstance(exc, DecisionError) else "provider_error"
                attempts.append({"provider": name, "status": "failed", "error": code,
                                 "elapsed_ms": round((time.perf_counter()-started)*1000, 3)})
                if code.startswith("invalid_") or code in {"state_too_large", "request_too_large", "unsupported_question_type"}:
                    break
        raise DecisionError(attempts[-1]["error"] if attempts else "unavailable")
    except (DecisionError, TypeError, ValueError) as exc:
        return {"schema_version": SCHEMA, "ok": False, "advisory_only": True,
                "action_authority": False, "real_world_verified": False, "answers": {},
                "error": str(exc) if isinstance(exc, DecisionError) else "invalid_request", "attempts": attempts,
                "routing": getattr(exc, "routing", routing),
                "question_format": "Each question has ONLY type, instructions, criteria. Choice criteria is an option-ID:description object. Noul criteria is an optional true:false description object. Score criteria is an ordered description list. Never use prose as criteria.",
                "next": "Use the main model or fix the explicit provider configuration; no action was authorized."}


def decision_decide(state: str | dict | list, questions: dict, provider: str | None = None, task: str = "",
                    local_only: bool = True, policy: dict | None = None, cfg: Any = None) -> str:
    """Answer bounded questions with a configured decision provider; never execute actions.

    Supply relevant state and questions keyed by IDs. Each question contains type,
    instructions and criteria: choice has explicit option IDs mapped to descriptions;
    noul has an optional true/false description map; score has ordered level descriptions.
    Jev uses the existing TypeSafe companion; clef/clef-flash/tev1 use native Ollama
    System One; clm/decider use their configured local helpers. Omit provider to use
    Jev coordination when enabled, or a direct configured default. Explicit provider
    bypasses coordination only when the user requested that override. Jev routes
    question specifications, not raw evidence; the selected provider answers them.
    Clef-flash/Tev1 coordinate only after confirmed internet loss. local_only=true
    forbids remote routing/inference/fallbacks; online Jev needs local_only=false and normal
    runtime authority. Noul returns a probability, not a boolean. Scores may be
    fractional. An optional policy supplies explicit noul_thresholds or a minimum
    choice probability; uncertain derived results remain null. Retain distributions
    and missing evidence; the main model plans the next step and normal tools must
    obtain authorization and verify it. No images, shell strings or implicit file reads.
    Validation checks response structure, not factual or mathematical correctness.
    Confidence is distribution concentration, not a correctness guarantee. Exact
    arithmetic, probability calculations, graph algorithms and proofs need authorized
    deterministic verification. Never count this tool as mutation verification.
    """
    return encoded(decide(cfg, state, questions, provider=provider, task=task,
                          local_only=local_only, policy=policy)).decode()
