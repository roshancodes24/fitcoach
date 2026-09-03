"""Per-muscle recovery from Jefit workout history, sped or slowed by Whoop.

Whoop has no per-muscle recovery. Local repair still starts from the last hard
set on that muscle (base window W). Systemic speed S is then modulated by last
night's sleep, today's Whoop recovery, recent strain, and protein journal:

  elapsed_effective = (now - last_trained) * S
  rate              = clamp(100 * elapsed_effective / W, 0, 100)
  remaining         = max(0, W/S - (now - last_trained))

S is clamped to 0.65–1.35. Unknown protein is not punished (factor 1.0).
Never-trained muscles are treated as fully recovered (100%, "—").
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .insights import _nutrition_settings, load_config

IST = ZoneInfo("Asia/Kolkata")

# Display order / keys match Jefit-style muscle recovery names.
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

# Hours until a muscle is considered fully recovered after hard training.
# Large compounds recover slower; smaller isolation groups recover faster.
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

# Systemic repair speed from sleep / Whoop / strain / protein.
SPEED_MIN = 0.65
SPEED_MAX = 1.35

_SKIP_TOKENS = (
    "stretch",
    "mobility",
    "foam roll",
    "warm up",
    "warmup",
    "cool down",
    "cooldown",
)


def _exercise_key(name: str) -> str:
    return " ".join(name.lower().replace("-", " ").replace("'", "").split())


def muscles_from_exercise(name: str) -> list[str]:
    """Map an exercise name to one or more primary muscle group keys."""
    n = _exercise_key(name)
    if not n or any(tok in n for tok in _SKIP_TOKENS):
        return []

    # Cardio / conditioning
    if any(
        k in n
        for k in (
            "walk",
            "treadmill",
            "elliptical",
            "cycling",
            "cycle",
            "bike",
            "row erg",
            "jump rope",
            "running",
            "run ",
            "cardio",
        )
    ):
        # Pure cardio names only — "bike" in other contexts is rare in this DB
        if any(
            k in n
            for k in (
                "walk",
                "treadmill",
                "elliptical",
                "indoor cycling",
                "air bike",
                "running",
                "cardio",
            )
        ) or n in {"walking", "cycling"}:
            return ["cardio"]

    hits: list[str] = []

    def add(*keys: str) -> None:
        for k in keys:
            if k not in hits:
                hits.append(k)

    # Chest
    if any(
        k in n
        for k in (
            "bench",
            "chest press",
            "chest raise",
            "fly",
            "flye",
            "crossover",
            "cross over",
            "push up",
            "pushup",
            "pullover",
        )
    ):
        add("chest")
        if "push up" in n or "pushup" in n or "dip" in n:
            add("triceps", "shoulders")
        elif any(k in n for k in ("bench", "chest press", "decline press")):
            add("triceps", "shoulders")

    # Shoulders
    if any(
        k in n
        for k in (
            "shoulder press",
            "military press",
            "arnold",
            "overhead press",
            "lateral raise",
            "front raise",
            "upright row",
            "rear delt",
            "reverse fly",
            "bent over raise",
            "face pull",
        )
    ):
        add("shoulders")
        if "face pull" in n:
            add("back")

    # Triceps (kickback only when named as a tricep move)
    if any(
        k in n
        for k in (
            "tricep",
            "triceps",
            "pushdown",
            "pressdown",
            "skull",
        )
    ) and "curl" not in n:
        add("triceps")
    elif "kickback" in n and "glute" not in n and "leg" not in n:
        add("triceps")
    if "dip" in n and "shoulder" not in n:
        add("triceps", "chest")

    # Biceps
    if any(k in n for k in ("curl", "bicep", "preacher")) and "leg" not in n:
        add("biceps")
        if any(k in n for k in ("hammer", "reverse curl")):
            add("forearms")

    # Forearms (dedicated — avoid matching "wide grip" / "close grip" adjectives)
    if any(k in n for k in ("wrist", "forearm", "farmer walk", "farmers walk")):
        add("forearms")

    # Back
    if any(
        k in n
        for k in (
            "pull up",
            "pullup",
            "pulldown",
            "lat ",
            "row",
            "t bar",
            "deadlift",
            "shrug",
            "hyperextension",
            "back extension",
        )
    ):
        add("back")
        if "row" in n or "pull up" in n or "pullup" in n or "pulldown" in n:
            add("biceps")
        if "deadlift" in n:
            add("upper_leg", "glutes")
        if "shrug" in n:
            add("shoulders")

    # Close-grip pressing still loads triceps
    if "close grip" in n and any(k in n for k in ("bench", "press", "push")):
        add("triceps", "chest")
    # Abs / core (exclude cardio air bike — handled above)
    if any(
        k in n
        for k in (
            "crunch",
            "ab ",
            "abs",
            "plank",
            "torso",
            "leg raise",
            "ab rollout",
            "draw leg",
        )
    ) or n.startswith("ab "):
        add("abs")

    # Glutes
    if any(k in n for k in ("glute", "hip thrust", "hip bridge")) or (
        "kickback" in n and "tricep" not in n
    ):
        add("glutes")

    # Upper leg (quads / hamstrings)
    if any(
        k in n
        for k in (
            "squat",
            "lunge",
            "leg press",
            "leg extension",
            "leg curl",
            "hamstring",
            "rdl",
            "romanian",
            "step up",
            "split squat",
        )
    ):
        add("upper_leg")
        if any(k in n for k in ("squat", "lunge", "hip thrust", "deadlift", "rdl")):
            add("glutes")

    # Lower leg
    if "calf" in n:
        add("lower_leg")

    return hits


def _parse_trained_at(
    date_str: str,
    start_time: str | None,
    end_time: str | None,
) -> datetime:
    for raw in (end_time, start_time):
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(IST)
        except ValueError:
            continue
    try:
        base = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        base = datetime.now(IST)
    return base.replace(hour=12, minute=0, second=0, microsecond=0, tzinfo=IST)


def _zone_for_rate(rate: int, has_training: bool) -> str:
    if not has_training and rate == 100:
        return "green"
    if rate >= 100:
        return "green"
    if rate >= 80:
        return "orange"
    if rate >= 1:
        return "red"
    return "muted"


def _format_remaining(remaining_min: int | None) -> str:
    if remaining_min is None or remaining_min <= 0:
        return "—"
    hours, minutes = divmod(int(remaining_min), 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _row_val(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _coalesce(*vals: Any) -> Any:
    for val in vals:
        if val is not None:
            return val
    return None


def _fetch_whoop_day(conn: sqlite3.Connection, day_key: str) -> Any:
    try:
        return conn.execute(
            "SELECT * FROM whoop_daily WHERE date = ?", (day_key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _fetch_latest_whoop(conn: sqlite3.Connection) -> Any:
    try:
        return conn.execute(
            "SELECT * FROM whoop_daily ORDER BY date DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _protein_journal_logged(conn: sqlite3.Connection, day_key: str) -> bool | None:
    """Whoop journal is yes/no, not grams. None = unknown (do not punish)."""
    try:
        rows = conn.execute(
            """
            SELECT answered_yes, question FROM whoop_journal
            WHERE date = ? AND lower(question) LIKE '%protein%'
            """,
            (day_key,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    for row in rows:
        question = str(_row_val(row, "question") or "").lower()
        if "consumed" in question:
            raw = _row_val(row, "answered_yes")
            return None if raw is None else bool(raw)
    raw = _row_val(rows[0], "answered_yes")
    return None if raw is None else bool(raw)


def _sleep_factor(
    sleep_performance: float | None,
    sleep_hours: float | None,
    sleep_target_hours: float | None,
) -> float:
    """Sleep is the strongest driver of overnight muscle repair."""
    parts: list[tuple[float, float]] = []
    if sleep_performance is not None:
        perf = max(0.0, min(100.0, float(sleep_performance)))
        if perf >= 85:
            factor = 1.08 + 0.10 * min(1.0, (perf - 85.0) / 15.0)
        elif perf >= 70:
            factor = 0.92 + 0.16 * (perf - 70.0) / 15.0
        else:
            factor = 0.72 + 0.20 * (perf / 70.0)
        parts.append((factor, 0.7))

    target = float(sleep_target_hours) if sleep_target_hours else 7.5
    if sleep_hours is not None and target > 0:
        ratio = max(0.0, float(sleep_hours) / target)
        if ratio >= 1.0:
            hours_f = 1.06 + min(0.06, (ratio - 1.0) * 0.4)
        elif ratio >= 0.85:
            hours_f = 0.94 + 0.12 * (ratio - 0.85) / 0.15
        else:
            hours_f = 0.74 + 0.20 * min(1.0, ratio / 0.85)
        parts.append((hours_f, 0.3 if sleep_performance is not None else 1.0))

    if not parts:
        return 1.0
    return sum(factor * weight for factor, weight in parts) / sum(weight for _, weight in parts)


def _whoop_recovery_factor(
    recovery: float | None,
    green: int = 67,
    yellow: int = 50,
) -> float:
    if recovery is None:
        return 1.0
    rec = max(0.0, min(100.0, float(recovery)))
    if rec >= green:
        span = max(1.0, 100.0 - float(green))
        return 1.04 + 0.08 * min(1.0, (rec - green) / span)
    if rec >= yellow:
        span = max(1.0, float(green - yellow))
        return 0.94 + 0.10 * (rec - yellow) / span
    return 0.82 + 0.12 * (rec / max(1.0, float(yellow)))


def _strain_factor(day_strain: float | None) -> float:
    if day_strain is None:
        return 1.0
    strain = max(0.0, float(day_strain))
    if strain < 8:
        return 1.06
    if strain < 14:
        return 1.0
    if strain < 18:
        return 0.94
    return 0.88


def _protein_factor(logged: bool | None) -> float:
    if logged is True:
        return 1.08
    if logged is False:
        return 0.90
    return 1.0


def combine_speed_multiplier(
    sleep_f: float,
    recovery_f: float,
    strain_f: float,
    protein_f: float,
) -> float:
    weights = (
        (sleep_f, 0.45),
        (recovery_f, 0.25),
        (strain_f, 0.15),
        (protein_f, 0.15),
    )
    log_sum = sum(weight * math.log(max(factor, 0.05)) for factor, weight in weights)
    return max(SPEED_MIN, min(SPEED_MAX, math.exp(log_sum)))


def muscle_progress(
    elapsed_h: float,
    window_h: float,
    speed: float,
) -> tuple[int, int]:
    """Return (rate 0–100, remaining minutes) using effective elapsed = t * S."""
    window = float(window_h) if window_h else 1.0
    speed_clamped = max(SPEED_MIN, min(SPEED_MAX, float(speed) if speed else 1.0))
    elapsed = max(0.0, float(elapsed_h))
    elapsed_eff = elapsed * speed_clamped
    rate = int(round(max(0.0, min(100.0, 100.0 * elapsed_eff / window))))
    remaining_h = max(0.0, window / speed_clamped - elapsed)
    remaining_min = int(round(remaining_h * 60)) if remaining_h > 0 else 0
    if rate >= 100:
        return 100, 0
    return rate, remaining_min


def _speed_caption(speed: float, inputs: dict[str, Any]) -> str:
    if speed >= 1.08:
        tone = "faster repair"
    elif speed <= 0.92:
        tone = "slower repair"
    else:
        tone = "typical pace"
    bits: list[str] = []
    perf = inputs.get("sleep_performance")
    hours = inputs.get("sleep_hours")
    if perf is not None:
        bits.append(f"sleep {int(round(float(perf)))}%")
    elif hours is not None:
        bits.append(f"sleep {float(hours):.1f}h")
    rec = inputs.get("recovery")
    if rec is not None:
        bits.append(f"Whoop recovery {int(round(float(rec)))}%")
    strain = inputs.get("day_strain")
    if strain is not None:
        bits.append(f"strain {float(strain):.1f}")
    logged = inputs.get("protein_logged")
    if logged is True:
        bits.append("protein logged")
    elif logged is False:
        bits.append("protein not logged")
    head = f"Repair speed {speed:.2f}× ({tone})"
    if not bits:
        return f"{head} — Whoop not synced; protein unknown (not counted)"
    return f"{head} — " + " · ".join(bits)


def build_systemic_modifiers(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    clock: datetime,
) -> dict[str, Any]:
    """Systemic S from today + last-night Whoop and protein journal (not grams)."""
    today_key = clock.date().isoformat()
    yesterday_key = (clock.date() - timedelta(days=1)).isoformat()
    today_row = _fetch_whoop_day(conn, today_key)
    yesterday_row = _fetch_whoop_day(conn, yesterday_key)
    latest_row = today_row or _fetch_latest_whoop(conn)

    user = config.get("user") or {}
    thresholds = config.get("recovery_thresholds") or {"green": 67, "yellow": 50}
    sleep_target = float(user.get("sleep_target_hours") or 7.5)
    try:
        protein_target = int(_nutrition_settings(config)["protein_target_g"])
    except Exception:
        protein_target = int(user.get("protein_target_g") or 110)

    sleep_performance = _coalesce(
        _row_val(today_row, "sleep_performance"),
        _row_val(yesterday_row, "sleep_performance"),
        _row_val(latest_row, "sleep_performance"),
    )
    sleep_hours = _coalesce(
        _row_val(today_row, "sleep_hours"),
        _row_val(yesterday_row, "sleep_hours"),
        _row_val(latest_row, "sleep_hours"),
    )
    recovery = _coalesce(
        _row_val(today_row, "recovery"),
        _row_val(yesterday_row, "recovery"),
        _row_val(latest_row, "recovery"),
    )
    # Yesterday's completed strain is more honest than a partial today score.
    day_strain = _coalesce(
        _row_val(yesterday_row, "day_strain"),
        _row_val(today_row, "day_strain"),
        _row_val(latest_row, "day_strain"),
    )
    protein_logged = _coalesce(
        _protein_journal_logged(conn, today_key),
        _protein_journal_logged(conn, yesterday_key),
    )

    sleep_f = _sleep_factor(sleep_performance, sleep_hours, sleep_target)
    recovery_f = _whoop_recovery_factor(
        recovery,
        green=int(thresholds.get("green", 67)),
        yellow=int(thresholds.get("yellow", 50)),
    )
    strain_f = _strain_factor(day_strain)
    protein_f = _protein_factor(protein_logged)
    speed = combine_speed_multiplier(sleep_f, recovery_f, strain_f, protein_f)
    speed = round(speed, 3)

    inputs = {
        "sleep_performance": sleep_performance,
        "sleep_hours": sleep_hours,
        "sleep_target_hours": sleep_target,
        "recovery": recovery,
        "day_strain": day_strain,
        "protein_logged": protein_logged,
        "protein_target_g": protein_target,
        "whoop_date": _row_val(today_row, "date") or _row_val(latest_row, "date"),
    }
    return {
        "speed_multiplier": speed,
        "sleep_factor": round(sleep_f, 3),
        "whoop_recovery_factor": round(recovery_f, 3),
        "strain_factor": round(strain_f, 3),
        "protein_factor": round(protein_f, 3),
        "inputs": inputs,
        "caption": _speed_caption(speed, inputs),
    }


def _last_trained_by_muscle(conn: sqlite3.Connection) -> dict[str, datetime]:
    rows = conn.execute(
        """
        SELECT e.date, e.exercise_name, s.start_time, s.end_time
        FROM jefit_exercises e
        LEFT JOIN jefit_sessions s ON e.session_id = s.session_id
        ORDER BY e.date DESC, s.end_time DESC, s.start_time DESC
        """
    ).fetchall()

    latest: dict[str, datetime] = {}
    for row in rows:
        muscles = muscles_from_exercise(row["exercise_name"] or "")
        if not muscles:
            continue
        trained_at = _parse_trained_at(
            row["date"],
            row["start_time"] if "start_time" in row.keys() else None,
            row["end_time"] if "end_time" in row.keys() else None,
        )
        for key in muscles:
            prev = latest.get(key)
            if prev is None or trained_at > prev:
                latest[key] = trained_at
    return latest


def build_muscle_recovery(
    conn: sqlite3.Connection,
    config_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute per-muscle recovery rates and remaining times."""
    tz_name = "Asia/Kolkata"
    config: dict[str, Any] = {}
    if config_path is not None:
        try:
            config = load_config(config_path)
            tz_name = config.get("timezone") or tz_name
        except Exception:
            pass
    tz = ZoneInfo(tz_name)
    clock = now.astimezone(tz) if now is not None else datetime.now(tz)

    modifiers = build_systemic_modifiers(conn, config, clock)
    speed = float(modifiers["speed_multiplier"] or 1.0)
    last_by_muscle = _last_trained_by_muscle(conn)
    muscles_out: list[dict[str, Any]] = []

    for key, name in MUSCLE_GROUPS:
        window_h = float(RECOVERY_HOURS.get(key, 48))
        effective_h = window_h / speed if speed else window_h
        last = last_by_muscle.get(key)
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
                    "recovered_at": None,
                }
            )
            continue

        last_local = last.astimezone(tz)
        elapsed_h = max(0.0, (clock - last_local).total_seconds() / 3600.0)
        rate, remaining_min = muscle_progress(elapsed_h, window_h, speed)
        zone = _zone_for_rate(rate, has_training=True)
        recovered_at = last_local + timedelta(hours=effective_h)
        muscles_out.append(
            {
                "name": name,
                "key": key,
                "rate": rate,
                "remaining_min": remaining_min,
                "remaining_label": _format_remaining(remaining_min if rate < 100 else 0),
                "zone": zone,
                "last_trained": last_local.isoformat(),
                "last_trained_ms": int(last_local.timestamp() * 1000),
                "recovery_hours": window_h,
                "recovery_hours_effective": round(effective_h, 3),
                "speed_multiplier": speed,
                "recovered_at": recovered_at.isoformat(),
            }
        )

    muscles_out.sort(key=lambda m: (m["rate"], m["name"]))

    return {
        "muscles": muscles_out,
        "generated_at": clock.isoformat(),
        "timezone": tz_name,
        "modifiers": modifiers,
        "model": {
            "source": "jefit_exercises+whoop_daily",
            "note": (
                "Local last-trained from Jefit; repair speed from last-night sleep, "
                "Whoop recovery, strain, and protein journal. Whoop has no per-muscle recovery."
            ),
            "recovery_hours": dict(RECOVERY_HOURS),
            "speed_range": [SPEED_MIN, SPEED_MAX],
        },
    }
