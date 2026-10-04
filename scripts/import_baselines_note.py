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
import html
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
_OTHER_HEADING = re.compile(r"^##(?!#)\s*(?P<title>.+?)\s*$")  # ### and deeper stay inside a block
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
    without_time = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "", text)
    try:
        d = _parse_one_date(without_time, year, None)
    except ValueError:
        first = re.search(r"[A-Za-z]{3}[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?", without_time)
        if not first:
            raise
        d = _parse_one_date(first.group(), year, None)
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
        "stand time avg": "stand_min_day",
        "avg speed": "avg_speed_kmh",
        "weekly km": "total_km",
        "calories/day": "calories_kcal_day",
        "protein/day": "protein_g_day",
        "protein avg": "protein_g_day",
        "protein": "protein_g_day",
        "magnesium": "magnesium_mg_day",
        "magnesium/day": "magnesium_mg_day",
        "potassium/day": "potassium_mg_day",
        "rem avg": "rem_min",
        "nrem total avg": "nrem_min",
    }
    out: list[MetricInput] = []
    if label in simple:
        m = _num_metric(simple[label], value)
        return [m] if m else []
    if label in ("water avg/day", "hydration"):
        n = first_number(value)
        if n is None:
            return []
        if re.search(r"\d\s*(?:L|l|litres?|liters?)\b", value) and "ml" not in value.lower():
            n *= 1000  # stored as mL
        return [MetricInput(key="water_avg_ml_day", value=n)]
    if label == "hrv avg":
        if m := _num_metric("hrv_avg_ms", value):
            out.append(m)
        if b := re.search(r"baseline[:\s]*(\d+(?:\.\d+)?)", notes):
            out.append(MetricInput(key="hrv_baseline_ms", value=float(b[1])))
        return out
    if label == "body fat %":
        if m := _num_metric("body_fat_pct", value):
            out.append(m)
        if b := re.search(r"LBM[:\s]*(\d+(?:\.\d+)?)", notes):
            out.append(MetricInput(key="lbm_kg", value=float(b[1])))
        return out
    if label == "sleep consistency":
        out.append(MetricInput(key="sleep_consistency", value_text=value.strip()))
        if b := re.search(r"(\d+(?:\.\d+)?)\s*min", notes):
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
        if bp := re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", value):
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
    if label == "training load":
        return _load_metrics(value)
    return None


def _load_metrics(text: str) -> list[MetricInput]:
    """CTL/ATL/TSB values from text like `CTL 40.0 · ATL 45.5 · TSB −5.5`."""
    return [
        MetricInput(key=name.lower(), value=n)
        for name, num in re.findall(r"\b(CTL|ATL|TSB)\b\s*[:=]?\s*([+−\-]?[\d.]+)", text)
        if (n := first_number(num)) is not None
    ]


@dataclass
class _Block:
    kind: str  # "period" | "supplement"
    header: re.Match[str]
    lines: list[str] = field(default_factory=list)


_META_LABELS = (
    "Report generated",
    "Period covered",
    "Key findings",
    "Flags",
    "Training load context",
    "Validated interventions",
)


def _norm(line: str) -> str:
    """
    Strip; move a colon written inside a bold label outside it (`**Flags:**` → `**Flags**:`)
    and bold a known label written plainly (`Report generated:` → `**Report generated**:`).
    """
    line = re.sub(r"^\*\*([^*]+?):\*\*", r"**\1**:", line.strip())
    for label in _META_LABELS:
        if line.startswith(f"{label}:") or line.startswith(f"{label} ("):
            return f"**{label}**{line[len(label) :]}"
    return line


# --- Flat notes ----------------------------------------------------------------
# Claude's Apple Notes writes store the whole note as one paragraph: headings,
# labels and table rows run together. reflow_flat() rebuilds the line structure
# the parser expects. Table rows are recovered by counting cells (the column count
# comes from the header's |---| separator), which is exact even with empty cells.

_TABLE_HEAD = re.compile(r"\|(?P<cells>(?:[^|\n]*\|){1,12}?)\s*(?P<sep>\|(?:\s*:?-{2,}:?\s*\|)+)")
_BARE_HEAD = "Metric | Value | Notes"


