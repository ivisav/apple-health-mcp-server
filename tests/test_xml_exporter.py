import io

from scripts.xml_exporter import XMLExporter

SAMPLE_XML = b"""<HealthData>
<Workout workoutActivityType="HKWorkoutActivityTypeWalking" duration="30"
    durationUnit="min" sourceName="iPhone"
    startDate="2026-01-01 00:00:00 +0000" endDate="2026-01-01 00:30:00 +0000">
<WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned"
    startDate="2026-01-01 00:00:00 +0000" endDate="2026-01-01 00:30:00 +0000"
    sum="100" unit="kcal"/>
<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning"
    startDate="2026-01-01 00:00:00 +0000" endDate="2026-01-01 00:30:00 +0000"
    sum="2.5" unit="km"/>
</Workout>
</HealthData>"""


def test_workout_statistics_are_captured_not_cleared() -> None:
    exporter = XMLExporter.__new__(XMLExporter)
    exporter.chunk_size = 50000
    exporter.cutoff_date = None

    frames = list(exporter.parse_xml(source=io.BytesIO(SAMPLE_XML)))
    stats = next(df for df in frames if set(df.columns) == set(exporter.WORKOUT_STATS_COLUMNS))

    assert len(stats) == 2
    assert stats["type"].notna().all()
    assert stats["startDate"].notna().all()
    assert set(stats["sum"]) == {100.0, 2.5}


# Apple's export lists every food-<Correlation> member <Record> twice: once nested
# inside the <Correlation> and once again at top level. Only the top-level copy
# should be imported.
CORRELATION_XML = b"""<HealthData>
 <Record type="HKQuantityTypeIdentifierDietaryFatTotal" sourceName="MacroFactor"
    unit="g" creationDate="2026-08-05 20:14:41 +0000"
    startDate="2026-08-05 14:00:58 +0000" endDate="2026-08-05 14:01:58 +0000" value="0.174"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch"
    unit="count" creationDate="2026-08-05 12:00:00 +0000"
    startDate="2026-08-05 12:00:00 +0000" endDate="2026-08-05 12:01:00 +0000" value="42"/>
 <Correlation type="HKCorrelationTypeIdentifierFood" sourceName="MacroFactor"
    creationDate="2026-08-05 20:14:41 +0000"
    startDate="2026-08-05 14:00:58 +0000" endDate="2026-08-05 14:01:58 +0000">
  <MetadataEntry key="HKFoodType" value="Lemon"/>
  <Record type="HKQuantityTypeIdentifierDietaryFatTotal" sourceName="MacroFactor"
    unit="g" creationDate="2026-08-05 20:14:41 +0000"
    startDate="2026-08-05 14:00:58 +0000" endDate="2026-08-05 14:01:58 +0000" value="0.174"/>
 </Correlation>
</HealthData>"""


def test_correlation_nested_records_are_not_double_counted() -> None:
    exporter = XMLExporter.__new__(XMLExporter)
    exporter.chunk_size = 50000
    exporter.cutoff_date = None

    frames = list(exporter.parse_xml(source=io.BytesIO(CORRELATION_XML)))
    records = next(
        df for df in frames if set(df.columns) == set(exporter.RECORD_COLUMNS) and len(df)
    )

    # Only the two top-level <Record>s, not the <Correlation>-nested duplicate.
    assert len(records) == 2
    assert sorted(records["type"]) == [
        "HKQuantityTypeIdentifierDietaryFatTotal",
        "HKQuantityTypeIdentifierStepCount",
    ]


# Blood pressure readings follow the same shape: Apple wraps the systolic,
# diastolic, and (often) heart-rate samples in a HKCorrelationTypeIdentifierBloodPressure
# <Correlation>, and each member <Record> is duplicated as a top-level <Record>.
BLOOD_PRESSURE_XML = b"""<HealthData>
 <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="Withings"
    unit="mmHg" creationDate="2026-08-05 08:00:10 +0000"
    startDate="2026-08-05 08:00:00 +0000" endDate="2026-08-05 08:00:00 +0000" value="118"/>
 <Record type="HKQuantityTypeIdentifierBloodPressureDiastolic" sourceName="Withings"
    unit="mmHg" creationDate="2026-08-05 08:00:10 +0000"
    startDate="2026-08-05 08:00:00 +0000" endDate="2026-08-05 08:00:00 +0000" value="76"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Withings"
    unit="count/min" creationDate="2026-08-05 08:00:10 +0000"
    startDate="2026-08-05 08:00:00 +0000" endDate="2026-08-05 08:00:00 +0000" value="62"/>
 <Correlation type="HKCorrelationTypeIdentifierBloodPressure" sourceName="Withings"
    creationDate="2026-08-05 08:00:10 +0000"
    startDate="2026-08-05 08:00:00 +0000" endDate="2026-08-05 08:00:00 +0000">
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="Withings"
    unit="mmHg" creationDate="2026-08-05 08:00:10 +0000"
    startDate="2026-08-05 08:00:00 +0000" endDate="2026-08-05 08:00:00 +0000" value="118"/>
  <Record type="HKQuantityTypeIdentifierBloodPressureDiastolic" sourceName="Withings"
    unit="mmHg" creationDate="2026-08-05 08:00:10 +0000"
    startDate="2026-08-05 08:00:00 +0000" endDate="2026-08-05 08:00:00 +0000" value="76"/>
  <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Withings"
    unit="count/min" creationDate="2026-08-05 08:00:10 +0000"
    startDate="2026-08-05 08:00:00 +0000" endDate="2026-08-05 08:00:00 +0000" value="62"/>
 </Correlation>
</HealthData>"""


def test_blood_pressure_correlation_nested_records_are_not_double_counted() -> None:
    exporter = XMLExporter.__new__(XMLExporter)
    exporter.chunk_size = 50000
    exporter.cutoff_date = None

    frames = list(exporter.parse_xml(source=io.BytesIO(BLOOD_PRESSURE_XML)))
    records = next(
        df for df in frames if set(df.columns) == set(exporter.RECORD_COLUMNS) and len(df)
    )

    # Only the three top-level <Record>s, not the <Correlation>-nested duplicates.
    assert len(records) == 3
    assert sorted(records["type"]) == [
        "HKQuantityTypeIdentifierBloodPressureDiastolic",
        "HKQuantityTypeIdentifierBloodPressureSystolic",
        "HKQuantityTypeIdentifierHeartRate",
    ]
