import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import duckdb

from app.config import settings
from app.schemas.report_store import CORE_METRICS, NOTE_KINDS, MetricInput, NoteInput
from app.services.health.writable_duckdb import WritableDuckDB

REPORT_STORE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS report_periods (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        report_generated TIMESTAMP NOT NULL UNIQUE,
        cp_start DATE NOT NULL,
        cp_end DATE NOT NULL,
        days DOUBLE NOT NULL,
        iso_year INTEGER NOT NULL,
        iso_week INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS report_metrics (
        period_id UUID NOT NULL,
        key VARCHAR NOT NULL,
        value DOUBLE,
        value_text VARCHAR,
        unit VARCHAR,
        note VARCHAR,
        source_skill VARCHAR NOT NULL,
        updated_at TIMESTAMP DEFAULT now(),
        PRIMARY KEY (period_id, key)
    );
    CREATE TABLE IF NOT EXISTS report_notes (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        period_id UUID NOT NULL,
        kind VARCHAR NOT NULL,
        subject VARCHAR,
        text VARCHAR NOT NULL,
        source_skill VARCHAR NOT NULL,
        created_at TIMESTAMP DEFAULT now()
    );
"""

store = WritableDuckDB(path=settings.REPORTS_DUCKDB_FILENAME, schema=REPORT_STORE_SCHEMA)

_PERIOD_COLS = "id, cp_start, cp_end, report_generated, days, iso_year, iso_week"
# A period with no metrics is a run that never got to its writeback (crash,
# timeout); it must never be treated as a previous period.
_NON_EMPTY = "EXISTS (SELECT 1 FROM report_metrics m WHERE m.period_id = report_periods.id)"


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from e


def _parse_timestamp(value: str, field: str) -> datetime:
    """ISO date or datetime; any UTC offset is dropped — stored as local wall-clock time."""
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be an ISO 8601 timestamp, got {value!r}") from e


def _parse_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"period_id must be a UUID, got {value!r}") from e


def _require_source(source_skill: str) -> str:
    if not source_skill or not source_skill.strip():
        raise ValueError("source_skill must name the skill writing this data")
    return source_skill.strip()


def _require_period(con: duckdb.DuckDBPyConnection, period_id: str) -> None:
    if (
        con.execute("SELECT 1 FROM report_periods WHERE id = ?::UUID", [period_id]).fetchone()
        is None
    ):
        raise ValueError(f"No report period with id={period_id}; call start_report_period first")


def _default_unit(key: str) -> str | None:
    core = CORE_METRICS.get(key)
    return core.unit if core else None


def _period_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "period_id": str(row[0]),
        "cp_start": row[1].isoformat(),
        "cp_end": row[2].isoformat(),
        "report_generated": row[3].isoformat(),
        "days": float(row[4]),
        "iso_year": int(row[5]),
        "iso_week": int(row[6]),
    }


def start_report_period(
    cp_start: str,
    cp_end: str,
    report_generated: str,
    days: float | None = None,
) -> dict[str, Any]:
    start = _parse_date(cp_start, "cp_start")
    end = _parse_date(cp_end, "cp_end")
    if end < start:
        raise ValueError("cp_end must be on or after cp_start")
    generated = _parse_timestamp(report_generated, "report_generated")
    if days is None:
        days = float((end - start).days + 1)
    if days <= 0:
        raise ValueError("days must be positive")
    iso_year, iso_week, _ = end.isocalendar()

    with store.lock, store.connect() as con:
        row = con.execute(
            f"SELECT {_PERIOD_COLS} FROM report_periods WHERE report_generated = ?",
            [generated],
        ).fetchone()
        created = row is None
        if created:
            row = con.execute(
                f"""
                INSERT INTO report_periods
                    (cp_start, cp_end, report_generated, days, iso_year, iso_week)
                VALUES (?, ?, ?, ?, ?, ?)
                RETURNING {_PERIOD_COLS}
                """,
                [start, end, generated, days, iso_year, iso_week],
            ).fetchone()
            store.checkpoint(con)
        assert row is not None
        previous = con.execute(
            f"""
            SELECT {_PERIOD_COLS} FROM report_periods
            WHERE report_generated < ? AND {_NON_EMPTY}
            ORDER BY report_generated DESC LIMIT 2
            """,
            [generated],
        ).fetchall()

    return {
        **_period_dict(row),
        "created": created,
        "previous_periods": [_period_dict(r) for r in previous],
    }


def upsert_report_metrics(
    period_id: str,
    metrics: Sequence[MetricInput | dict[str, Any]],
    source_skill: str,
) -> dict[str, Any]:
    items = [m if isinstance(m, MetricInput) else MetricInput.model_validate(m) for m in metrics]
    if not items:
        raise ValueError("metrics must not be empty")
    source = _require_source(source_skill)
    keys = [m.key for m in items]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"duplicate metric keys in one call: {dupes}")
    pid = _parse_uuid(period_id)

    with store.lock, store.connect() as con:
        _require_period(con, pid)
        known = {r[0] for r in con.execute("SELECT DISTINCT key FROM report_metrics").fetchall()}
        con.execute("BEGIN TRANSACTION")
        try:
            for m in items:
                con.execute(
                    """
                    INSERT INTO report_metrics
                        (period_id, key, value, value_text, unit, note, source_skill, updated_at)
                    VALUES (?::UUID, ?, ?, ?, ?, ?, ?, now())
                    ON CONFLICT (period_id, key) DO UPDATE SET
                        value = EXCLUDED.value,
                        value_text = EXCLUDED.value_text,
                        unit = EXCLUDED.unit,
                        note = EXCLUDED.note,
                        source_skill = EXCLUDED.source_skill,
                        updated_at = EXCLUDED.updated_at
                    """,
                    [
                        pid,
                        m.key,
                        m.value,
                        m.value_text,
                        m.unit or _default_unit(m.key),
                        m.note,
                        source,
                    ],
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        store.checkpoint(con)

    new_keys = sorted({k for k in keys if k not in CORE_METRICS and k not in known})
    return {"period_id": pid, "upserted": len(items), "new_keys": new_keys}


def add_report_notes(
    period_id: str,
    kind: str,
    items: Sequence[NoteInput | dict[str, Any] | str],
    source_skill: str,
    replace: bool = True,
) -> dict[str, Any]:
    if kind not in NOTE_KINDS:
        raise ValueError(f"kind must be one of {list(NOTE_KINDS)}, got {kind!r}")
    notes = [
        n
        if isinstance(n, NoteInput)
        else NoteInput(text=n)
        if isinstance(n, str)
        else NoteInput.model_validate(n)
        for n in items
    ]
    noted_at = [_parse_timestamp(n.noted_at, "noted_at") if n.noted_at else None for n in notes]
    source = _require_source(source_skill)
    pid = _parse_uuid(period_id)

    with store.lock, store.connect() as con:
        _require_period(con, pid)
        con.execute("BEGIN TRANSACTION")
        try:
            replaced = 0
            if replace:
                replaced = len(
                    con.execute(
                        """
                        DELETE FROM report_notes
                        WHERE period_id = ?::UUID AND kind = ? AND source_skill = ?
                        RETURNING id
                        """,
                        [pid, kind, source],
                    ).fetchall()
                )
            for n, at in zip(notes, noted_at, strict=True):
                con.execute(
                    """
                    INSERT INTO report_notes
                        (period_id, kind, subject, text, source_skill, created_at)
                    VALUES (?::UUID, ?, ?, ?, ?, COALESCE(?::TIMESTAMP, now()))
                    """,
                    [pid, kind, n.subject, n.text, source, at],
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        store.checkpoint(con)

    return {"period_id": pid, "kind": kind, "added": len(notes), "replaced": replaced}
