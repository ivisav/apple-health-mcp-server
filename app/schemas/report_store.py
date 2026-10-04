import math
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field, field_validator, model_validator

ReportNoteKind = Literal["finding", "flag", "recommendation", "validated_intervention"]
NOTE_KINDS: tuple[str, ...] = ("finding", "flag", "recommendation", "validated_intervention")
TrendGranularity = Literal["week", "month"]
Direction = Literal["higher_is_better", "lower_is_better", "neutral"]


class CoreMetric(NamedTuple):
    unit: str | None
    direction: Direction


# Canonical keys the report skills write. Units live in the key name so a key
# never silently changes meaning. Unknown keys are still accepted (open set).
CORE_METRICS: dict[str, CoreMetric] = {
    "sleep_score": CoreMetric("score/100", "higher_is_better"),
    "sleep_consistency": CoreMetric(None, "neutral"),
    "sleep_consistency_stddev_min": CoreMetric("min", "lower_is_better"),
    "sleep_duration_h": CoreMetric("h", "higher_is_better"),
    "deep_sleep_raw_min": CoreMetric("min", "higher_is_better"),
    "rem_min": CoreMetric("min", "higher_is_better"),
    "nrem_min": CoreMetric("min", "higher_is_better"),
    "recovery_status": CoreMetric("score/10", "higher_is_better"),
    "training_status": CoreMetric(None, "neutral"),
    "body_battery_est": CoreMetric("score/100", "higher_is_better"),
    "hrv_avg_ms": CoreMetric("ms", "higher_is_better"),
    "hrv_baseline_ms": CoreMetric("ms", "neutral"),
    "rhr_avg_bpm": CoreMetric("bpm", "lower_is_better"),
    "resp_rate_sleep_brpm": CoreMetric("br/min", "neutral"),
    "spo2_avg_pct": CoreMetric("%", "higher_is_better"),
    "vo2max": CoreMetric("mL/min·kg", "higher_is_better"),
    "hrr_1min_bpm": CoreMetric("bpm", "higher_is_better"),
    "weight_avg_kg": CoreMetric("kg", "lower_is_better"),
    "body_fat_pct": CoreMetric("%", "lower_is_better"),
    "lbm_kg": CoreMetric("kg", "higher_is_better"),
    "steps_avg_day": CoreMetric("steps/day", "higher_is_better"),
    "water_avg_ml_day": CoreMetric("mL/day", "higher_is_better"),
    "stand_h_day": CoreMetric("h/day", "higher_is_better"),
    "stand_min_day": CoreMetric("min/day", "higher_is_better"),
    "other_activity": CoreMetric(None, "neutral"),
    "cycling_sessions": CoreMetric("rides", "neutral"),
    "total_km": CoreMetric("km", "neutral"),
    "total_elev_m": CoreMetric("m", "neutral"),
    "avg_speed_kmh": CoreMetric("km/h", "higher_is_better"),
    "avg_cadence_rpm": CoreMetric("rpm", "neutral"),
    "ctl": CoreMetric("CTL", "higher_is_better"),
    "atl": CoreMetric("ATL", "neutral"),
    "tsb": CoreMetric("TSB", "neutral"),
    "calories_kcal_day": CoreMetric("kcal/day", "neutral"),
    "protein_g_day": CoreMetric("g/day", "higher_is_better"),
    "magnesium_mg_day": CoreMetric("mg/day", "higher_is_better"),
    "potassium_mg_day": CoreMetric("mg/day", "higher_is_better"),
    "alcohol_drinks": CoreMetric("drinks", "lower_is_better"),
    "bp_systolic_mmhg": CoreMetric("mmHg", "lower_is_better"),
    "bp_diastolic_mmhg": CoreMetric("mmHg", "lower_is_better"),
    "bp_category": CoreMetric(None, "neutral"),
}


class MetricInput(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_]+$", max_length=64)
    value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    note: str | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("value must be a finite number")
        return v

    @model_validator(mode="after")
    def _has_value(self) -> "MetricInput":
        if self.value is None and not self.value_text:
            raise ValueError(f"metric {self.key!r} needs value or value_text")
        return self


class NoteInput(BaseModel):
    text: str = Field(min_length=1)
    subject: str | None = None
    noted_at: str | None = None  # ISO date/datetime; defaults to now