def _is_flat(text: str) -> bool:
    headings = len(re.findall(r"(?<!#)##(?!#) ", text))
    line_headings = len(re.findall(r"(?m)^\s*##(?!#) ", text))
    return headings > line_headings


def _reflow_piped_tables(s: str) -> str:
    out: list[str] = []
    pos = 0
    while m := _TABLE_HEAD.search(s, pos):
        n = m["sep"].count("|") - 1
        head = [c.strip() for c in m["cells"].split("|")[:-1]]
        if len(head) != n:
            out.append(s[pos : m.end()])
            pos = m.end()
            continue
        out.append(s[pos : m.start()])
        out.append("\n| " + " | ".join(head) + " |\n|" + "---|" * n + "\n")
        i = m.end()
        while True:
            j = i
            while j < len(s) and s[j] == " ":
                j += 1
            if j >= len(s) or s[j] != "|":
                break
            cells: list[str] = []
            k = j + 1
            for _ in range(n):
                nxt = s.find("|", k)
                if nxt == -1:
                    break
                cells.append(s[k:nxt].strip())
                k = nxt + 1
            if len(cells) < n:
                break
            out.append("| " + " | ".join(cells) + " |\n")
            i = k
        pos = i
    out.append(s[pos:])
    return "".join(out)


def _known_labels() -> list[str]:
    probe = [
        "sleep score",
        "sleep consistency",
        "recovery status",
        "steps avg/day",
        "water avg/day",
        "hrv avg",
        "rhr avg",
        "resp rate (sleep)",
        "spo2 avg",
        "vo2 max",
        "hr recovery 1-min",
        "body weight",
        "body fat %",
        "cycling sessions",
        "total km",
        "total elevation",
        "avg cadence",
        "avg speed",
        "other activity",
        "stand hours avg",
        "stand time avg",
        "calories/day",
        "protein/day",
        "protein avg",
        "protein",
        "hydration",
        "magnesium",
        "caffeine",
        "magnesium/day",
        "potassium/day",
        "alcohol",
        "deep sleep avg (raw)",
        "deep sleep avg",
        "rem avg",
        "nrem total avg",
        "blood pressure",
        "training status",
        "training load",
        "weekly km",
    ]
    return sorted(probe, key=len, reverse=True)


def _split_label(chunk: str, guess: bool) -> tuple[str, str] | None:
    """Split `previous row's notes NextLabel` → (notes, label), by known label or a guess."""
    low = chunk.replace("₂", "2").lower()
    for lab in _known_labels():
        if low.endswith(lab) and (len(low) == len(lab) or low[-len(lab) - 1] == " "):
            cut = len(chunk) - len(lab)
            return chunk[:cut].strip(), chunk[cut:].strip()
    m = re.search(r"(?:[.!?;)\]]|—)\s+([A-Z][^.!?;)\]—|]{0,40})$", chunk) if guess else None
    if m and len(m[1].split()) <= 5:
        return chunk[: m.start(1)].strip(), m[1].strip()
    return None


def _reflow_bare_table(line: str) -> list[str] | None:
    """`Metric | Value | Notes L1 | V1 | N1 L2 | V2 | N2 …` (no outer pipes) → piped rows."""
    if not line.startswith(_BARE_HEAD):
        return None
    parts = [c.strip() for c in line[len(_BARE_HEAD) :].split(" | ")]
    if len(parts) < 2:
        return None
    rows = ["| Metric | Value | Notes |", "|---|---|---|"]
    label, i = parts[0], 1
    while i < len(parts):
        value = parts[i]
        rest = parts[i + 1 :]
        if len(rest) <= 1:  # final row: value plus optional notes
            rows.append(f"| {label} | {value} | {rest[0] if rest else ''} |")
            break
        found = None
        # Prefer a known next label, letting notes contain " | " (merge up to 2 more parts);
        # otherwise guess the label starts after the last sentence end in the notes.
        for guess in (False, True):
            for extra in range(1 if guess else 3):
                if extra + 1 >= len(rest):
                    break
                split = _split_label(" | ".join(rest[: extra + 1]), guess)
                if split:
                    found = (split, extra)
                    break
            if found:
                break
        if found is None:
            rows.append(f"| {label} | {value} | {rest[0]} |")
            rows.append(f"| ?unsplit | {' | '.join(rest[1:])} |")
            break
        (notes, next_label), extra = found
        rows.append(f"| {label} | {value} | {notes} |")
        label, i = next_label, i + 2 + extra
    return rows


