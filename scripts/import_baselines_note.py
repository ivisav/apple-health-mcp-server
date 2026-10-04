"""
One-time import of the "Weekly Health Baselines" Apple Note into the report store.

Copy the note's text into a Markdown file outside the repo (or under data/),
then:
    uv run scripts/import_baselines_note.py path/to/note.md --dry-run
    uv run scripts/import_baselines_note.py path/to/note.md

Idempotent: periods are keyed by their report_generated timestamp, metrics are
upserted and imported notes replaced, so re-running after fixing the export is safe.
"""

import calendar
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.schemas.report_store import MetricInput, NoteInput
from app.services.health.report_store import (
    add_report_notes,
    start_report_period,
    upsert_report_metrics,
)

IMPORT_SOURCE = "import_apple_notes"

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ISO_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_HEADER = re.compile(
    r"^##\s+(?:Period|Week) of\s+(?P<range>.+?)\s*\(reported\s+(?P<reported>[^)]+)\)\s*$"
)
_SUPPLEMENT = re.compile(r"supplement for period\s+(?P<range>.+?)\s*\**\s*$", re.IGNORECASE)
_EMPTY = {"", "—", "-", "–", "n/a", "na"}


@dataclass
class ParsedPeriod:
    cp_start: date
    cp_end: date
    report_generated: datetime
    days: float
    metrics: dict[str, MetricInput] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    interventions: list[NoteInput] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.cp_start}→{self.cp_end}"


@dataclass
class ParsedNote:
    periods: list[ParsedPeriod]  # file order: newest first
    warnings: list[str]


def first_number(text: str) -> float | None:
    cleaned = text.replace("−", "-").replace(",", "").replace("~", "")
    m = _NUMBER.search(cleaned)
    return float(m.group()) if m else None


def _parse_one_date(text: str, year: int | None, month: int | None) -> date:
    t = " ".join(text.replace(",", " ").split()).strip(" .")
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:\s+(\d{4}))?", t):
        y = int(m[3]) if m[3] else year
        if y is None:
            raise ValueError(f"no year for date {text!r}")
        return date(y, _MONTHS[m[1].lower()], int(m[2]))
    if m := re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?(?:\s+(\d{4}))?", t):
        y = int(m[3]) if m[3] else year
        if y is None:
            raise ValueError(f"no year for date {text!r}")
        return date(y, _MONTHS[m[2].lower()], int(m[1]))
    if (m := re.fullmatch(r"(\d{1,2})(?:\s+(\d{4}))?", t)) and month:
        y = int(m[2]) if m[2] else year
        if y is None:
            raise ValueError(f"no year for date {text!r}")
        return date(y, month, int(m[1]))
    raise ValueError(f"unrecognised date {text!r}")


def parse_date_range(text: str, fallback_year: int | None) -> tuple[date, date]:
    text = re.sub(r"\([^)]*\)", "", text).strip()
    parts = re.split(r"\s*[–—]\s*|\s+-\s+|\s+to\s+", text)
    if len(parts) != 2:
        raise ValueError(f"unrecognised date range {text!r}")
    left, right = parts
    year_m = re.search(r"\b(\d{4})\b", right)
    left_has_year = re.search(r"\b\d{4}\b", left) is not None
    year = int(year_m[1]) if year_m else fallback_year
    start = _parse_one_date(left, year, None)
    end = _parse_one_date(right, year, start.month)
    if start > end and not left_has_year:
        start = start.replace(year=start.year - 1)
    if start > end:
        raise ValueError(f"range ends before it starts: {text!r}")
    return start, end


def _parse_reported(text: str, year: int) -> datetime:
    if m := _ISO_TS.search(text):
        return datetime.fromisoformat(m.group()).replace(tzinfo=None)
    d = _parse_one_date(text, year, None)
    return datetime(d.year, d.month, d.day)


def _label(cell: str) -> str:
    return " ".join(cell.replace("*", "").replace("₂", "2").lower().split())


def _num_metric(key: str, value: str) -> MetricInput | None:
    n = first_number(value)
    return MetricInput(key=key, value=n) if n is not None else None


