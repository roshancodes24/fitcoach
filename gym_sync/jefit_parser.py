from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SECTION_RE = re.compile(r"^###\s+(.+?)\s+#+")


def _parse_sections(text: str) -> dict[str, list[list[dict[str, str]]]]:
    sections: dict[str, list[list[dict[str, str]]]] = {}
    current_name: str | None = None
    header: list[str] | None = None
    rows: list[dict[str, str]] = []

    def flush_table() -> None:
        nonlocal header, rows
        if current_name and header is not None and rows:
            sections.setdefault(current_name, []).append(rows)
        header = None
        rows = []

    def flush_section() -> None:
        flush_table()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_table()
            continue
        if line.startswith("################################################"):
            continue
        match = SECTION_RE.match(line)
        if match:
            flush_section()
            current_name = match.group(1).strip().upper()
            header = None
            continue
        if current_name is None:
            continue
        if header is None:
            flush_table()
            header = next(csv.reader([line]))
            continue
        row = next(csv.reader([line]))
        if len(row) < len(header):
            row.extend([""] * (len(header) - len(row)))
        rows.append(dict(zip(header, row)))
    flush_section()
    return sections


def _flatten_tables(section_tables: list[list[dict[str, str]]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    for table in section_tables:
        merged.extend(table)
    return merged


def _parse_plan_day_names(section_tables: list[list[dict[str, str]]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for table in section_tables:
        if not table:
            continue
        if "day_completed_timestamp" not in table[0] or "name" not in table[0]:
            continue
        for row in table:
            day_id = row.get("_id")
            name = row.get("name")
            if day_id and name:
                names[str(day_id)] = name
    return names


def _parse_day_item_to_plan(section_tables: list[list[dict[str, str]]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for table in section_tables:
        if not table:
            continue
        if "belongplan" not in table[0] or "_id" not in table[0]:
            continue
        for row in table:
            item_id = row.get("_id")
            plan_id = row.get("belongplan")
            if item_id and plan_id:
                mapping[str(item_id)] = str(plan_id)
    return mapping


def _unix_to_local(ts: str | int | float | None) -> datetime | None:
    if ts in (None, "", "0"):
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value, tz=IST)


def _parse_set_logs(logs: str) -> list[tuple[float, int]]:
    sets: list[tuple[float, int]] = []
    for part in (logs or "").split(","):
        part = part.strip()
        if not part or "x" not in part.lower():
            continue
        weight_s, reps_s = part.lower().split("x", 1)
        try:
            weight = float(weight_s)
            reps = int(float(reps_s))
        except ValueError:
            continue
        sets.append((weight, reps))
    return sets


def _exercise_stats(logs: str) -> dict[str, Any]:
    sets = _parse_set_logs(logs)
    if not sets:
        return {"sets_count": 0, "top_weight": None, "top_reps": None, "volume": 0.0}
    top_weight, top_reps = max(sets, key=lambda s: (s[0], s[1]))
    volume = sum(w * r for w, r in sets)
    return {
        "sets_count": len(sets),
        "top_weight": top_weight,
        "top_reps": top_reps,
        "volume": volume,
    }


def _infer_day_name(exercises: list[str]) -> str | None:
    names = " ".join(exercises).lower()
    if any(k in names for k in ("leg press", "squat", "lunge", "leg curl", "leg extension", "calf", "rdl", "deadlift", "hip thrust")):
        if any(k in names for k in ("curl", "pulldown", "row", "bench", "press", "fly", "pushdown")):
            return "Mixed"
        return "Legs"
    if any(k in names for k in ("pulldown", "row", "pull-up", "curl", "face pull")) and not any(
        k in names for k in ("bench", "fly", "pushdown", "tricep")
    ):
        return "Pull"
    if any(k in names for k in ("bench", "fly", "pushdown", "tricep", "shoulder press", "lateral raise")):
        return "Push"
    return None


def parse_jefit_export(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    sections = _parse_sections(text)
    plan_day_names = _parse_plan_day_names(sections.get("ROUTINES", []))
    day_item_to_plan = _parse_day_item_to_plan(sections.get("ROUTINES", []))

    sessions: dict[int, dict[str, Any]] = {}
    for row in _flatten_tables(sections.get("WORKOUT SESSIONS", [])):
        try:
            session_id = int(row["_id"])
        except (KeyError, ValueError):
            continue
        start_dt = _unix_to_local(row.get("starttime"))
        end_dt = _unix_to_local(row.get("endtime"))
        date = start_dt.date().isoformat() if start_dt else None
        sessions[session_id] = {
            "session_id": session_id,
            "date": date,
            "start_time": start_dt.isoformat() if start_dt else None,
            "end_time": end_dt.isoformat() if end_dt else None,
            "duration_min": round(float(row.get("total_time") or 0) / 60, 1),
            "workout_min": round(float(row.get("workout_time") or 0) / 60, 1),
            "exercise_count": int(float(row.get("total_exercise") or 0)),
            "total_volume": float(row.get("total_weight") or 0),
            "day_name": None,
        }

    exercises_by_session: dict[int, list[dict[str, Any]]] = {}
    for row in _flatten_tables(sections.get("EXERCISE LOGS", [])):
        try:
            session_id = int(row["belongsession"])
        except (KeyError, ValueError):
            continue
        logs = row.get("logs", "")
        stats = _exercise_stats(logs)
        day_item_id = str(row.get("day_item_id", ""))
        plan_id = day_item_to_plan.get(day_item_id)
        exercise = {
            "session_id": session_id,
            "date": row.get("mydate"),
            "exercise_name": row.get("ename", "").strip(),
            "logs": logs,
            "plan_id": plan_id,
            **stats,
        }
        exercises_by_session.setdefault(session_id, []).append(exercise)

    for session_id, session in sessions.items():
        exercises = exercises_by_session.get(session_id, [])
        plan_ids = [ex["plan_id"] for ex in exercises if ex.get("plan_id")]
        if plan_ids:
            top_plan = max(set(plan_ids), key=plan_ids.count)
            session["day_name"] = plan_day_names.get(top_plan)
        if not session.get("day_name"):
            ex_names = [e["exercise_name"] for e in exercises]
            session["day_name"] = _infer_day_name(ex_names)

    return {
        "sessions": list(sessions.values()),
        "exercises": [ex for items in exercises_by_session.values() for ex in items],
        "source_file": str(path),
    }


def load_jefit_into_db(conn, path: Path) -> int:
    from datetime import datetime, timezone

    from .db import log_import

    data = parse_jefit_export(path)
    now = datetime.now(timezone.utc).isoformat()
    count = 0

    for session in data["sessions"]:
        if not session.get("date"):
            continue
        conn.execute(
            """
            INSERT INTO jefit_sessions (
                session_id, date, start_time, end_time, duration_min, workout_min,
                exercise_count, total_volume, day_name, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                date=excluded.date,
                start_time=excluded.start_time,
                end_time=excluded.end_time,
                duration_min=excluded.duration_min,
                workout_min=excluded.workout_min,
                exercise_count=excluded.exercise_count,
                total_volume=excluded.total_volume,
                day_name=excluded.day_name,
                updated_at=excluded.updated_at
            """,
            (
                session["session_id"],
                session["date"],
                session["start_time"],
                session["end_time"],
                session["duration_min"],
                session["workout_min"],
                session["exercise_count"],
                session["total_volume"],
                session["day_name"],
                now,
            ),
        )
        count += 1

    session_ids = [s["session_id"] for s in data["sessions"] if s.get("date")]
    if session_ids:
        placeholders = ",".join("?" * len(session_ids))
        conn.execute(f"DELETE FROM jefit_exercises WHERE session_id IN ({placeholders})", session_ids)
    for exercise in data["exercises"]:
        if not exercise.get("date"):
            continue
        conn.execute(
            """
            INSERT INTO jefit_exercises (
                session_id, date, exercise_name, logs, sets_count, top_weight, top_reps, volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exercise["session_id"],
                exercise["date"],
                exercise["exercise_name"],
                exercise["logs"],
                exercise["sets_count"],
                exercise["top_weight"],
                exercise["top_reps"],
                exercise["volume"],
            ),
        )
        count += 1

    conn.commit()
    log_import(conn, "jefit", str(path), count)
    return count