def reflow_flat(text: str) -> str:
    s = " ".join(text.split())
    s = re.sub(r"\s*(?=(?<!#)##(?!#) )", "\n\n", s)
    s = re.sub(r"(##\s+(?:Period|Week) of [^\n()]*\(reported [^)\n]*\))\s*", r"\1\n", s)
    # consume each **label** whole so opening/closing markers pair left to right
    s = re.sub(r"\s*(\*\*[^*\n]{1,60}\*\*)", r"\n\1", s)
    labels = "|".join(map(re.escape, _META_LABELS))
    s = re.sub(rf"(?<=[^\s|])\s+(?=(?:{labels})(?::|\s\())", "\n", s)
    s = re.sub(r"(?<=[^\s|])\s+(?=Metric \| Value \| Notes(?!\s*\|))", "\n", s)
    s = _reflow_piped_tables(s)
    lines: list[str] = []
    for line in s.splitlines():
        rows = _reflow_bare_table(line.strip())
        lines.extend(rows if rows is not None else [line])
    return "\n".join(lines)


def _split_blocks(text: str) -> list[_Block]:
    blocks: list[_Block] = []
    for line in text.splitlines():
        if m := _HEADER.match(line.strip()):
            blocks.append(_Block("period", m))
        elif line.lstrip().startswith("#") and (m := _SUPPLEMENT.search(line)):
            # only a heading starts a supplement block; an inline **… supplement for
            # period …** label is part of the period it sits in
            blocks.append(_Block("supplement", m))
        elif m := _OTHER_HEADING.match(line.strip()):
            # Any other `##` heading ends the previous block, so its content never
            # leaks into a period; parse_note reports it as skipped.
            blocks.append(_Block("unknown", m))
        elif blocks:
            blocks[-1].lines.append(line)
    return blocks


def _split_numbered(text: str) -> list[str]:
    """`1. a 2. b 3. c` → [a, b, c]; numbers must run 1, 2, 3… so a stray `Oct 2.` isn't a split."""
    if not re.match(r"1[.)]\s", text):
        return [text]
    starts: list[tuple[int, int]] = []
    pos, n = 0, 1
    while m := re.compile(rf"(?:^|\s){n}[.)]\s").search(text, pos):
        starts.append((m.start(), m.end()))
        pos, n = m.end(), n + 1
    items = [
        text[b : (starts[i + 1][0] if i + 1 < len(starts) else len(text))].strip()
        for i, (_, b) in enumerate(starts)
    ]
    return [x for x in items if x]


