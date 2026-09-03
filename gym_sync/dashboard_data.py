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
from .whoop_api import is_connected
from .whoop_auto import sync_whoop

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


def build_trends(daily_log: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact time series for dashboard charts (no invented values)."""
    dates: list[str] = []
    labels: list[str] = []
    recovery: list[float | None] = []
    sleep_hours: list[float | None] = []
    hrv: list[float | None] = []
    volume: list[float | None] = []
    sessions: list[str | None] = []
    for row in daily_log:
        day = row.get("date") or ""
        dates.append(day)
        labels.append(day[5:] if len(day) >= 10 else day)
        recovery.append(row.get("recovery"))
        sleep_hours.append(row.get("sleep_hours"))
        hrv.append(row.get("hrv"))
        vol = row.get("total_volume")
        volume.append(float(vol) if vol is not None else None)
        sessions.append(row.get("jefit_session"))
    return {
        "dates": dates,
        "labels": labels,
        "recovery": recovery,
        "sleep_hours": sleep_hours,
        "hrv": hrv,
        "volume": volume,
        "sessions": sessions,
    }


VOLUME_RANGES = frozenset({"7d", "month", "year"})


def _volume_by_date(
    conn: sqlite3.Connection, start: date, end: date
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT date,
               SUM(COALESCE(total_volume, 0)) AS volume,
               GROUP_CONCAT(day_name, ' + ') AS sessions,
               COUNT(*) AS session_count
        FROM jefit_sessions
        WHERE date >= ? AND date <= ?
        GROUP BY date
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["date"]
        vol = row["volume"]
        out[key] = {
            "volume": float(vol) if vol is not None else 0.0,
            "sessions": row["sessions"],
            "session_count": int(row["session_count"] or 0),
        }
    return out


def _volume_by_month(
    conn: sqlite3.Connection, start: date, end: date
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', date) AS month,
               SUM(COALESCE(total_volume, 0)) AS volume,
               COUNT(*) AS session_count
        FROM jefit_sessions
        WHERE date >= ? AND date <= ?
        GROUP BY month
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        month = row["month"]
        if not month:
            continue
        vol = row["volume"]
        out[month] = {
            "volume": float(vol) if vol is not None else 0.0,
            "session_count": int(row["session_count"] or 0),
        }
    return out


def build_volume_trends(
    conn: sqlite3.Connection,
    range_key: str = "7d",
) -> dict[str, Any]:
    """Training volume series from real Jefit sessions (zeros for empty buckets)."""
    key = (range_key or "7d").strip().lower()
    if key not in VOLUME_RANGES:
        key = "7d"

    today = datetime.now(IST).date()
    dates: list[str] = []
    labels: list[str] = []
    volume: list[float] = []
    sessions: list[str | None] = []
    session_counts: list[int] = []

    if key == "year":
        # Last 12 calendar months including the current month.
        first_of_this_month = today.replace(day=1)
        start_month = first_of_this_month
        for _ in range(11):
            prev = start_month - timedelta(days=1)
            start_month = prev.replace(day=1)
        # Inclusive end: last day of current month is fine; SQL uses date <= today.
        by_month = _volume_by_month(conn, start_month, today)
        cursor = start_month
        for _ in range(12):
            ym = cursor.strftime("%Y-%m")
            bucket = by_month.get(ym, {"volume": 0.0, "session_count": 0})
            dates.append(ym)
            labels.append(cursor.strftime("%b"))
            volume.append(float(bucket["volume"] or 0.0))
            count = int(bucket["session_count"] or 0)
            session_counts.append(count)
            sessions.append(f"{count} session{'s' if count != 1 else ''}" if count else None)
            # Advance one month
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
        granularity = "month"
    else:
        day_count = 30 if key == "month" else 7
        start = today - timedelta(days=day_count - 1)
        by_day = _volume_by_date(conn, start, today)
        for i in range(day_count):
            d = start + timedelta(days=i)
            iso = d.isoformat()
            bucket = by_day.get(iso)
            dates.append(iso)
            labels.append(iso[5:])
            if bucket:
                volume.append(float(bucket["volume"] or 0.0))
                sessions.append(bucket.get("sessions"))
                session_counts.append(int(bucket.get("session_count") or 0))
            else:
                volume.append(0.0)
                sessions.append(None)
                session_counts.append(0)
        granularity = "day"

    return {
        "range": key,
        "granularity": granularity,
        "dates": dates,
        "labels": labels,
        "volume": volume,
        "sessions": sessions,
        "session_counts": session_counts,
    }


def _maybe_sync_whoop(conn: sqlite3.Connection, config_path: Path) -> None:
    """Refresh Whoop when connected and today's vitals row is missing."""
    if not is_connected():
        return
    today_key = datetime.now(IST).date().isoformat()
    if conn.execute("SELECT 1 FROM whoop_daily WHERE date = ?", (today_key,)).fetchone():
        return
    try:
        sync_whoop(conn, load_config(config_path), force="api")
    except Exception:
        pass


def build_sleep_detail(
    conn: sqlite3.Connection,
    config_path: Path,
    days: int = 14,
) -> dict[str, Any]:
    """Today sleep vitals plus recent history for the sleep detail panel."""
    config = load_config(config_path)
    user = config.get("user") or {}
    sleep_target = float(user.get("sleep_target_hours") or 7.5)
    today = datetime.now(IST).date()
    today_key = today.isoformat()
    span = max(1, min(int(days or 14), 90))
    start_key = (today - timedelta(days=span - 1)).isoformat()

    whoop = conn.execute("SELECT * FROM whoop_daily WHERE date = ?", (today_key,)).fetchone()
    today_block: dict[str, Any] | None = None
    if whoop:
        row = dict(whoop)
        today_block = {
            "date": today_key,
            "sleep_hours": row.get("sleep_hours"),
            "sleep_performance": row.get("sleep_performance"),
            "sleep_target_hours": sleep_target,
            "sleep_debt_min": row.get("sleep_debt_min"),
            "deep_min": row.get("deep_min"),
            "rem_min": row.get("rem_min"),
            # Whoop light only — never estimate from total − deep − REM
            "light_min": row.get("light_min"),
        }

    history_rows = conn.execute(
        """
        SELECT date, sleep_hours, sleep_performance, sleep_debt_min, deep_min, rem_min, light_min
        FROM whoop_daily
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC
        """,
        (start_key, today_key),
    ).fetchall()

    dates: list[str] = []
    labels: list[str] = []
    sleep_hours: list[float | None] = []
    sleep_performance: list[float | None] = []
    for row in history_rows:
        day = row["date"] or ""
        dates.append(day)
        labels.append(day[5:] if len(day) >= 10 else day)
        sleep_hours.append(row["sleep_hours"])
        sleep_performance.append(row["sleep_performance"])

    return {
        "today": today_block,
        "sleep_target_hours": sleep_target,
        "history": {
            "dates": dates,
            "labels": labels,
            "sleep_hours": sleep_hours,
            "sleep_performance": sleep_performance,
        },
    }


def build_dashboard_payload(
    conn: sqlite3.Connection,
    config_path: Path,
    days: int = 14,
) -> dict[str, Any]:
    _maybe_sync_whoop(conn, config_path)
    report = build_report(conn, config_path, days=days)
    workouts = fetch_recent_workouts(conn, limit=8)
    imports = fetch_import_status(conn)
    week = build_week_view(conn, config_path)

    latest_workout = None
    if workouts:
        latest_workout = fetch_workout_detail(conn, workouts[0]["date"])

    config = load_config(config_path)
    briefing = build_morning_briefing(conn, config_path)
    trends = build_trends(report.get("daily_log") or [])
    volume_trends = build_volume_trends(conn, "7d")

    return {
        "report": report,
        "week": week,
        "recent_workouts": workouts,
        "latest_workout": latest_workout,
        "imports": imports,
        "schedule": config.get("schedule", {}),
        "user": config.get("user", {}),
        "briefing": briefing,
        "trends": trends,
        "volume_trends": volume_trends,
    }
