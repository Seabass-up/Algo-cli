"""B66. Cross-Source Analysis.

Sentiment, consensus, disagreements across sources.
Source: sibyl pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
import re


class Sentiment(Enum):
    POSITIVE = auto()
    NEUTRAL = auto()
    NEGATIVE = auto()
    MIXED = auto()


@dataclass
class SourcePosition:
    source: str
    title: str
    position: str  # summary of what this source says
    sentiment: Sentiment = Sentiment.NEUTRAL
    key_points: list[str] = field(default_factory=list)


@dataclass
class CrossSourceAnalysis:
    question: str
    positions: list[SourcePosition] = field(default_factory=list)
    consensus: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    overall_sentiment: Sentiment = Sentiment.NEUTRAL
    source_count: int = 0


class CrossSourceAnalyzer:
    """Analyze agreement/disagreement across multiple sources."""

    POSITIVE_WORDS = {"good", "great", "excellent", "best", "recommend", "fast", "efficient", "easy", "powerful", "effective"}
    NEGATIVE_WORDS = {"bad", "poor", "slow", "broken", "fail", "issue", "problem", "difficult", "complex", "deprecated"}

    def analyze(self, question: str,
                sources: list[tuple[str, str, str]]) -> CrossSourceAnalysis:
        """Analyze positions across sources.

        Args:
            question: The question being analyzed
            sources: List of (source_name, title, content)
        """
        result = CrossSourceAnalysis(question=question, source_count=len(sources))

        for src_name, title, content in sources:
            position = SourcePosition(source=src_name, title=title, position=content[:200])
            position.sentiment = self._detect_sentiment(content)
            position.key_points = self._extract_key_points(content)
            result.positions.append(position)

        result.consensus = self._find_consensus(result.positions)
        result.disagreements = self._find_disagreements(result.positions)
        result.overall_sentiment = self._overall_sentiment(result.positions)

        return result

    def _detect_sentiment(self, text: str) -> Sentiment:
        words = set(text.lower().split())
        pos = len(words & self.POSITIVE_WORDS)
        neg = len(words & self.NEGATIVE_WORDS)
        if pos > neg and pos > 0:
            return Sentiment.POSITIVE
        elif neg > pos and neg > 0:
            return Sentiment.NEGATIVE
        elif pos > 0 and neg > 0:
            return Sentiment.MIXED
        return Sentiment.NEUTRAL

    def _extract_key_points(self, content: str) -> list[str]:
        sentences = re.split(r"[.!?]+", content)
        return [s.strip() for s in sentences if len(s.strip()) > 20][:5]

    def _find_consensus(self, positions: list[SourcePosition]) -> list[str]:
        """Find points mentioned by multiple sources."""
        point_counts: dict[str, int] = {}
        for pos in positions:
            for point in pos.key_points:
                key = point.lower()[:50]
                point_counts[key] = point_counts.get(key, 0) + 1
        return [p for p, c in point_counts.items() if c >= 2]

    def _find_disagreements(self, positions: list[SourcePosition]) -> list[str]:
        """Find where sources have different sentiments."""
        disagreements: list[str] = []
        sentiments = {p.source: p.sentiment for p in positions}
        unique = set(sentiments.values())
        if len(unique) > 1:
            disagreements.append(f"Sources disagree on sentiment: {sentiments}")
        return disagreements

    def _overall_sentiment(self, positions: list[SourcePosition]) -> Sentiment:
        if not positions:
            return Sentiment.NEUTRAL
        sentiments = [p.sentiment for p in positions]
        pos = sum(1 for s in sentiments if s == Sentiment.POSITIVE)
        neg = sum(1 for s in sentiments if s == Sentiment.NEGATIVE)
        if pos > neg:
            return Sentiment.POSITIVE
        elif neg > pos:
            return Sentiment.NEGATIVE
        elif pos > 0 and neg > 0:
            return Sentiment.MIXED
        return Sentiment.NEUTRAL