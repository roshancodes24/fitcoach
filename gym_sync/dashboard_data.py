from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .coaching import build_morning_briefing
from .insights import (
    _planned_session,
    _recovery_zone,
    build_report,
    fetch_exercises_for_date,
    load_config,
)

IST = ZoneInfo("Asia/Kolkata")


def fetch_import_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source, file_path, imported_at, records
        FROM import_log
        ORDER BY imported_at DESC
        LIMIT 10
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_recent_workouts(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT session_id, date, day_name, duration_min, workout_min, exercise_count, total_volume, start_time
        FROM jefit_sessions
        ORDER BY date DESC, start_time DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_workout_detail(conn: sqlite3.Connection, day: str) -> dict[str, Any] | None:
    session = conn.execute(
        """
        SELECT session_id, date, day_name, duration_min, workout_min, exercise_count, total_volume, start_time, end_time
        FROM jefit_sessions
        WHERE date = ?
        ORDER BY start_time DESC
        LIMIT 1
        """,
        (day,),
    ).fetchone()
    if not session:
        return None
    exercises = [dict(row) for row in fetch_exercises_for_date(conn, day)]
    whoop = conn.execute("SELECT * FROM whoop_daily WHERE date = ?", (day,)).fetchone()
    return {
        "session": dict(session),
        "exercises": exercises,
        "whoop": dict(whoop) if whoop else None,
    }


def build_week_view(conn: sqlite3.Connection, config_path: Path, anchor: date | None = None) -> list[dict[str, Any]]:
    config = load_config(config_path)
    thresholds = config.get("recovery_thresholds", {"green": 67, "yellow": 50})
    today = anchor or datetime.now(IST).date()
    monday = today - timedelta(days=today.weekday())

    week: list[dict[str, Any]] = []
    for offset in range(7):
        d = monday + timedelta(days=offset)
        key = d.isoformat()
        planned = _planned_session(config, d)
        whoop = conn.execute("SELECT recovery, sleep_hours, hrv FROM whoop_daily WHERE date = ?", (key,)).fetchone()
        sessions = conn.execute(
            "SELECT day_name, duration_min FROM jefit_sessions WHERE date = ? ORDER BY start_time",
            (key,),
        ).fetchall()
        actual = " + ".join(s["day_name"] for s in sessions if s["day_name"]) if sessions else None
        recovery = whoop["recovery"] if whoop else None
        week.append(
            {
                "date": key,
                "weekday": d.strftime("%a"),
                "weekday_full": d.strftime("%A"),
                "is_today": key == today.isoformat(),
                "planned": planned,
                "actual": actual,
                "status": _day_status(planned, actual, key, today.isoformat()),
                "recovery": recovery,
                "recovery_zone": _recovery_zone(recovery, thresholds),
                "sleep_hours": whoop["sleep_hours"] if whoop else None,
                "hrv": whoop["hrv"] if whoop else None,
            }
        )
    return week


def _day_status(planned: str, actual: str | None, day: str, today: str) -> str:
    if planned == "OFF":
        return "rest" if not actual else "extra"
    if actual:
        return "done"
    if day < today:
        return "missed"
    if day == today:
        return "today"
    return "upcoming"


def build_dashboard_payload(
    conn: sqlite3.Connection,
    config_path: Path,
    days: int = 14,
) -> dict[str, Any]:
    report = build_report(conn, config_path, days=days)
    workouts = fetch_recent_workouts(conn, limit=8)
    imports = fetch_import_status(conn)
    week = build_week_view(conn, config_path)

    latest_workout = None
    if workouts:
        latest_workout = fetch_workout_detail(conn, workouts[0]["date"])

    config = load_config(config_path)
    briefing = build_morning_briefing(conn, config_path)

    return {
        "report": report,
        "week": week,
        "recent_workouts": workouts,
        "latest_workout": latest_workout,
        "imports": imports,
        "schedule": config.get("schedule", {}),
        "user": config.get("user", {}),
        "briefing": briefing,
    }