def _row_metrics(label: str, value: str, notes: str) -> list[MetricInput] | None:
    """Map one table row to metrics. None = unmapped label; [] = mapped but empty."""
    if value.strip().lower() in _EMPTY:
        return []
    simple = {
        "sleep score": "sleep_score",
        "recovery status": "recovery_status",
        "steps avg/day": "steps_avg_day",
        "water avg/day": "water_avg_ml_day",
        "rhr avg": "rhr_avg_bpm",
        "resp rate (sleep)": "resp_rate_sleep_brpm",
        "spo2 avg": "spo2_avg_pct",
        "vo2 max": "vo2max",
        "hr recovery 1-min": "hrr_1min_bpm",
        "body weight": "weight_avg_kg",
        "cycling sessions": "cycling_sessions",
        "total km": "total_km",
        "total elevation": "total_elev_m",
        "avg cadence": "avg_cadence_rpm",
        "stand hours avg": "stand_h_day",
        "calories/day": "calories_kcal_day",
        "protein/day": "protein_g_day",
        "magnesium/day": "magnesium_mg_day",
        "potassium/day": "potassium_mg_day",
        "rem avg": "rem_min",
        "nrem total avg": "nrem_min",
    }
    out: list[MetricInput] = []
    if label in simple:
        m = _num_metric(simple[label], value)
        return [m] if m else []
    if label == "hrv avg":
        if m := _num_metric("hrv_avg_ms", value):
            out.append(m)
        if b := re.search(r"baseline\s*([\d.]+)", notes):
            out.append(MetricInput(key="hrv_baseline_ms", value=float(b[1])))
        return out
    if label == "body fat %":
        if m := _num_metric("body_fat_pct", value):
            out.append(m)
        if b := re.search(r"LBM\s*([\d.]+)", notes):
            out.append(MetricInput(key="lbm_kg", value=float(b[1])))
        return out
    if label == "sleep consistency":
        out.append(MetricInput(key="sleep_consistency", value_text=value.strip()))
        if b := re.search(r"([\d.]+)\s*min", notes):
            out.append(MetricInput(key="sleep_consistency_stddev_min", value=float(b[1])))
        return out
    if label.startswith("deep sleep avg"):
        n = first_number(value)
        if n is None:
            return []
        if "raw" not in label and "1.65" in value + notes:
            n = round(n / 1.65, 2)
        return [MetricInput(key="deep_sleep_raw_min", value=n)]
    if label == "blood pressure":
        if bp := re.search(r"(\d+)\s*/\s*(\d+)", value):
            out += [
                MetricInput(key="bp_systolic_mmhg", value=float(bp[1])),
                MetricInput(key="bp_diastolic_mmhg", value=float(bp[2])),
            ]
        if "—" in notes and (cat := notes.rsplit("—", 1)[1].strip()):
            out.append(MetricInput(key="bp_category", value_text=cat))
        return out
    if label == "alcohol":
        if value.strip().lower().startswith("none"):
            return [MetricInput(key="alcohol_drinks", value=0.0)]
        m = _num_metric("alcohol_drinks", value)
        return [m] if m else []
    if label == "other activity":
        return [MetricInput(key="other_activity", value_text=value.strip())]
    if label == "training status":
        return [MetricInput(key="training_status", value_text=value.strip())]
    return None


@dataclass
class _Block:
    kind: str  # "period" | "supplement"
    header: re.Match[str]
    lines: list[str] = field(default_factory=list)


def _split_blocks(text: str) -> list[_Block]:
    blocks: list[_Block] = []
    for line in text.splitlines():
        if m := _HEADER.match(line.strip()):
            blocks.append(_Block("period", m))
        elif line.lstrip().startswith(("#", "**")) and (m := _SUPPLEMENT.search(line)):
            blocks.append(_Block("supplement", m))
        elif blocks:
            blocks[-1].lines.append(line)
    return blocks


