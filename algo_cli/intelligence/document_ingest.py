"""B34. Converter Pipeline + Markdown Normalization (MarkItDown Pattern).

Converts arbitrary file formats into token-efficient Markdown before RAG or
agent analysis.  Uses a registry of format-specific converters with narrow
readers (local file, stream, URL) and security boundaries.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

# ── data types ────────────────────────────────────────────────────────


@dataclass
class ConversionResult:
    """Outcome of a single conversion."""

    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class UnsupportedFormat(Exception):
    """No registered converter can handle the given file."""


# ── converter base ────────────────────────────────────────────────────


class Converter:
    """Base converter — override ``supports`` and ``convert_local``."""

    def supports(self, path: Path) -> bool:
        raise NotImplementedError

    def convert_local(self, path: Path) -> ConversionResult:
        raise NotImplementedError

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        raise NotImplementedError


# ── built-in converters ───────────────────────────────────────────────


class CSVConverter(Converter):
    """CSV → Markdown table."""

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".csv", ".tsv"}

    def convert_local(self, path: Path) -> ConversionResult:
        delim = "\t" if path.suffix.lower() == ".tsv" else ","
        with open(path, encoding="utf-8", newline="") as fh:
            return self._from_reader(csv.reader(fh, delimiter=delim), path)

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        text = data.decode("utf-8", errors="replace")
        return self._from_reader(csv.reader(io.StringIO(text)), Path(name))

    @staticmethod
    def _from_reader(rows: Iterable[list[str]], path: Path) -> ConversionResult:
        lines = list(rows)
        if not lines:
            return ConversionResult(markdown="", metadata={"source": str(path)})
        header = lines[0]
        md = "| " + " | ".join(header) + " |\n"
        md += "|" + "---|" * len(header) + "\n"
        for row in lines[1:]:
            # pad / truncate to header length
            cells = list(row) + [""] * (len(header) - len(row))
            md += "| " + " | ".join(cells[: len(header)]) + " |\n"
        return ConversionResult(
            markdown=md,
            metadata={"source": str(path), "rows": len(lines) - 1, "columns": len(header)},
        )


class JSONConverter(Converter):
    """JSON → fenced code block."""

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".json"

    def convert_local(self, path: Path) -> ConversionResult:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return self._from_data(data, path)

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        return self._from_data(json.loads(data), Path(name))

    @staticmethod
    def _from_data(data: Any, path: Path) -> ConversionResult:
        md = f"```json\n{json.dumps(data, indent=2)}\n```\n"
        return ConversionResult(
            markdown=md,
            metadata={"source": str(path), "type": type(data).__name__},
        )


class XMLConverter(Converter):
    """XML → indented text inside a code fence."""

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".xml", ".svg", ".xsd", ".xsl"}

    def convert_local(self, path: Path) -> ConversionResult:
        tree = ElementTree.parse(path)
        return self._from_tree(tree, path)

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        return self._from_tree(ElementTree.fromstring(data), Path(name))

    @staticmethod
    def _from_tree(tree: Any, path: Path) -> ConversionResult:
        if isinstance(tree, ElementTree.ElementTree):
            root = tree.getroot()
        else:
            root = tree
        text = ElementTree.tostring(root, encoding="unicode")
        md = f"```xml\n{text}\n```\n"
        return ConversionResult(
            markdown=md,
            metadata={"source": str(path), "root_tag": root.tag},
        )


class HTMLConverter(Converter):
    """Strip HTML tags → plain text with basic structure preservation."""

    _TAG_RE = re.compile(r"<[^>]+>")
    _BLOCK_RE = re.compile(r"</(p|div|h[1-6]|li|tr|br)\s*>", re.I)

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".html", ".htm"}

    def convert_local(self, path: Path) -> ConversionResult:
        html = path.read_text(encoding="utf-8", errors="replace")
        return self._from_html(html, path)

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        return self._from_html(data.decode("utf-8", errors="replace"), Path(name))

    def _from_html(self, html: str, path: Path) -> ConversionResult:
        # insert newlines after block-level closes
        text = self._BLOCK_RE.sub("\n", html)
        text = self._TAG_RE.sub("", text)
        # collapse excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        md = f"{text}\n"
        return ConversionResult(
            markdown=md,
            metadata={"source": str(path), "chars": len(text)},
        )


class TextConverter(Converter):
    """Plain text / Markdown passthrough."""

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".txt", ".md", ".markdown", ".rst", ".log"}

    def convert_local(self, path: Path) -> ConversionResult:
        text = path.read_text(encoding="utf-8", errors="replace")
        return ConversionResult(
            markdown=text,
            metadata={"source": str(path), "chars": len(text)},
        )

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        text = data.decode("utf-8", errors="replace")
        return ConversionResult(
            markdown=text,
            metadata={"source": name, "chars": len(text)},
        )


class ZipConverter(Converter):
    """Traverse ZIP archives and convert each entry with a sub-registry."""

    def __init__(self, sub_registry: "ConverterRegistry", max_files: int = 50, max_bytes: int = 5_000_000):
        self.sub_registry = sub_registry
        self.max_files = max_files
        self.max_bytes = max_bytes

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".zip"

    def convert_local(self, path: Path) -> ConversionResult:
        parts: list[str] = []
        warnings: list[str] = []
        total_bytes = 0
        file_count = 0
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if file_count >= self.max_files:
                    warnings.append(f"max_files={self.max_files} reached, remaining entries skipped")
                    break
                if total_bytes + info.file_size > self.max_bytes:
                    warnings.append(f"max_bytes={self.max_bytes} reached, remaining entries skipped")
                    break
                data = zf.read(info)
                total_bytes += len(data)
                file_count += 1
                inner_path = Path(info.filename)
                try:
                    sub = self.sub_registry.convert_local(inner_path, _data_override=data)
                except UnsupportedFormat:
                    parts.append(f"## {info.filename}\n\n*(binary or unsupported)*\n")
                    continue
                parts.append(f"## {info.filename}\n\n{sub.markdown}")
        return ConversionResult(
            markdown="\n\n".join(parts),
            metadata={"source": str(path), "entries": file_count, "total_bytes": total_bytes},
            warnings=warnings,
        )

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        # write to temp, delegate
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
            tf.write(data)
            tf_path = Path(tf.name)
        try:
            return self.convert_local(tf_path)
        finally:
            os.unlink(tf_path)


# ── registry ──────────────────────────────────────────────────────────


class ConverterRegistry:
    """Ordered registry of converters with narrow reader methods."""

    def __init__(self) -> None:
        self._converters: list[Converter] = []

    def register(self, converter: Converter) -> None:
        self._converters.append(converter)

    def _find(self, path: Path) -> Converter:
        for c in self._converters:
            if c.supports(path):
                return c
        raise UnsupportedFormat(f"No converter for {path.suffix}")

    def convert_local(self, path: Path, _data_override: bytes | None = None) -> ConversionResult:
        converter = self._find(path)
        if _data_override is not None and hasattr(converter, "convert_stream"):
            return converter.convert_stream(_data_override, str(path))
        return converter.convert_local(path)

    def convert_stream(self, data: bytes, name: str = "") -> ConversionResult:
        path = Path(name or "stream")
        converter = self._find(path)
        return converter.convert_stream(data, name)

    def supported_suffixes(self) -> set[str]:
        suffixes: set[str] = set()
        for c in self._converters:
            # probe common suffixes
            for suf in [".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".txt", ".md", ".zip"]:
                if c.supports(Path(f"file{suf}")):
                    suffixes.add(suf)
        return suffixes


def default_registry() -> ConverterRegistry:
    """Build a registry with all built-in converters."""
    reg = ConverterRegistry()
    reg.register(CSVConverter())
    reg.register(JSONConverter())
    reg.register(XMLConverter())
    reg.register(HTMLConverter())
    reg.register(TextConverter())
    # zip needs a sub-registry without itself to avoid recursion
    sub = ConverterRegistry()
    sub.register(CSVConverter())
    sub.register(JSONConverter())
    sub.register(XMLConverter())
    sub.register(HTMLConverter())
    sub.register(TextConverter())
    reg.register(ZipConverter(sub))
    return reg