"""Per-muscle recovery estimates from recent Jefit training."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MUSCLE_GROUPS: list[tuple[str, str]] = [
    ("chest", "Chest"),
    ("triceps", "Triceps"),
    ("shoulders", "Shoulders"),
    ("back", "Back"),
    ("abs", "Abs"),
    ("upper_leg", "Upper Leg"),
    ("glutes", "Glutes"),
    ("lower_leg", "Lower Leg"),
    ("forearms", "Forearms"),
    ("biceps", "Biceps"),
    ("cardio", "Cardio"),
]

RECOVERY_HOURS: dict[str, float] = {
    "chest": 72,
    "back": 72,
    "upper_leg": 72,
    "glutes": 60,
    "shoulders": 48,
    "triceps": 48,
    "biceps": 36,
    "abs": 36,
    "lower_leg": 36,
    "forearms": 24,
    "cardio": 24,
}

# Keyword → muscle keys (first match wins for multi-muscle lifts via extras)
EXERCISE_MAP: list[tuple[tuple[str, ...], list[str]]] = [
    (("bench", "chest press", "pec", "fly", "push-up", "pushup", "dip"), ["chest", "triceps", "shoulders"]),
    (("row", "pulldown", "pull-up", "pullup", "lat", "deadlift"), ["back", "biceps"]),
    (("squat", "leg press", "lunge", "quad", "leg extension"), ["upper_leg", "glutes"]),
    (("rdl", "romanian", "hamstring", "leg curl"), ["upper_leg", "glutes"]),
    (("hip thrust", "glute", "kickback"), ["glutes"]),
    (("calf",), ["lower_leg"]),
    (("shoulder", "overhead press", "ohp", "lateral raise", "delt"), ["shoulders"]),
    (("tricep", "skull", "pushdown"), ["triceps"]),
    (("bicep", "curl"), ["biceps"]),
    (("forearm", "wrist", "grip"), ["forearms"]),
    (("crunch", "plank", "ab", "core"), ["abs"]),
    (("run", "cardio", "bike", "row erg", "elliptical"), ["cardio"]),
]


def _zone_for_rate(rate: int) -> str:
    if rate >= 100:
        return "green"
    if rate >= 80:
        return "orange"
    if rate >= 1:
        return "red"
    return "muted"


def _format_remaining(remaining_min: int) -> str:
    if remaining_min <= 0:
        return "—"
    if remaining_min < 60:
        return f"{remaining_min}m"
    h, m = divmod(remaining_min, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _muscles_for_exercise(name: str) -> list[str]:
    low = (name or "").lower()
    for keys, muscles in EXERCISE_MAP:
        if any(k in low for k in keys):
            return list(muscles)
    return []


def _last_trained_by_muscle(conn) -> dict[str, datetime]:
    rows = conn.execute(
        """
        SELECT date, exercise_name
        FROM jefit_exercises
        ORDER BY date DESC
        LIMIT 2000
        """
    ).fetchall()
    last: dict[str, datetime] = {}
    for row in rows:
        muscles = _muscles_for_exercise(row["exercise_name"])
        if not muscles:
            continue
        date_str = row["date"] or ""
        try:
            when = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(
                hour=18, minute=0, second=0, microsecond=0, tzinfo=IST
            )
        except ValueError:
            continue
        for m in muscles:
            if m not in last or when > last[m]:
                last[m] = when
    return last


def muscle_progress(elapsed_h: float, window_h: float, speed: float = 1.0) -> tuple[int, int]:
    speed = max(0.65, min(1.35, float(speed) or 1.0))
    elapsed_eff = elapsed_h * speed
    rate = int(round(max(0.0, min(100.0, 100.0 * elapsed_eff / window_h))))
    remaining_h = max(0.0, window_h / speed - elapsed_h)
    remaining_min = int(round(remaining_h * 60)) if rate < 100 else 0
    if rate >= 100:
        rate = 100
        remaining_min = 0
    return rate, remaining_min


def build_muscle_recovery(
    conn,
    config_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    tz_name = "Asia/Kolkata"
    speed = 1.0
    if config_path is not None:
        try:
            import json

            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            tz_name = config.get("timezone") or tz_name
        except Exception:
            pass
    tz = ZoneInfo(tz_name)
    clock = now.astimezone(tz) if now is not None else datetime.now(tz)

    last_by = _last_trained_by_muscle(conn)
    muscles_out: list[dict[str, Any]] = []
    for key, name in MUSCLE_GROUPS:
        window_h = float(RECOVERY_HOURS.get(key, 48))
        effective_h = window_h / speed if speed else window_h
        last = last_by.get(key)
        if last is None:
            muscles_out.append(
                {
                    "name": name,
                    "key": key,
                    "rate": 100,
                    "remaining_min": 0,
                    "remaining_label": "—",
                    "zone": "green",
                    "last_trained": None,
                    "last_trained_ms": None,
                    "recovery_hours": window_h,
                    "recovery_hours_effective": round(effective_h, 3),
                    "speed_multiplier": speed,
                }
            )
            continue
        last_local = last.astimezone(tz) if last.tzinfo else last.replace(tzinfo=tz)
        elapsed_h = max(0.0, (clock - last_local).total_seconds() / 3600.0)
        rate, remaining_min = muscle_progress(elapsed_h, window_h, speed)
        muscles_out.append(
            {
                "name": name,
                "key": key,
                "rate": rate,
                "remaining_min": remaining_min,
                "remaining_label": _format_remaining(remaining_min if rate < 100 else 0),
                "zone": _zone_for_rate(rate),
                "last_trained": last_local.isoformat(),
                "last_trained_ms": int(last_local.timestamp() * 1000),
                "recovery_hours": window_h,
                "recovery_hours_effective": round(effective_h, 3),
                "speed_multiplier": speed,
            }
        )

    muscles_out.sort(key=lambda m: (m["rate"], m["name"]))
    return {
        "muscles": muscles_out,
        "generated_at": clock.isoformat(),
        "timezone": tz_name,
        "modifiers": {"speed_multiplier": speed, "caption": f"Repair speed {speed:.2f}×"},
    }
