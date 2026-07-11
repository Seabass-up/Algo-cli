"""Electrical construction labor-unit reference database.

Provides a SQLite + FTS5 searchable database of electrical labor-unit
records from multiple sources:

- **NECA MLU 2015-2016**: 13,712 records, 3 columns (Normal / Difficult / Very Difficult)
- **Durand & Associates 2022**: 4,165 records, 5 columns (Easy / Average / Difficult / Remodel / Old Work)

Each record carries man-hours per installed item plus the unit basis
(E=each, C=per hundred, M=per thousand LF, etc.).

The database is built once from the extracted CSVs and cached at
``~/.algo_cli/mlu_labor_units.db``. Subsequent calls open the cached DB
read-only unless a rebuild is requested.

Pattern: B27-style incremental index — file hash watermark on the CSV
sources avoids unnecessary rebuilds (see ALGO.md).

EC&M / Mike Holt adjustment factors are encoded as lookup tables for
fine-grained productivity adjustments beyond the column system.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = Path(
    os.environ.get("ALGO_CLI_MLU_DB", str(Path.home() / ".algo_cli" / "mlu_labor_units.db"))
)

# CSV sources live alongside the extractor scripts in the repo.
_REPO_ROOT = Path(__file__).resolve().parents[3]  # algo_cli/intelligence/construction -> repo root
DEFAULT_NECA_CSV = _REPO_ROOT / "scripts" / "mlu_labor_units.csv"
DEFAULT_DURAND_CSV = _REPO_ROOT / "scripts" / "durand_labor_units.csv"

VALID_UNITS = frozenset({"E", "C", "M", "LF", "FT", "CY", "SF", "PR", "EA"})

UNIT_LABELS = {
    "E": "Each",
    "EA": "Each",
    "C": "Per hundred items",
    "M": "Per thousand linear feet",
    "LF": "Linear foot",
    "FT": "Foot",
    "CY": "Cubic yard",
    "SF": "Square foot",
    "PR": "Pair",
}

# ── NECA Labor Factor Score Sheet (1-5 scale, 37 conditions, max 175) ────────
# Score 36-75 -> Normal, 76-134 -> Difficult, 135-175 -> Very Difficult
SCORE_THRESHOLDS = {
    "Normal": (36, 75),
    "Difficult": (76, 134),
    "VeryDifficult": (135, 175),
}

# ── NECA Labor Adjustment Chart (1-3 scale, 30 situations, max 90) ────────────
# Score 30-40 -> Normal, 41-70 -> Difficult, 71-90 -> Very Difficult
CHART_SCORE_THRESHOLDS = {
    "Normal": (30, 40),
    "Difficult": (41, 70),
    "VeryDifficult": (71, 90),
}

# Building height productivity loss: 1-2% per floor above 3 stories
HEIGHT_LOSS_PER_FLOOR = 0.015  # 1.5% average

# ── EC&M / Mike Holt adjustment factors ──────────────────────────────────────
# Source: "Adjusting Labor Units the Smart Way" (EC&M, Mike Holt)
# These are percentage adjustments applied to base labor hours.

# Building height adjustment (add to total labor)
BUILDING_HEIGHT_FACTORS: dict[int, float] = {
    1: 0.00,   # 1-2 floors
    3: 0.01,   # 3-6 floors (+1%)
    7: 0.02,   # 7-8 floors (+2%)
    9: 0.05,   # 9-14 floors (+5%)
    15: 0.07,  # 15-19 floors (+7%)
    20: 0.13,  # 20-30 floors (+13%)
}

# Ladder/scaffold work adjustment by working height (add to total labor)
LADDER_HEIGHT_FACTORS: dict[int, float] = {
    12: 0.03,   # 12 ft (+3%)
    13: 0.05,   # 13 ft (+5%)
    14: 0.08,   # 14 ft (+8%)
    15: 0.10,   # 15 ft (+10%)
    16: 0.13,   # 16 ft (+13%)
    17: 0.16,   # 17 ft (+16%)
    18: 0.19,   # 18 ft (+19%)
    19: 0.22,   # 19 ft (+22%)
    20: 0.25,   # 20 ft (+25%)
}

# Fixed scaffold: +40% plus setup/move/takedown labor
FIXED_SCAFFOLD_FACTOR = 0.40

# Concealed/exposed wiring adjustments (multiplier on base labor)
CONCEALED_FACTORS: dict[str, float] = {
    "concrete_wall": 0.50,       # Concealed in concrete walls: +50%
    "concrete_column": 1.00,     # Concealed in concrete columns: +100%
    "exposed_enclosure": 0.10,   # Exposed enclosures: +10%
    "exposed_raceway": 0.20,     # Exposed raceways: +20%
}

# Repetitive work productivity gain (multiplier on base labor)
REPETITIVE_FACTORS: dict[int, float] = {
    2: 0.10,    # 1-2 repeats: 10% of base
    5: 0.15,    # 3-5 repeats: 15%
    10: 0.25,   # 6-10 repeats: 25%
    15: 0.35,   # 11-15 repeats: 35%
    16: 0.45,   # 16+ repeats: 45%
}

# Remodeling adjustment (up to +200% for fish-in wire / cut-in boxes)
REMODEL_FACTOR = 2.0

# ── Standard labor unit composition (from Estimating 101 field study) ─────────
LABOR_COMPOSITION: dict[str, float] = {
    "plan_spec_study": 0.06,      # 6% (3.5 min)
    "ordering": 0.01,             # 1% (0.5 min)
    "receiving_storing": 0.04,    # 4% (2.5 min)
    "handling_to_location": 0.07, # 7% (4 min)
    "tooling_up": 0.05,           # 5% (3 min)
    "layout": 0.05,               # 5% (3 min)
    "installation": 0.62,         # 62% (37.5 min)
    "non_productive": 0.10,       # 10% (6 min)
}

# ── Fennec Lab residential defaults (NECA MLU-based minutes) ─────────────────
RESIDENTIAL_DEFAULTS: dict[str, float] = {
    "receptacle_outlet": 35.0,    # 35 min rough-in
    "switch": 25.0,               # 25 min rough-in
    "light_fixture": 45.0,        # 45 min rough-in
    "panel_circuit": 60.0,        # 60 min per circuit
    "low_voltage_run": 20.0,      # 20 min per run
}

# Residential service multiplier (finished walls, occupied, smaller crews)
RESIDENTIAL_SERVICE_MULTIPLIER = (1.1, 1.3)  # 1.1-1.3x MLU

# Finish/trim-out: add 30-50% of rough-in
FINISH_TRIM_PERCENT = (0.30, 0.50)

# Inspection rework buffer: 5-10%
INSPECTION_REWORK_BUFFER = (0.05, 0.10)


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaborUnit:
    """A single labor-unit entry from NECA or Durand."""

    section: str
    subsection: str
    description: str
    unit: str
    source: str = "NECA"  # "NECA" or "Durand"
    # NECA 3-column system
    normal: float = 0.0
    difficult: float = 0.0
    very_difficult: float = 0.0
    # Durand 5-column system
    easy: float = 0.0
    average: float = 0.0
    hard: float = 0.0       # Durand's "Difficult" (renamed to avoid collision)
    remodel: float = 0.0
    old_work: float = 0.0
    # Common
    rev: str = ""
    company_experience: str = ""
    table_index: int = 0

    @property
    def unit_label(self) -> str:
        return UNIT_LABELS.get(self.unit, self.unit)

    def hours_for(self, difficulty: str = "Normal") -> float:
        """Return the labor hours for the given difficulty column.

        Supports both NECA (Normal/Difficult/VeryDifficult) and
        Durand (Easy/Average/Difficult/Remodel/OldWork) systems.

        Args:
            difficulty: One of 'Normal', 'Difficult', 'VeryDifficult'
                (NECA) or 'Easy', 'Average', 'Hard', 'Remodel', 'OldWork'
                (Durand). Case-insensitive, accepts short forms.
        """
        key = difficulty.strip().lower().replace("-", "_").replace(" ", "_")
        # NECA columns
        if key in ("normal", "n"):
            return self.normal
        if key in ("difficult", "d", "diff"):
            # If NECA source, use NECA difficult; if Durand, use hard
            return self.hard if self.source == "Durand" and self.hard else self.difficult
        if key in ("very_difficult", "verydifficult", "very", "v"):
            return self.very_difficult
        # Durand columns
        if key in ("easy", "e"):
            return self.easy
        if key in ("average", "avg", "a"):
            return self.average
        if key in ("hard",):
            return self.hard
        if key in ("remodel", "r"):
            return self.remodel
        if key in ("old_work", "oldwork", "old", "o"):
            return self.old_work
        raise ValueError(f"Unknown difficulty: {difficulty!r}")

    def available_difficulties(self) -> list[str]:
        """Return the difficulty columns that have non-zero values."""
        all_cols = [
            ("Easy", self.easy),
            ("Normal", self.normal),
            ("Average", self.average),
            ("Difficult", self.difficult),
            ("Hard", self.hard),
            ("VeryDifficult", self.very_difficult),
            ("Remodel", self.remodel),
            ("OldWork", self.old_work),
        ]
        return [name for name, val in all_cols if val and val > 0]

    def adjusted_hours(
        self,
        difficulty: str = "Normal",
        quantity: float = 1.0,
        building_floors: int = 0,
    ) -> float:
        """Compute total labor hours for a given quantity and building height.

        Args:
            difficulty: Difficulty column name (see hours_for).
            quantity: Number of units (each, hundred, thousand LF, etc.).
            building_floors: Number of floors above 3 stories (0 = ground/low-rise).

        Returns:
            Total adjusted labor hours.
        """
        base = self.hours_for(difficulty)
        total = base * quantity
        if building_floors > 0:
            loss = 1.0 + (HEIGHT_LOSS_PER_FLOOR * building_floors)
            total *= loss
        return round(total, 2)


@dataclass
class SearchResult:
    """A ranked search hit from the FTS5 query."""

    labor_unit: LaborUnit
    rank: float
    snippet: str = ""


# ── Database builder ──────────────────────────────────────────────────────────


def _csv_hash(csv_path: Path) -> str:
    """Return a short hash of the CSV file contents for change detection."""
    h = hashlib.sha256()
    with open(csv_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _combined_hash(paths: list[Path]) -> str:
    """Hash multiple CSV files together for change detection."""
    h = hashlib.sha256()
    for p in paths:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _needs_rebuild(db_path: Path, csv_paths: list[Path]) -> bool:
    """Check if the DB needs rebuilding based on CSV hash watermark."""
    if not db_path.exists():
        return True
    if not any(p.exists() for p in csv_paths):
        return False  # can't rebuild without CSVs; use existing DB
    try:
        conn = sqlite3.connect(str(db_path))
        stored = conn.execute(
            "SELECT value FROM _meta WHERE key = 'csv_hash'"
        ).fetchone()
        conn.close()
        if stored is None:
            return True
        return stored[0] != _combined_hash(csv_paths)
    except sqlite3.OperationalError:
        return True


def build_database(
    csv_paths: Path | str | list[Path | str] | None = None,
    db_path: Path | str | None = None,
    force: bool = False,
) -> Path:
    """Build (or rebuild) the SQLite + FTS5 labor-units database.

    Loads both NECA and Durand CSVs by default. Each CSV is auto-detected
    as NECA (has 'normal' column) or Durand (has 'easy' column).

    Args:
        csv_paths: Path or list of paths to CSV files. Defaults to
            ``[<repo>/scripts/mlu_labor_units.csv, <repo>/scripts/durand_labor_units.csv]``.
        db_path: Path for the SQLite database. Defaults to
            ``~/.algo_cli/mlu_labor_units.db``.
        force: Rebuild even if the CSV hash hasn't changed.

    Returns:
        Path to the built database.
    """
    # Normalize csv_paths to a list
    if csv_paths is None:
        csv_paths = [DEFAULT_NECA_CSV, DEFAULT_DURAND_CSV]
    elif isinstance(csv_paths, (str, Path)):
        csv_paths = [csv_paths]
    csv_paths = [Path(p) for p in csv_paths]

    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH

    existing = [p for p in csv_paths if p.exists()]
    if not existing:
        raise FileNotFoundError(f"No labor-unit CSVs found in: {csv_paths}")

    if not force and not _needs_rebuild(db_path, existing):
        return db_path

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove stale DB + WAL/SHM
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix) if suffix else db_path
        if p.exists():
            p.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Main table — supports both NECA and Durand columns
    conn.execute("""
        CREATE TABLE labor_units (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'NECA',
            section TEXT NOT NULL DEFAULT '',
            subsection TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL,
            normal REAL DEFAULT 0,
            difficult REAL DEFAULT 0,
            very_difficult REAL DEFAULT 0,
            easy REAL DEFAULT 0,
            average REAL DEFAULT 0,
            hard REAL DEFAULT 0,
            remodel REAL DEFAULT 0,
            old_work REAL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT '',
            rev TEXT DEFAULT '',
            company_experience TEXT DEFAULT '',
            table_index INTEGER DEFAULT 0
        )
    """)

    # FTS5 full-text index on description + subsection
    conn.execute("""
        CREATE VIRTUAL TABLE fts_labor_units
        USING fts5(
            description,
            subsection,
            content='labor_units',
            content_rowid='id',
            tokenize='porter unicode61'
        )
    """)

    # Bulk insert from each CSV
    total_rows = 0
    for csv_path in existing:
        with open(csv_path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = []
            for r in reader:
                # Auto-detect source from CSV columns
                if "source" in r and r["source"]:
                    source = r["source"]
                elif "easy" in r and r.get("easy"):
                    source = "Durand"
                else:
                    source = "NECA"

                rows.append((
                    source,
                    r.get("section", ""),
                    r.get("subsection", ""),
                    r.get("description", ""),
                    _safe_float(r.get("normal")),
                    _safe_float(r.get("difficult")),
                    _safe_float(r.get("very_difficult")),
                    _safe_float(r.get("easy")),
                    _safe_float(r.get("average")),
                    _safe_float(r.get("hard") or r.get("difficult_durand")),
                    _safe_float(r.get("remodel")),
                    _safe_float(r.get("old_work")),
                    r.get("unit", ""),
                    r.get("rev", ""),
                    r.get("company_experience", ""),
                    int(r.get("table_index", 0) or 0),
                ))

            conn.executemany(
                "INSERT INTO labor_units "
                "(source, section, subsection, description, "
                "normal, difficult, very_difficult, "
                "easy, average, hard, remodel, old_work, "
                "unit, rev, company_experience, table_index) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            total_rows += len(rows)

    # Populate FTS index
    conn.execute(
        "INSERT INTO fts_labor_units (rowid, description, subsection) "
        "SELECT id, description, subsection FROM labor_units"
    )

    # Indexes for non-FTS lookups
    conn.execute("CREATE INDEX idx_source ON labor_units(source)")
    conn.execute("CREATE INDEX idx_subsection ON labor_units(subsection)")
    conn.execute("CREATE INDEX idx_section ON labor_units(section)")
    conn.execute("CREATE INDEX idx_unit ON labor_units(unit)")

    # Meta table for change detection
    conn.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO _meta (key, value) VALUES ('csv_hash', ?)",
        (_combined_hash(existing),),
    )
    conn.execute(
        "INSERT INTO _meta (key, value) VALUES ('record_count', ?)",
        (str(total_rows),),
    )
    conn.execute(
        "INSERT INTO _meta (key, value) VALUES ('sources', ?)",
        (",".join(p.stem for p in existing),),
    )

    conn.commit()
    conn.close()
    return db_path


def _safe_float(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _row_to_labor_unit(row: sqlite3.Row) -> LaborUnit:
    return LaborUnit(
        source=row["source"],
        section=row["section"],
        subsection=row["subsection"],
        description=row["description"],
        normal=row["normal"] or 0.0,
        difficult=row["difficult"] or 0.0,
        very_difficult=row["very_difficult"] or 0.0,
        easy=row["easy"] or 0.0,
        average=row["average"] or 0.0,
        hard=row["hard"] or 0.0,
        remodel=row["remodel"] or 0.0,
        old_work=row["old_work"] or 0.0,
        unit=row["unit"],
        rev=row["rev"] or "",
        company_experience=row["company_experience"] or "",
        table_index=row["table_index"],
    )


# ── Query API ─────────────────────────────────────────────────────────────────


class LaborUnitDatabase:
    """Searchable labor-unit database backed by SQLite + FTS5.

    Usage::

        db = LaborUnitDatabase()  # opens or builds cached DB
        results = db.search("fire alarm control panel")
        for r in results:
            print(r.labor_unit.description, r.labor_unit.normal)
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        csv_paths: Path | str | list[Path | str] | None = None,
        auto_build: bool = True,
    ):
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        if csv_paths is None:
            self._csv_paths = [DEFAULT_NECA_CSV, DEFAULT_DURAND_CSV]
        elif isinstance(csv_paths, (str, Path)):
            self._csv_paths = [Path(csv_paths)]
        else:
            self._csv_paths = [Path(p) for p in csv_paths]
        self._conn: sqlite3.Connection | None = None

        if auto_build and _needs_rebuild(self._db_path, self._csv_paths):
            existing = [p for p in self._csv_paths if p.exists()]
            if existing:
                build_database(existing, self._db_path)
        self._open()

    def _open(self) -> None:
        if not self._db_path.exists():
            raise FileNotFoundError(
                f"Labor-unit database not found at {self._db_path}. "
                f"Run build_database() first or ensure CSVs are available."
            )
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> LaborUnitDatabase:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Search ─────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 20,
        subsection: str | None = None,
        source: str | None = None,
    ) -> list[SearchResult]:
        """Full-text search on description + subsection.

        Args:
            query: Search terms (FTS5 syntax: AND/OR/NOT, phrase "quotes").
            limit: Max results.
            subsection: Optional filter to narrow to a subsection.
            source: Optional filter ('NECA' or 'Durand').

        Returns:
            Ranked list of SearchResult.
        """
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        sql = (
            "SELECT l.*, f.rank, snippet(fts_labor_units, 0, '[', ']', '...', 10) as snip "
            "FROM fts_labor_units f "
            "JOIN labor_units l ON l.id = f.rowid "
            "WHERE fts_labor_units MATCH ? "
        )
        params: list[Any] = [fts_query]
        if subsection:
            sql += "AND l.subsection = ? "
            params.append(subsection)
        if source:
            sql += "AND l.source = ? "
            params.append(source)
        sql += "ORDER BY f.rank LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [
            SearchResult(
                labor_unit=_row_to_labor_unit(r),
                rank=r["rank"],
                snippet=r["snip"] or "",
            )
            for r in rows
        ]

    def by_subsection(self, subsection: str, limit: int = 500, source: str | None = None) -> list[LaborUnit]:
        """Return all labor units for a given subsection (exact match)."""
        if source:
            rows = self.conn.execute(
                "SELECT * FROM labor_units WHERE subsection = ? AND source = ? "
                "ORDER BY id LIMIT ?",
                (subsection, source, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM labor_units WHERE subsection = ? "
                "ORDER BY id LIMIT ?",
                (subsection, limit),
            ).fetchall()
        return [_row_to_labor_unit(r) for r in rows]

    def by_section(self, section: str, limit: int = 2000, source: str | None = None) -> list[LaborUnit]:
        """Return all labor units for a given section (exact match)."""
        if source:
            rows = self.conn.execute(
                "SELECT * FROM labor_units WHERE section = ? AND source = ? "
                "ORDER BY id LIMIT ?",
                (section, source, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM labor_units WHERE section = ? "
                "ORDER BY id LIMIT ?",
                (section, limit),
            ).fetchall()
        return [_row_to_labor_unit(r) for r in rows]

    def list_subsections(self, section: str | None = None, source: str | None = None) -> list[str]:
        """List all subsections, optionally filtered by section and/or source."""
        if section and source:
            rows = self.conn.execute(
                "SELECT DISTINCT subsection FROM labor_units "
                "WHERE section = ? AND source = ? ORDER BY subsection",
                (section, source),
            ).fetchall()
        elif section:
            rows = self.conn.execute(
                "SELECT DISTINCT subsection FROM labor_units "
                "WHERE section = ? ORDER BY subsection",
                (section,),
            ).fetchall()
        elif source:
            rows = self.conn.execute(
                "SELECT DISTINCT subsection FROM labor_units "
                "WHERE source = ? ORDER BY subsection",
                (source,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT subsection FROM labor_units ORDER BY subsection"
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def list_sections(self, source: str | None = None) -> list[str]:
        """List all sections, optionally filtered by source."""
        if source:
            rows = self.conn.execute(
                "SELECT DISTINCT section FROM labor_units WHERE source = ? ORDER BY section",
                (source,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT section FROM labor_units ORDER BY section"
            ).fetchall()
        return [r[0] for r in rows if r[0]]

    def list_sources(self) -> list[str]:
        """List all data sources in the database."""
        rows = self.conn.execute(
            "SELECT DISTINCT source FROM labor_units ORDER BY source"
        ).fetchall()
        return [r[0] for r in rows]

    def get(self, description: str, source: str | None = None) -> LaborUnit | None:
        """Exact-match lookup by description."""
        if source:
            row = self.conn.execute(
                "SELECT * FROM labor_units WHERE description = ? AND source = ? LIMIT 1",
                (description, source),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM labor_units WHERE description = ? LIMIT 1",
                (description,),
            ).fetchone()
        return _row_to_labor_unit(row) if row else None

    def estimate(
        self,
        description: str,
        quantity: float = 1.0,
        difficulty: str = "Normal",
        building_floors: int = 0,
        working_height_ft: int | None = None,
        concealed: str | None = None,
        repetitive_count: int | None = None,
        remodel: bool = False,
        source: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a labor unit and compute total adjusted hours.

        Applies NECA height adjustment plus optional EC&M fine-grained
        adjustments (working height, concealed wiring, repetitive work,
        remodeling).

        Args:
            description: Item description (exact or FTS search).
            quantity: Number of units.
            difficulty: Difficulty column name (see hours_for).
            building_floors: Floors above 3 stories for NECA height adjustment.
            working_height_ft: Working height in feet (for EC&M ladder adjustment).
            concealed: Type of concealed/exposed wiring ('concrete_wall',
                'concrete_column', 'exposed_enclosure', 'exposed_raceway').
            repetitive_count: Number of repetitive installations (for productivity gain).
            remodel: True if remodeling work (applies REMODEL_FACTOR).
            source: Filter to a specific source ('NECA' or 'Durand').

        Returns:
            Dict with labor_unit, base_hours, adjustments, total_hours,
            or None if not found.
        """
        unit = self.get(description, source=source)
        if unit is None:
            results = self.search(description, limit=1, source=source)
            if not results:
                return None
            unit = results[0].labor_unit

        base = unit.hours_for(difficulty)
        adjustments: dict[str, float] = {}

        # NECA building height adjustment
        if building_floors > 0:
            height_adj = HEIGHT_LOSS_PER_FLOOR * building_floors
            adjustments["building_height"] = height_adj

        # EC&M working height (ladder/scaffold) adjustment
        if working_height_ft is not None and working_height_ft >= 12:
            wh_factor = _lookup_factor(LADDER_HEIGHT_FACTORS, working_height_ft)
            adjustments["working_height"] = wh_factor

        # EC&M concealed/exposed wiring adjustment
        if concealed and concealed in CONCEALED_FACTORS:
            adjustments["concealed"] = CONCEALED_FACTORS[concealed]

        # EC&M repetitive work productivity gain
        if repetitive_count is not None and repetitive_count > 1:
            rep_factor = _lookup_factor(REPETITIVE_FACTORS, repetitive_count)
            adjustments["repetitive"] = -rep_factor  # negative = productivity gain

        # Remodeling adjustment
        if remodel:
            adjustments["remodel"] = REMODEL_FACTOR

        # Apply adjustments
        total_multiplier = 1.0
        for adj_name, adj_val in adjustments.items():
            total_multiplier += adj_val

        total = base * quantity * total_multiplier
        total = round(total, 2)

        return {
            "description": unit.description,
            "subsection": unit.subsection,
            "source": unit.source,
            "unit": unit.unit,
            "unit_label": unit.unit_label,
            "difficulty": difficulty,
            "base_hours": base,
            "quantity": quantity,
            "building_floors": building_floors,
            "adjustments": adjustments,
            "adjustment_multiplier": round(total_multiplier, 4),
            "total_hours": total,
            "available_difficulties": unit.available_difficulties(),
        }

    @property
    def record_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM labor_units").fetchone()
        return row[0]

    def source_counts(self) -> dict[str, int]:
        """Return record count per source."""
        rows = self.conn.execute(
            "SELECT source, COUNT(*) FROM labor_units GROUP BY source"
        ).fetchall()
        return {r[0]: r[1] for r in rows}


# ── EC&M adjustment helpers ───────────────────────────────────────────────────


def _lookup_factor(table: dict[int, float], value: int) -> float:
    """Look up a factor from a threshold table (keys are min thresholds)."""
    result = 0.0
    for threshold in sorted(table.keys()):
        if value >= threshold:
            result = table[threshold]
        else:
            break
    return result


def building_height_adjustment(floors: int) -> float:
    """Return the EC&M building height adjustment factor.

    Args:
        floors: Total building floors (not floors above 3).

    Returns:
        Multiplier adjustment (0.0 to 0.13).
    """
    return _lookup_factor(BUILDING_HEIGHT_FACTORS, floors)


def ladder_height_adjustment(height_ft: int) -> float:
    """Return the EC&M ladder/scaffold height adjustment factor.

    Args:
        height_ft: Working height in feet.

    Returns:
        Multiplier adjustment (0.0 to 0.25).
    """
    return _lookup_factor(LADDER_HEIGHT_FACTORS, height_ft)


def repetitive_adjustment(count: int) -> float:
    """Return the EC&M repetitive work productivity gain factor.

    Args:
        count: Number of repetitive installations.

    Returns:
        Productivity gain (0.10 to 0.45). Negative when applied as adjustment.
    """
    return _lookup_factor(REPETITIVE_FACTORS, count)


# ── Difficulty scoring ────────────────────────────────────────────────────────


def difficulty_from_score(score: int) -> str:
    """Map a NECA Labor Factor Score Sheet total to a difficulty column.

    Uses the 1-5 scale, 37 conditions, max 175 points system.

    Score ranges:
        36-75   -> Normal
        76-134  -> Difficult
        135-175 -> VeryDifficult
    """
    if score < 36:
        return "Normal"
    if score <= 75:
        return "Normal"
    if score <= 134:
        return "Difficult"
    return "VeryDifficult"


def difficulty_from_chart_score(score: int) -> str:
    """Map a NECA Labor Adjustment Chart total to a difficulty column.

    Uses the 1-3 scale, 30 situations, max 90 points system.

    Score ranges:
        30-40 -> Normal
        41-70 -> Difficult
        71-90 -> VeryDifficult
    """
    if score <= 40:
        return "Normal"
    if score <= 70:
        return "Difficult"
    return "VeryDifficult"


def residential_minutes(task: str, service: bool = False) -> float:
    """Return estimated minutes for a residential task.

    Uses Fennec Lab NECA MLU-based defaults with optional service multiplier.

    Args:
        task: One of 'receptacle_outlet', 'switch', 'light_fixture',
            'panel_circuit', 'low_voltage_run'.
        service: If True, apply residential service multiplier (1.2x midpoint).

    Returns:
        Estimated minutes.
    """
    base = RESIDENTIAL_DEFAULTS.get(task, 0.0)
    if service and base > 0:
        # Use midpoint of 1.1-1.3 range
        base *= (RESIDENTIAL_SERVICE_MULTIPLIER[0] + RESIDENTIAL_SERVICE_MULTIPLIER[1]) / 2
    return base


# ── FTS5 helpers ──────────────────────────────────────────────────────────────

_FTS5_SPECIAL = re.compile(r'["\'\*\(\)\+\-\^:]')


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a user query for FTS5 MATCH.

    Wraps each token in double quotes to prevent syntax errors from
    special characters. Preserves AND/OR/NOT operators.
    """
    query = query.strip()
    if not query:
        return ""
    tokens = re.findall(r'"[^"]*"|\S+', query)
    sanitized = []
    for tok in tokens:
        if tok.upper() in ("AND", "OR", "NOT"):
            sanitized.append(tok.upper())
        elif tok.startswith('"') and tok.endswith('"'):
            sanitized.append(tok)
        else:
            clean = _FTS5_SPECIAL.sub("", tok)
            if clean:
                sanitized.append(f'"{clean}"')
    return " ".join(sanitized)


# ── NECA Labor Factor Score Sheet (37 conditions, 1-5 scale) ──────────────────


@dataclass
class ScoreCondition:
    """One condition in the NECA Labor Factor Score Sheet (1-5 scale).

    Each level (1-5) has a description. Score 1 = best conditions,
    5 = worst conditions. Total score range: 37-185.
    """

    id: str
    description: str
    level_1: str = ""
    level_2: str = ""
    level_3: str = ""
    level_4: str = ""
    level_5: str = ""
    score: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 5:
            raise ValueError(f"Score must be 0-5, got {self.score}")

    @property
    def is_scored(self) -> bool:
        return self.score > 0


@dataclass
class LaborFactorScoreSheet:
    """NECA Labor Factor Score Sheet — 37 conditions, 1-5 scale.

    Total score 36-75 → Normal, 76-134 → Difficult, 135-175 → Very Difficult.
    """

    conditions: list[ScoreCondition]

    @classmethod
    def standard(cls) -> LaborFactorScoreSheet:
        """Create the standard 37-condition NECA score sheet."""
        return cls(conditions=list(_STANDARD_SCORE_SHEET_CONDITIONS()))

    @property
    def total_score(self) -> int:
        return sum(c.score for c in self.conditions if c.is_scored)

    @property
    def max_possible_score(self) -> int:
        return len(self.conditions) * 5

    @property
    def scored_count(self) -> int:
        return sum(1 for c in self.conditions if c.is_scored)

    @property
    def difficulty(self) -> str:
        return difficulty_from_score(self.total_score)

    def set_score(self, condition_id: str, score: int) -> None:
        for c in self.conditions:
            if c.id == condition_id:
                c.score = score
                return
        raise KeyError(f"Unknown condition: {condition_id}")

    def reset(self) -> None:
        for c in self.conditions:
            c.score = 0

    def summary(self) -> dict[str, object]:
        return {
            "scored": self.scored_count,
            "total_conditions": len(self.conditions),
            "total_score": self.total_score,
            "difficulty": self.difficulty,
        }


def _STANDARD_SCORE_SHEET_CONDITIONS() -> list[ScoreCondition]:
    """Build the 37 standard NECA Labor Factor Score Sheet conditions."""
    return [
        ScoreCondition("working_height", "Working Height",
                       "≤10'", "10'-15'", "15'-20'", "20'-30'", ">30'"),
        ScoreCondition("building_height", "Building Height (floors)",
                       "1-3 floors", "4-7 floors", "8-14 floors", "15-30 floors", ">30 floors"),
        ScoreCondition("site_size", "Site Size",
                       "Large/open", "Medium", "Small", "Confined", "Very confined"),
        ScoreCondition("job_condition", "Job Condition",
                       "New construction", "Remodel—unoccupied", "Remodel—occupied", "Tenant finish—occupied", "Historic/asbestos"),
        ScoreCondition("conduit_type", "Conduit Type",
                       "EMT/flex", "Mixed EMT/RMC", "RMC/PVC", "Rigid aluminum/PVC-coated", "Explosion-proof"),
        ScoreCondition("voltage", "Voltage Level",
                       "0-600V", "600V-5kV", "5kV-15kV", "15kV-35kV", ">35kV"),
        ScoreCondition("drawings_complete", "Drawings % Complete",
                       "100%", "90%", "75%", "50%", "<50%"),
        ScoreCondition("change_order_quantity", "Change Order Quantity",
                       "None", "Few (<5%)", "Moderate (5-10%)", "Many (10-20%)", "Extensive (>20%)"),
        ScoreCondition("change_order_timing", "Change Order Timing",
                       "Before start", "Early phase", "Mid-project", "Late phase", "Near completion"),
        ScoreCondition("craft_coordination", "Craft Coordination",
                       "Single trade", "2-3 trades", "4-6 trades", "7-10 trades", ">10 trades"),
        ScoreCondition("ahj_experience", "AHJ Experience with Project Type",
                       "Considerable", "Moderate", "Limited", "None", "Hostile"),
        ScoreCondition("hours_worked", "Hours Worked per Week",
                       "40 hrs", "45 hrs", "50 hrs", "55 hrs", "60+ hrs"),
        ScoreCondition("shifts", "Shifts",
                       "Day only", "Day + occasional OT", "2 shifts", "3 shifts", "Rotating shifts"),
        ScoreCondition("job_documents", "Job Documents",
                       "Complete specs", "Partial specs", "Minimal specs", "Drawings only", "No documents"),
        ScoreCondition("working_conditions", "Working Conditions",
                       "Controlled indoor", "Outdoor—moderate", "Outdoor—extreme weather", "Hazardous environment", "Extreme hazardous"),
        ScoreCondition("crew_density", "Crew Density",
                       "Low (≤3 workers)", "Medium (4-6)", "High (7-10)", "Very high (11-15)", "Extreme (>15)"),
        ScoreCondition("job_duration", "Job Duration",
                       "Long (>6 months)", "Medium (3-6 months)", "Short (1-3 months)", "Very short (<1 month)", "Crash schedule"),
        ScoreCondition("building_sqft", "Building Square Footage",
                       ">100k sq ft", "50k-100k", "10k-50k", "1k-10k", "<1k sq ft"),
        ScoreCondition("project_size", "Project Size ($)",
                       ">$750k", "$100k-750k", "$25k-100k", "$5k-25k", "<$5k"),
        ScoreCondition("safety", "Safety Requirements",
                       "Low risk", "Standard PPE", "High risk (fall protection)", "Very high (confined space)", "Extreme (HAZWOPER)"),
        ScoreCondition("clean_up", "Clean-up Requirements",
                       "Minimal", "Standard", "Extensive", "Daily full clean", "White-glove/PHI"),
        ScoreCondition("installation_repetition", "Installation Repetition",
                       "Highly repetitive", "Moderate repetition", "Some repetition", "Low repetition", "One-off/custom"),
        ScoreCondition("construction_type", "Construction Type",
                       "New—steel frame", "New—wood frame", "Addition", "Remodel", "Tenant finish"),
        ScoreCondition("systems_complexity", "Systems Complexity",
                       "Simple (power/lighting)", "Moderate (power+low-voltage)", "Complex (IBS+automation)", "Very complex (BMS+process)", "Extreme (mission critical)"),
        ScoreCondition("project_access", "Project Access",
                       "Easy—open site", "Moderate—some restrictions", "Difficult—limited hours", "Very difficult—security", "Extreme—escorted only"),
        ScoreCondition("tools", "Tools & Equipment",
                       "Adequate", "Mostly adequate", "Some shortages", "Frequent shortages", "Inadequate"),
        ScoreCondition("labor_base", "Labor Base Experience",
                       "5+ years, company crew", "3-5 years, company", "1-3 years, mixed", "JW from hall", "Apprentices/helpers"),
        ScoreCondition("information_flow", "Information Flow",
                       "Real-time RFIs answered", "24-hr RFI response", "48-hr RFI response", "Weekly RFI response", "Poor/no response"),
        ScoreCondition("decision_making", "Decision Making Speed",
                       "Same day", "1-2 days", "3-5 days", "1 week", ">1 week"),
        ScoreCondition("job_continuity", "Job Continuity",
                       "Continuous work", "Minor breaks", "Some downtime", "Frequent breaks", "Start-stop"),
        ScoreCondition("job_schedule", "Job Schedule Pressure",
                       "Relaxed", "Normal", "Moderate pressure", "Tight schedule", "Crash/accelerated"),
        ScoreCondition("job_meetings", "Job Meetings Frequency",
                       "Monthly", "Bi-weekly", "Weekly", "2-3x/week", "Daily"),
        # ── 5 conditions missing from original model (Tier 2 addition) ──
        ScoreCondition("bim_usage", "BIM Usage",
                       "Proactive BIM coordination", "Moderately proactive", "Contract-only BIM", "2D drawings only", "No BIM/no drawings"),
        ScoreCondition("gc_count", "General Contractors on Jobsite",
                       "Single prime", "Two primes", "Three primes", "Four+ primes", "Multiple primes + subs"),
        ScoreCondition("shared_responsibility", "Shared Responsibility (ECs on site)",
                       "Sole EC", "Two ECs", "Three ECs", "Four+ ECs", "Fragmented responsibility"),
        ScoreCondition("material_proximity", "Proximity of Stored Materials",
                       "On site—staged at work area", "On site—general area", "Off site—nearby", "Off site—distant", "Remote warehouse"),
        ScoreCondition("ahj_project_type", "AHJ Experience with This Project Type",
                       "Considerable—same AHJ", "Moderate—familiar AHJ", "Limited—new AHJ", "None—unfamiliar AHJ", "Hostile AHJ"),
    ]


# ── NECA Labor Adjustment Chart (30 situations, 1-3 scale) ────────────────────


@dataclass
class ChartSituation:
    """One situation in the NECA Labor Adjustment Chart (1-3 scale).

    Score 1 = best conditions, 3 = worst. Total range: 30-90.
    """

    id: str
    description: str
    level_1: str = ""
    level_2: str = ""
    level_3: str = ""
    score: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 3:
            raise ValueError(f"Score must be 0-3, got {self.score}")

    @property
    def is_scored(self) -> bool:
        return self.score > 0


@dataclass
class LaborAdjustmentChart:
    """NECA Labor Adjustment Chart — 30 situations, 1-3 scale.

    Total score 30-40 → Normal, 41-70 → Difficult, 71-90 → Very Difficult.
    """

    situations: list[ChartSituation]

    @classmethod
    def standard(cls) -> LaborAdjustmentChart:
        return cls(situations=list(_STANDARD_CHART_SITUATIONS()))

    @property
    def total_score(self) -> int:
        return sum(s.score for s in self.situations if s.is_scored)

    @property
    def max_possible_score(self) -> int:
        return len(self.situations) * 3

    @property
    def scored_count(self) -> int:
        return sum(1 for s in self.situations if s.is_scored)

    @property
    def difficulty(self) -> str:
        return difficulty_from_chart_score(self.total_score)

    def set_score(self, situation_id: str, score: int) -> None:
        for s in self.situations:
            if s.id == situation_id:
                s.score = score
                return
        raise KeyError(f"Unknown situation: {situation_id}")

    def reset(self) -> None:
        for s in self.situations:
            s.score = 0

    def summary(self) -> dict[str, object]:
        return {
            "scored": self.scored_count,
            "total_situations": len(self.situations),
            "total_score": self.total_score,
            "difficulty": self.difficulty,
        }


def _STANDARD_CHART_SITUATIONS() -> list[ChartSituation]:
    """Build the 30 standard NECA Labor Adjustment Chart situations."""
    return [
        ChartSituation("hours_worked", "Hours Worked", "40 hrs", "50 hrs", ">50 hrs"),
        ChartSituation("shifts", "Shifts", "Day", "2nd shift", "3rd shift"),
        ChartSituation("job_documents", "Job Documents", "Standard", "Poor", "None"),
        ChartSituation("working_conditions", "Working Conditions", "Controlled indoor", "Outdoor—moderate", "Extreme weather"),
        ChartSituation("crew_density", "Crew Density", "Low", "Medium", "High"),
        ChartSituation("working_height", "Working Height", "≤10'", "10'-20'", ">20'"),
        ChartSituation("floors", "Floors", "0-3", "4-7", "8+"),
        ChartSituation("job_duration", "Job Duration", "Long", "Medium", "Short"),
        ChartSituation("building_sqft", "Building Sq Ft", "Large", "Medium", "Small"),
        ChartSituation("project_size", "Project Size", "≤$100k", "$100k-750k", ">$750k"),
        ChartSituation("site_size", "Site Size", "Large", "Medium", "Small"),
        ChartSituation("safety", "Safety", "Low risk", "Medium", "High"),
        ChartSituation("job_condition", "Job Condition", "New", "Remodel", "Occupied"),
        ChartSituation("clean_up", "Clean-up", "Minimal", "Moderate", "Extensive"),
        ChartSituation("installation_repetition", "Installation Repetition", "High", "Medium", "Low"),
        ChartSituation("construction_type", "Construction Type", "New", "Addition", "Remodel"),
        ChartSituation("systems_complexity", "Systems Complexity", "Simple", "Moderate", "Complex"),
        ChartSituation("project_access", "Project Access", "Easy", "Moderate", "Difficult"),
        ChartSituation("voltage", "Voltage", "0-600V", "600V-5kV", ">5kV"),
        ChartSituation("tools", "Tools", "Adequate", "Limited", "Inadequate"),
        ChartSituation("craft_coordination", "Craft Coordination", "Single trade", "Few trades", "Many trades"),
        ChartSituation("labor_base", "Labor Base", "Experienced", "Moderate", "Inexperienced"),
        ChartSituation("information_flow", "Information Flow", "Good", "Moderate", "Poor"),
        ChartSituation("decision_making", "Decision Making", "Quick", "Moderate", "Slow"),
        ChartSituation("job_continuity", "Job Continuity", "Continuous", "Some breaks", "Frequent breaks"),
        ChartSituation("change_order_quantity", "Change Order Quantity", "None", "Few", "Many"),
        ChartSituation("change_order_timing", "Change Order Timing", "Early", "Mid", "Late"),
        ChartSituation("job_schedule", "Job Schedule", "Relaxed", "Moderate", "Compressed"),
        ChartSituation("job_meetings", "Job Meetings", "Few", "Weekly", "Daily"),
        ChartSituation("conduit_type", "Conduit Type", "Simple", "Mixed", "Complex"),
    ]


# ── Durand Multi-Level Construction Adjustments (Section 23) ──────────────────


def durand_vertical_multiplier(level: int) -> float:
    """Durand vertical-work multiplier for multi-level construction.

    Vertical work (risers, vertical cable runs) becomes harder as
    building height increases. Formula: 1.03 + (level-1) * 0.02.

    Args:
        level: Building level (2-40). Level 1 = ground floor (1.0).

    Returns:
        Multiplier to apply to total vertical labor hours.

    Example:
        >>> durand_vertical_multiplier(20)
        1.41
        >>> durand_vertical_multiplier(40)
        1.81
    """
    if level <= 1:
        return 1.0
    return round(1.03 + (level - 1) * 0.02, 2)


def durand_horizontal_setup_hours(level: int) -> int:
    """Durand horizontal-work setup hours for multi-level construction.

    Horizontal work (branch circuits, same-level wiring) needs setup
    time for material/tool/personnel transport between floors.
    Formula: (level - 1) * 32 hours.

    Args:
        level: Building level (2-40). Level 1 = ground floor (0 hours).

    Returns:
        Total setup hours for that level.
    """
    if level <= 1:
        return 0
    return (level - 1) * 32


# Durand core-work multipliers (repetitive typical floors reduce labor).
# Encoded from the Section 23 table (page 23-4 of the Durand manual).
_DURAND_CORE_TABLE: dict[int, float] = {
    2: 0.95, 3: 0.93, 4: 0.91, 5: 0.89, 6: 0.87, 7: 0.86, 8: 0.85,
    9: 0.84, 10: 0.84, 11: 0.84, 12: 0.84, 13: 0.84, 14: 0.82, 15: 0.82,
    16: 0.82, 17: 0.82, 18: 0.82, 19: 0.82, 20: 0.81, 21: 0.81, 22: 0.81,
    23: 0.81, 24: 0.81, 25: 0.80, 26: 0.80, 27: 0.80, 28: 0.80, 29: 0.80,
    30: 0.78, 31: 0.78, 32: 0.78, 33: 0.78, 34: 0.78, 35: 0.78, 36: 0.75,
    37: 0.75, 38: 0.75, 39: 0.75, 40: 0.75,
}


def durand_core_multiplier(level: int) -> float:
    """Durand core-work multiplier for repetitive typical floors.

    Core areas (stairways, elevator lobbies, restrooms, electrical rooms)
    are typical floor-to-floor. Repetitive work *reduces* labor.

    Args:
        level: Building level (2-40). Level 1 = ground floor (1.0).

    Returns:
        Multiplier to apply to core-area labor hours.

    Example:
        >>> durand_core_multiplier(20)
        0.81
    """
    if level <= 1:
        return 1.0
    return _DURAND_CORE_TABLE.get(level, 0.75)


# ── Crew Size Calculator (Durand Section 23) ─────────────────────────────────


@dataclass
class CrewSizeEstimate:
    """Result of a crew-size calculation."""

    average_crew_size: float
    maximum_crew_size: float
    working_days: int
    non_working_foremen: int
    total_crew_including_supervision: int
    method: str

    def summary(self) -> dict[str, object]:
        return {
            "average_crew_size": round(self.average_crew_size, 2),
            "maximum_crew_size": round(self.maximum_crew_size, 2),
            "working_days": self.working_days,
            "non_working_foremen": self.non_working_foremen,
            "total_crew_including_supervision": self.total_crew_including_supervision,
            "method": self.method,
        }


def crew_size_straight_line(
    man_hours: float,
    calendar_days: int,
    work_days_per_week: int = 5,
    work_hours_per_day: int = 8,
) -> CrewSizeEstimate:
    """Calculate crew size using the Durand straight-line method.

    Formula:
        WD = CD / 7 * WDPW
        ACS = MH / WHPD / WD
        MCS = ACS * 1.4

    Non-working supervision: 1 foreman per 6 electricians (when MCS >= 6).

    Args:
        man_hours: Total estimated man-hours for the project.
        calendar_days: Project duration in calendar days.
        work_days_per_week: Working days per week (default 5).
        work_hours_per_day: Work hours per day (default 8).

    Returns:
        CrewSizeEstimate with average/max crew size and supervision needs.

    Example:
        >>> est = crew_size_straight_line(3000, 90)
        >>> est.average_crew_size
        5.86
        >>> est.maximum_crew_size
        8.2
    """
    working_days = max(1, round(calendar_days / 7 * work_days_per_week))
    acs = man_hours / work_hours_per_day / working_days
    mcs = acs * 1.4
    foremen = int(mcs // 6) if mcs >= 6 else 0
    total_crew = int(round(mcs)) + foremen
    return CrewSizeEstimate(
        average_crew_size=acs,
        maximum_crew_size=mcs,
        working_days=working_days,
        non_working_foremen=foremen,
        total_crew_including_supervision=total_crew,
        method="straight_line",
    )


def crew_size_curve(average_crew_size: float) -> float:
    """Calculate maximum crew size using the Durand curve method.

    MCS = ACS * 1.4. The curve method accounts for overlapping
    activities (conduit, wire, fixtures, trim) that peak mid-project.

    Args:
        average_crew_size: Average crew size from straight-line method.

    Returns:
        Maximum crew size at project peak.
    """
    return average_crew_size * 1.4


def non_working_supervision(crew_size: int | float) -> int:
    """Calculate non-working foremen needed.

    Rule: 1 non-working foreman per 6 working electricians.

    Args:
        crew_size: Number of working electricians.

    Returns:
        Number of non-working foremen required.
    """
    if crew_size < 6:
        return 0
    return int(crew_size // 6)