def _fill(period: ParsedPeriod, lines: list[str], warnings: list[str]) -> None:
    mode = "metrics"
    findings_open = False
    for raw in lines:
        line = _norm(raw)
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
        if mode == "flags" and (b := re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", line)):
            period.flags.append(b[1].strip())
            continue
        mode = "metrics" if line.startswith("**") else mode
        if line.startswith("**Key findings**"):
            findings_open = True
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                period.findings.extend(_split_numbered(rest))
            continue
        if line.startswith("**Flags**"):
            findings_open = False
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if not rest:
                mode = "flags"  # flags follow as a bullet list
            elif rest.lower().strip(" .") not in {"none", "—"}:
                # "·" and ";" separate flags; a comma does only when neither is used
                # (flags themselves often contain commas).
                pattern = r"[·;]" if re.search(r"[·;]", rest) else r","
                parts = [f.strip() for f in re.split(pattern, rest)]
                period.flags = [f for f in parts if f.lower().strip(" .") not in {"", "none", "—"}]
            continue
        if line.startswith("**Training load context**"):
            findings_open = False
            for m in _load_metrics(line):
                period.metrics.setdefault(m.key, m)
            continue
        if line.startswith("**"):
            findings_open = False
            continue
        if findings_open and (b := re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", line)):
            period.findings.append(b[1].strip())


def parse_note(text: str) -> ParsedNote:
    if _is_flat(text):
        text = reflow_flat(text)
    warnings: list[str] = []
    periods: list[ParsedPeriod] = []
    supplements: list[_Block] = []
    for block in _split_blocks(text):
        if block.kind == "supplement":
            supplements.append(block)
            continue
        if block.kind == "unknown":
            title = block.header["title"]
            title = title if len(title) <= 60 else title[:57] + "..."
            warnings.append(f"skipped unrecognised heading {title!r}")
            continue
        try:
            reported_text = block.header["reported"]
            fallback_year = int(y[0]) if (y := re.search(r"\d{4}", reported_text)) else None
            start, end = parse_date_range(block.header["range"], fallback_year)
            try:
                generated: datetime | None = _parse_reported(reported_text, end.year)
            except ValueError:
                generated = None  # an in-block **Report generated** line may still provide it
            days = float((end - start).days + 1)
            for line in map(_norm, block.lines):
                if line.startswith("**Report generated**") and (m := _ISO_TS.search(line)):
                    generated = datetime.fromisoformat(m.group()).replace(tzinfo=None)
                if line.startswith("**Period covered**"):
                    covered = line.split(":", 1)[1]
                    start, end = parse_date_range(covered, end.year)
                    if d := re.search(r"\(([\d.]+)\s*days?\b", covered):
                        days = float(d[1])
                    else:
                        days = float((end - start).days + 1)
            if generated is None:
                raise ValueError(f"unrecognised report date {reported_text!r}")
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

    # report_generated is a period's identity in the store. Two blocks reported the same
    # day without a time would collapse into one period, so nudge the older one (later in
    # the file — the note is newest-first) back by a second per clash and say so.
    seen: set[datetime] = set()
    for p in periods:
        original = p.report_generated
        while p.report_generated in seen:
            p.report_generated -= timedelta(seconds=1)
        if p.report_generated != original:
            warnings.append(
                f"[{p.label}] duplicate report date {original.isoformat()}; "
                f"stored as {p.report_generated.isoformat()}"
            )
        seen.add(p.report_generated)

    for p in periods:
        if not p.metrics:
            warnings.append(f"[{p.label}] no metrics parsed")
    return ParsedNote(periods=periods, warnings=warnings)


def write_periods(parsed: ParsedNote) -> dict[str, Any]:
    written = 0
    skipped: list[str] = []
    # oldest first so the store's creation order matches history
    for p in sorted(parsed.periods, key=lambda p: p.report_generated):
        stamp = p.report_generated.isoformat()
        period = start_report_period(p.cp_start.isoformat(), p.cp_end.isoformat(), stamp, p.days)
        if (period["cp_start"], period["cp_end"]) != (p.cp_start.isoformat(), p.cp_end.isoformat()):
            skipped.append(
                f"[{p.label}] store already has {period['cp_start']}→{period['cp_end']} "
                f"for report date {stamp}; not overwritten"
            )
            continue
        pid = period["period_id"]
        if p.metrics:
            upsert_report_metrics(pid, list(p.metrics.values()), IMPORT_SOURCE)
        # Date imported notes to their period, not to the import run, so `since`
        # filters and the ~8-week intervention cache see their real age.
        findings = [NoteInput(text=t, noted_at=stamp) for t in p.findings]
        flags = [NoteInput(text=t, noted_at=stamp) for t in p.flags]
        interventions = [
            n if n.noted_at else n.model_copy(update={"noted_at": stamp}) for n in p.interventions
        ]
        add_report_notes(pid, "finding", findings, IMPORT_SOURCE, replace=True)
        add_report_notes(pid, "flag", flags, IMPORT_SOURCE, replace=True)
        add_report_notes(pid, "validated_intervention", interventions, IMPORT_SOURCE, replace=True)
        written += 1
    return {"periods": written, "skipped": skipped}


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
    raw = Path(paths[0]).read_text(encoding="utf-8")
    if raw.lstrip().startswith("<"):  # Notes.app HTML body (osascript `body of note`)
        raw = re.sub(r"<br\s*/?>|</(?:div|p|li|h\d)>", "\n", raw)
        raw = html.unescape(re.sub(r"<[^>]+>", "", raw))
    parsed = parse_note(raw)
    _print(parsed)
    if not parsed.periods:
        print("No period blocks found — nothing to import.")
        return 1
    if dry_run:
        print("\nDry run — nothing written.")
        return 0
    result = write_periods(parsed)
    for line in result["skipped"]:
        print(f"  SKIPPED {line}")
    print(f"\nImported {result['periods']} periods.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
