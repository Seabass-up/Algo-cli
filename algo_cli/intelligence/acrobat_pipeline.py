"""B169, B174, B176: Acrobat-derived crash/format/pipeline patterns.

- B169: Structured Crash/Failure Reporter
- B174: Format Mapping/Transformation Tables
- B176: Specialized OCR/Document Pipeline Stages
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
import time


# ── B169: Structured Crash/Failure Reporter ───────────────────────────


@dataclass
class CrashReport:
    """A structured crash report (B169)."""
    process: str
    version: str = ""
    stack_trace: str = ""
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    user_comment: str = ""
    silent_send: bool = False
    timestamp: float = field(default_factory=time.time)
    tool_call_id: str = ""
    model: str = ""
    context_size: int = 0


@dataclass
class CrashReportResult:
    """Result of processing a crash report."""
    stored_locally: bool = False
    uploaded: bool = False
    display_variant: str = "medium"  # "small", "medium", "large"
    error: str = ""


class CrashReporter:
    """Structured crash/failure reporter (B169).

    Tool-call crashes do not kill the agent loop. Reports are stored
    locally and optionally uploaded with user consent.
    """

    def __init__(self) -> None:
        self._reports: list[CrashReport] = []
        self._upload_enabled: bool = False
        self._upload_url: str = ""

    def set_upload_config(self, url: str, enabled: bool = False) -> None:
        self._upload_url = url
        self._upload_enabled = enabled

    def report(self, crash: CrashReport, consent: bool = False) -> CrashReportResult:
        """Process a crash report."""
        self._reports.append(crash)
        result = CrashReportResult(stored_locally=True)

        # Determine display variant based on context size
        if crash.context_size > 100000:
            result.display_variant = "large"
        elif crash.context_size < 10000:
            result.display_variant = "small"
        else:
            result.display_variant = "medium"

        # Upload only with consent and if enabled
        if consent and self._upload_enabled and self._upload_url:
            result.uploaded = True
        return result

    def get_reports(self) -> list[CrashReport]:
        return list(self._reports)

    def report_count(self) -> int:
        return len(self._reports)

    def clear_reports(self) -> None:
        self._reports.clear()

    def latest_report(self) -> CrashReport | None:
        return self._reports[-1] if self._reports else None


class AgentLoopCrashGuard:
    """Wraps tool calls so crashes don't kill the agent loop (B169)."""

    def __init__(self, reporter: CrashReporter | None = None) -> None:
        self._reporter = reporter or CrashReporter()

    def safe_call(self, fn: Callable[..., Any], *args: Any, context: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Call a function safely, catching exceptions and reporting them."""
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            crash = CrashReport(
                process="agent_loop",
                stack_trace=str(e),
                context_snapshot=context or {},
            )
            self._reporter.report(crash)
            return None

    @property
    def reporter(self) -> CrashReporter:
        return self._reporter


# ── B174: Format Mapping/Transformation Tables ────────────────────────


@dataclass
class MappingRule:
    """A single mapping rule (B174)."""
    source_element: str
    target_element: str
    transform: str = "passthrough"  # "passthrough", "rename", "regex", "drop"
    pattern: str = ""
    replacement: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class MappingResult:
    """Result of applying mapping rules."""
    output: dict[str, Any] = field(default_factory=dict)
    unmapped_elements: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class FormatMappingTable:
    """Declarative format mapping/transformation table (B174)."""

    def __init__(self, source_format: str = "pdf", target_format: str = "text") -> None:
        self.source_format = source_format
        self.target_format = target_format
        self._rules: dict[str, MappingRule] = {}

    def add_rule(self, rule: MappingRule) -> None:
        self._rules[rule.source_element] = rule

    def transform(self, content: dict[str, Any]) -> MappingResult:
        """Transform content using mapping rules."""
        result = MappingResult()
        for key, value in content.items():
            rule = self._rules.get(key)
            if not rule:
                result.unmapped_elements.append(key)
                result.output[key] = value  # pass-through with warning
                result.warnings.append(f"unmapped element: {key}")
                continue
            if rule.transform == "drop":
                continue
            elif rule.transform == "rename":
                result.output[rule.target_element] = value
            elif rule.transform == "regex" and rule.pattern:
                import re
                if isinstance(value, str):
                    result.output[rule.target_element] = re.sub(
                        rule.pattern, rule.replacement, value
                    )
                else:
                    result.output[rule.target_element] = value
            else:
                result.output[rule.target_element] = value
            result.warnings.extend(rule.warnings)
        return result

    def all_rules(self) -> list[MappingRule]:
        return list(self._rules.values())


# ── B176: Specialized OCR/Document Pipeline Stages ────────────────────


@dataclass
class PipelineStageResult:
    """Result of a single pipeline stage."""
    stage_name: str
    success: bool = True
    output: Any = None
    skipped: bool = False
    error: str = ""
    duration_ms: float = 0.0


@dataclass
class PipelineResult:
    """Result of running an entire pipeline."""
    stages: list[PipelineStageResult] = field(default_factory=list)
    completed: bool = False
    output: Any = None

    @property
    def failed_stages(self) -> list[PipelineStageResult]:
        return [s for s in self.stages if not s.success and not s.skipped]

    @property
    def skipped_stages(self) -> list[PipelineStageResult]:
        return [s for s in self.stages if s.skipped]


class PipelineStage(Protocol):
    """Protocol for pipeline stages (B176)."""
    name: str
    def process(self, input_data: Any, language: str = "ENU") -> PipelineStageResult: ...


class SimpleStage:
    """A simple pipeline stage with a handler function."""

    def __init__(self, name: str, handler: Callable[[Any, str], Any]) -> None:
        self.name = name
        self._handler = handler

    def process(self, input_data: Any, language: str = "ENU") -> PipelineStageResult:
        start = time.monotonic()
        try:
            output = self._handler(input_data, language)
            return PipelineStageResult(
                stage_name=self.name,
                success=True,
                output=output,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as e:
            return PipelineStageResult(
                stage_name=self.name,
                success=False,
                error=str(e),
                duration_ms=(time.monotonic() - start) * 1000,
            )


class DocumentPipeline:
    """Multi-stage document processing pipeline (B176).

    Each stage is independently replaceable. Stage failure degrades
    gracefully instead of crashing the whole pipeline.
    """

    def __init__(self) -> None:
        self._stages: list[PipelineStage] = []
        self._available_languages: set[str] = {"ENU"}

    def add_stage(self, stage: PipelineStage) -> None:
        self._stages.append(stage)

    def register_language(self, lang: str) -> None:
        self._available_languages.add(lang)

    def run(self, input_data: Any, language: str = "ENU") -> PipelineResult:
        """Run the pipeline. Stages that fail are skipped with warnings."""
        result = PipelineResult()
        current_data = input_data

        for stage in self._stages:
            stage_result = stage.process(current_data, language)
            result.stages.append(stage_result)

            if stage_result.skipped:
                continue

            if not stage_result.success:
                # Stage failed - degrade gracefully, continue with current data
                continue

            current_data = stage_result.output

        result.output = current_data
        result.completed = True
        return result

    def stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    def available_languages(self) -> list[str]:
        return sorted(self._available_languages)


def default_ocr_pipeline() -> DocumentPipeline:
    """Create a default OCR pipeline from Acrobat's PaperCapture iDRS15."""
    pipeline = DocumentPipeline()
    pipeline.register_language("ENU")
    pipeline.register_language("JPN")
    pipeline.register_language("ARA")

    pipeline.add_stage(SimpleStage("preprocess", lambda data, lang: data))
    pipeline.add_stage(SimpleStage("ocr", lambda data, lang: f"ocr:{data}"))
    pipeline.add_stage(SimpleStage("document_output", lambda data, lang: f"out:{data}"))
    return pipeline