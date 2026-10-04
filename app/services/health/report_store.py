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
        created_at TIMESTAMP DEFAULT now(),
        position INTEGER NOT NULL DEFAULT 0  -- write order within one call
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
            for position, (n, at) in enumerate(zip(notes, noted_at, strict=True)):
                con.execute(
                    """
                    INSERT INTO report_notes
                        (period_id, kind, subject, text, source_skill, created_at, position)
                    VALUES (?::UUID, ?, ?, ?, ?, COALESCE(?::TIMESTAMP, now()), ?)
                    """,
                    [pid, kind, n.subject, n.text, source, at, position],
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        store.checkpoint(con)

    return {"period_id": pid, "kind": kind, "added": len(notes), "replaced": replaced}


MAX_LAST_N = 52
MAX_NOTES = 500


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def get_report_history(
    metrics: list[str] | None = None,
    last_n: int = 4,
    include_notes: bool = True,
) -> dict[str, Any]:
    if not 1 <= last_n <= MAX_LAST_N:
        raise ValueError(f"last_n must be between 1 and {MAX_LAST_N}")
    key_filter = " AND list_contains(?, m.key)" if metrics else ""
    key_params: list[Any] = [metrics] if metrics else []

    with store.lock, store.connect() as con:
        periods = con.execute(
            f"""
            SELECT {_PERIOD_COLS} FROM report_periods
            WHERE {_NON_EMPTY}
            ORDER BY report_generated DESC LIMIT ?
            """,
            [last_n],
        ).fetchall()
        result: dict[str, Any] = {"periods": [_period_dict(p) for p in periods], "metrics": {}}
        if include_notes:
            result["notes"] = {}
        if metrics is None:
            result["keys_in_use"] = {}
        if not periods:
            return result

        ids = [str(p[0]) for p in periods]
        rows = con.execute(
            f"""
            SELECT CAST(m.period_id AS VARCHAR), m.key, m.value, m.value_text, m.unit
            FROM report_metrics m
            WHERE list_contains(?, CAST(m.period_id AS VARCHAR)){key_filter}
            ORDER BY m.key
            """,
            [ids, *key_params],
        ).fetchall()
        extremes = con.execute(
            f"""
            SELECT m.key, max(m.value), arg_max(p.cp_end, m.value),
                   min(m.value), arg_min(p.cp_end, m.value)
            FROM report_metrics m JOIN report_periods p ON p.id = m.period_id
            WHERE m.value IS NOT NULL{key_filter}
            GROUP BY m.key
            """,
            key_params,
        ).fetchall()
        if include_notes:
            for kind, text in con.execute(
                """
                SELECT kind, text FROM report_notes
                WHERE period_id = ?::UUID AND kind IN ('finding', 'flag')
                ORDER BY created_at, position
                """,
                [ids[0]],
            ).fetchall():
                result["notes"].setdefault(kind, []).append(text)
        if metrics is None:
            result["keys_in_use"] = {
                k: n
                for k, n in con.execute(
                    "SELECT key, count(*) FROM report_metrics GROUP BY key ORDER BY key",
                ).fetchall()
            }

    pos = {pid: i for i, pid in enumerate(ids)}
    table: dict[str, dict[str, Any]] = {}
    for pid, key, value, value_text, unit in rows:
        entry = table.setdefault(key, {"unit": unit, "values": [None] * len(ids)})
        entry["values"][pos[pid]] = value if value is not None else value_text
        entry["unit"] = entry["unit"] or unit
    ext = {k: (mx, mx_end, mn, mn_end) for k, mx, mx_end, mn, mn_end in extremes}
    for key, entry in table.items():
        nums = [n for n in (_num(v) for v in entry["values"]) if n is not None]
        latest = _num(entry["values"][0])
        prev = _num(entry["values"][1]) if len(ids) > 1 else None
        core = CORE_METRICS.get(key)
        entry["direction"] = core.direction if core else "unknown"
        entry["avg_last_n"] = round(sum(nums) / len(nums), 3) if nums else None
        entry["delta_vs_prev"] = (
            round(latest - prev, 3) if latest is not None and prev is not None else None
        )
        if key in ext:
            mx, mx_end, mn, mn_end = ext[key]
            entry["all_time_max"] = {"value": mx, "cp_end": mx_end.isoformat()}
            entry["all_time_min"] = {"value": mn, "cp_end": mn_end.isoformat()}
        else:
            entry["all_time_max"] = entry["all_time_min"] = None
    result["metrics"] = table
    return result


def get_report_trend(
    metrics: list[str],
    granularity: str = "week",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    if not metrics:
        raise ValueError("metrics must list at least one key")
    if granularity not in ("week", "month"):
        raise ValueError(f"granularity must be 'week' or 'month', got {granularity!r}")
    bucket = (
        "printf('%d-W%02d', p.iso_year, p.iso_week)"
        if granularity == "week"
        else "strftime(p.cp_end, '%Y-%m')"
    )
    where = ["EXISTS (SELECT 1 FROM report_metrics m2 WHERE m2.period_id = p.id)"]
    params: list[Any] = []
    if date_from:
        where.append("p.cp_end >= ?")
        params.append(_parse_date(date_from, "date_from"))
    if date_to:
        where.append("p.cp_end <= ?")
        params.append(_parse_date(date_to, "date_to"))
    where_sql = " AND ".join(where)

    with store.lock, store.connect() as con:
        buckets = con.execute(
            f"""
            SELECT {bucket} AS bucket, count(*), sum(p.days)
            FROM report_periods p WHERE {where_sql}
            GROUP BY bucket ORDER BY bucket
            """,
            params,
        ).fetchall()
        values = con.execute(
            f"""
            SELECT {bucket} AS bucket, m.key, sum(m.value * p.days) / sum(p.days)
            FROM report_metrics m JOIN report_periods p ON p.id = m.period_id
            WHERE m.value IS NOT NULL AND list_contains(?, m.key) AND {where_sql}
            GROUP BY bucket, m.key
            """,
            [metrics, *params],
        ).fetchall()

    labels = [b[0] for b in buckets]
    pos = {b: i for i, b in enumerate(labels)}
    series: dict[str, list[float | None]] = {k: [None] * len(labels) for k in metrics}
    for b, key, v in values:
        series[key][pos[b]] = round(float(v), 3)
    return {
        "granularity": granularity,
        "buckets": labels,
        "n_periods": [int(b[1]) for b in buckets],
        "days": [float(b[2]) for b in buckets],
        "series": series,
    }


def get_report_notes(
    kinds: list[str] | None = None,
    subject: str | None = None,
    since: str | None = None,
    latest_per_subject: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if kinds:
        bad = [k for k in kinds if k not in NOTE_KINDS]
        if bad:
            raise ValueError(f"unknown note kind(s) {bad}; valid: {list(NOTE_KINDS)}")
    if not 1 <= limit <= MAX_NOTES:
        raise ValueError(f"limit must be between 1 and {MAX_NOTES}")
    where = ["1=1"]
    params: list[Any] = []
    if kinds:
        where.append("list_contains(?, n.kind)")
        params.append(kinds)
    if subject:
        where.append("lower(n.subject) = lower(?)")
        params.append(subject)
    if since:
        where.append("n.created_at >= ?")
        params.append(_parse_timestamp(since, "since"))
    qualify = ""
    if latest_per_subject:
        where.append("n.subject IS NOT NULL")
        qualify = (
            "QUALIFY row_number() OVER "
            "(PARTITION BY n.kind, lower(n.subject) ORDER BY n.created_at DESC) = 1"
        )

    with store.lock, store.connect() as con:
        rows = con.execute(
            f"""
            SELECT CAST(n.id AS VARCHAR), CAST(n.period_id AS VARCHAR), p.cp_end, n.kind,
                   n.subject, n.text, n.source_skill, n.created_at
            FROM report_notes n JOIN report_periods p ON p.id = n.period_id
            WHERE {" AND ".join(where)}
            {qualify}
            ORDER BY n.created_at DESC, n.position
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [
        {
            "id": r[0],
            "period_id": r[1],
            "cp_end": r[2].isoformat(),
            "kind": r[3],
            "subject": r[4],
            "text": r[5],
            "source_skill": r[6],
            "created_at": r[7].isoformat(),
        }
        for r in rows
    ]