def _fill(period: ParsedPeriod, lines: list[str], warnings: list[str]) -> None:
    mode = "metrics"
    findings_open = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("|"):
            findings_open = False
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells) or _label(cells[0]) in {
                "metric",
                "intervention",
            }:
                continue
            if mode == "interventions":
                if len(cells) >= 3:
                    when = _ISO_TS.search(cells[1])
                    period.interventions.append(
                        NoteInput(
                            text=cells[2],
                            subject=cells[0],
                            noted_at=when.group() if when else None,
                        )
                    )
                continue
            value = cells[1] if len(cells) > 1 else ""
            notes = cells[2] if len(cells) > 2 else ""
            mapped = _row_metrics(_label(cells[0]), value, notes)
            if mapped is None:
                warnings.append(f"[{period.label}] unmapped row: {cells[0]!r}")
                continue
            for m in mapped:
                period.metrics.setdefault(m.key, m)
            continue
        if line.startswith("**Validated interventions**"):
            mode, findings_open = "interventions", False
            continue
        mode = "metrics" if line.startswith("**") else mode
        if line.startswith("**Key findings**"):
            findings_open = True
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                period.findings.append(rest)
            continue
        if line.startswith("**Flags**"):
            findings_open = False
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest.lower().strip(" .") not in {"none", "—", ""}:
                period.flags = [f.strip() for f in rest.split(",") if f.strip()]
            continue
        if line.startswith("**Training load context**"):
            findings_open = False
            for name, num in re.findall(r"\b(CTL|ATL|TSB)\b\s*[:=]?\s*([−\-]?[\d.]+)", line):
                period.metrics.setdefault(
                    name.lower(), MetricInput(key=name.lower(), value=first_number(num))
                )
            continue
        if line.startswith("**"):
            findings_open = False
            continue
        if findings_open and (b := re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", line)):
            period.findings.append(b[1].strip())


def parse_note(text: str) -> ParsedNote:
    warnings: list[str] = []
    periods: list[ParsedPeriod] = []
    supplements: list[_Block] = []
    for block in _split_blocks(text):
        if block.kind == "supplement":
            supplements.append(block)
            continue
        try:
            reported_text = block.header["reported"]
            fallback_year = int(y[0]) if (y := re.search(r"\d{4}", reported_text)) else None
            start, end = parse_date_range(block.header["range"], fallback_year)
            generated = _parse_reported(reported_text, end.year)
            days = float((end - start).days + 1)
            for line in block.lines:
                if line.startswith("**Report generated**") and (m := _ISO_TS.search(line)):
                    generated = datetime.fromisoformat(m.group()).replace(tzinfo=None)
                if line.startswith("**Period covered**"):
                    covered = line.split(":", 1)[1]
                    start, end = parse_date_range(covered, end.year)
                    if d := re.search(r"\(([\d.]+)\s*days?\)", covered):
                        days = float(d[1])
                    else:
                        days = float((end - start).days + 1)
        except (ValueError, KeyError) as e:
            warnings.append(f"skipped block {block.header.group(0)!r}: {e}")
            continue
        period = ParsedPeriod(start, end, generated, days)
        _fill(period, block.lines, warnings)
        periods.append(period)

    for block in supplements:
        try:
            start, end = parse_date_range(
                block.header["range"], None if not periods else periods[0].cp_end.year
            )
        except ValueError as e:
            warnings.append(f"skipped supplement {block.header.group(0)!r}: {e}")
            continue
        target = next((p for p in periods if (p.cp_start, p.cp_end) == (start, end)), None)
        if target is None:
            warnings.append(f"supplement for {start}→{end} matches no period; skipped")
            continue
        _fill(target, block.lines, warnings)

    for p in periods:
        if not p.metrics:
            warnings.append(f"[{p.label}] no metrics parsed")
    return ParsedNote(periods=periods, warnings=warnings)


def write_periods(parsed: ParsedNote) -> dict[str, Any]:
    written = 0
    # oldest first so the store's creation order matches history
    for p in sorted(parsed.periods, key=lambda p: p.report_generated):
        pid = start_report_period(
            p.cp_start.isoformat(),
            p.cp_end.isoformat(),
            p.report_generated.isoformat(),
            p.days,
        )["period_id"]
        if p.metrics:
            upsert_report_metrics(pid, list(p.metrics.values()), IMPORT_SOURCE)
        add_report_notes(pid, "finding", p.findings, IMPORT_SOURCE, replace=True)
        add_report_notes(pid, "flag", p.flags, IMPORT_SOURCE, replace=True)
        add_report_notes(
            pid, "validated_intervention", p.interventions, IMPORT_SOURCE, replace=True
        )
        written += 1
    return {"periods": written}


def _print(parsed: ParsedNote) -> None:
    for p in parsed.periods:
        print(
            f"{p.label} ({p.days:g} d) reported {p.report_generated.isoformat()}  "
            f"metrics={len(p.metrics)} findings={len(p.findings)} flags={len(p.flags)} "
            f"interventions={len(p.interventions)}"
        )
        for key, m in sorted(p.metrics.items()):
            print(f"    {key} = {m.value if m.value is not None else m.value_text}")
    if parsed.warnings:
        print("\nWarnings:")
        for w in parsed.warnings:
            print(f"  {w}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        print(__doc__)
        return 2
    parsed = parse_note(Path(paths[0]).read_text(encoding="utf-8"))
    _print(parsed)
    if not parsed.periods:
        print("No period blocks found — nothing to import.")
        return 1
    if dry_run:
        print("\nDry run — nothing written.")
        return 0
    print(f"\nImported {write_periods(parsed)['periods']} periods.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
