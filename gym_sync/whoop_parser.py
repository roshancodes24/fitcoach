from __future__ import annotations

import csv
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=IST)


def _cycle_date(row: dict[str, str]) -> str | None:
    wake = _parse_dt(row.get("Wake onset"))
    if wake:
        return wake.date().isoformat()
    dt = _parse_dt(row.get("Cycle start time"))
    return dt.date().isoformat() if dt else None


def _workout_date(row: dict[str, str]) -> str | None:
    dt = _parse_dt(row.get("Workout start time"))
    return dt.date().isoformat() if dt else None


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _extract_whoop_files(source: Path) -> dict[str, Path]:
    if source.is_dir():
        files = {p.name: p for p in source.glob("*.csv")}
        return files
    if source.suffix.lower() == ".zip":
        target = source.parent / "whoop_export"
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            archive.extractall(target)
        return {p.name: p for p in target.glob("*.csv")}
    raise ValueError(f"Unsupported Whoop export: {source}")


def parse_whoop_export(source: Path) -> dict[str, list[dict]]:
    files = _extract_whoop_files(source)
    daily: list[dict] = []
    workouts: list[dict] = []
    journal: list[dict] = []

    cycles_path = files.get("physiological_cycles.csv")
    if cycles_path:
        for row in _read_csv_rows(cycles_path):
            date = _cycle_date(row)
            if not date:
                continue
            recovery_raw = row.get("Recovery score %", "")
            try:
                recovery = float(recovery_raw) if recovery_raw else None
            except ValueError:
                recovery = None
            if recovery == 1:
                continue
            asleep = row.get("Asleep duration (min)")
            daily.append(
                {
                    "date": date,
                    "recovery": recovery,
                    "hrv": _float(row.get("Heart rate variability (ms)")),
                    "rhr": _float(row.get("Resting heart rate (bpm)")),
                    "day_strain": _float(row.get("Day Strain")),
                    "sleep_hours": round(float(asleep) / 60, 2) if asleep else None,
                    "sleep_performance": _float(row.get("Sleep performance %")),
                    "sleep_debt_min": _float(row.get("Sleep debt (min)")),
                    "deep_min": _float(row.get("Deep (SWS) duration (min)")),
                    "rem_min": _float(row.get("REM duration (min)")),
                    "calories": _int(row.get("Energy burned (cal)")),
                }
            )

    workouts_path = files.get("workouts.csv")
    if workouts_path:
        for row in _read_csv_rows(workouts_path):
            date = _workout_date(row)
            if not date:
                continue
            workouts.append(
                {
                    "date": date,
                    "start_time": row.get("Workout start time"),
                    "activity": row.get("Activity name"),
                    "duration_min": _float(row.get("Duration (min)")),
                    "strain": _float(row.get("Activity Strain")),
                    "avg_hr": _float(row.get("Average HR (bpm)")),
                    "calories": _float(row.get("Energy burned (cal)")),
                }
            )

    journal_path = files.get("journal_entries.csv")
    if journal_path:
        for row in _read_csv_rows(journal_path):
            date = _cycle_date(row)
            question = row.get("Question text", "").strip()
            if not date or not question:
                continue
            answered = str(row.get("Answered yes", "")).lower() == "true"
            journal.append({"date": date, "question": question, "answered_yes": answered})

    return {"daily": daily, "workouts": workouts, "journal": journal, "source_file": str(source)}


def _float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def load_whoop_into_db(conn, source: Path) -> int:
    from datetime import datetime, timezone

    from .db import log_import

    data = parse_whoop_export(source)
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for row in data["daily"]:
        conn.execute(
            """
            INSERT INTO whoop_daily (
                date, recovery, hrv, rhr, day_strain, sleep_hours, sleep_performance,
                sleep_debt_min, deep_min, rem_min, calories, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                recovery=excluded.recovery,
                hrv=excluded.hrv,
                rhr=excluded.rhr,
                day_strain=excluded.day_strain,
                sleep_hours=excluded.sleep_hours,
                sleep_performance=excluded.sleep_performance,
                sleep_debt_min=excluded.sleep_debt_min,
                deep_min=excluded.deep_min,
                rem_min=excluded.rem_min,
                calories=excluded.calories,
                updated_at=excluded.updated_at
            """,
            (
                row["date"],
                row["recovery"],
                row["hrv"],
                row["rhr"],
                row["day_strain"],
                row["sleep_hours"],
                row["sleep_performance"],
                row["sleep_debt_min"],
                row["deep_min"],
                row["rem_min"],
                row["calories"],
                now,
            ),
        )
        count += 1

    for row in data["workouts"]:
        conn.execute(
            """
            INSERT INTO whoop_workouts (date, start_time, activity, duration_min, strain, avg_hr, calories)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, start_time, activity) DO UPDATE SET
                duration_min=excluded.duration_min,
                strain=excluded.strain,
                avg_hr=excluded.avg_hr,
                calories=excluded.calories
            """,
            (
                row["date"],
                row["start_time"],
                row["activity"],
                row["duration_min"],
                row["strain"],
                row["avg_hr"],
                row["calories"],
            ),
        )
        count += 1

    for row in data["journal"]:
        conn.execute(
            """
            INSERT INTO whoop_journal (date, question, answered_yes)
            VALUES (?, ?, ?)
            ON CONFLICT(date, question) DO UPDATE SET answered_yes=excluded.answered_yes
            """,
            (row["date"], row["question"], 1 if row["answered_yes"] else 0),
        )
        count += 1

    conn.commit()
    log_import(conn, "whoop", str(source), count)
    return count
