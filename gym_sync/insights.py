from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {"timezone": "Asia/Kolkata", "schedule": {}, "recovery_thresholds": {"green": 67, "yellow": 50}}
    return json.loads(config_path.read_text(encoding="utf-8"))


def save_config(config_path: Path, config: dict[str, Any]) -> None:
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _recovery_zone(recovery: float | None, thresholds: dict[str, int]) -> str:
    if recovery is None:
        return "unknown"
    if recovery >= thresholds.get("green", 67):
        return "green"
    if recovery >= thresholds.get("yellow", 50):
        return "yellow"
    return "red"


def _nutrition_settings(config: dict[str, Any]) -> dict[str, Any]:
    nutrition = config.get("nutrition", {})
    user = config.get("user", {})
    protein = nutrition.get("protein_target_g")
    if protein is None:
        protein = user.get("protein_target_g", 110)
    carbs = nutrition.get("carb_strategy") or {}
    return {
        "protein_target_g": protein,
        "calorie_target": nutrition.get("calorie_target"),
        "meals_per_day": nutrition.get("meals_per_day", 4),
        "diet_notes": nutrition.get("diet_notes", ""),
        "carb_gym": carbs.get("gym_days", "Higher carbs around training"),
        "carb_off": carbs.get("off_days", "Lighter carbs — keep protein steady"),
        "pre_workout": carbs.get("pre_workout", "Banana + protein powder before gym"),
        "hydration_liters": nutrition.get("hydration_liters", 3.0),
        "creatine_g": nutrition.get("creatine_g", 5),
        "fiber_g": nutrition.get("fiber_g", 30),
        "alcohol": nutrition.get(
            "alcohol", "Minimize; avoid the night before training when possible"
        ),
    }


def nutrition_day_tips(config: dict[str, Any], planned: str, already_done: bool = False) -> list[str]:
    n = _nutrition_settings(config)
    tips: list[str] = [
        f"Protein ~{n['protein_target_g']} g across {n['meals_per_day']} meals today."
    ]
    if planned == "OFF":
        tips.append(n["carb_off"])
        tips.append(f"Alcohol: {n['alcohol']}")
    else:
        tips.append(n["carb_gym"])
        if not already_done:
            tips.append(f"Pre-workout: {n['pre_workout']}")
    tips.append(f"Water ~{n['hydration_liters']:g} L · creatine {n['creatine_g']:g} g · fiber ~{n['fiber_g']} g.")
    return tips


def _weekday_name(d: date) -> str:
    return d.strftime("%A").lower()


def _planned_session(config: dict[str, Any], d: date) -> str:
    return config.get("schedule", {}).get(_weekday_name(d), "OFF")


def _date_range(days: int) -> list[date]:
    today = datetime.now(IST).date()
    start = today - timedelta(days=days - 1)
    return [start + timedelta(days=i) for i in range(days)]


def fetch_merged_days(conn: sqlite3.Connection, days: int = 14) -> list[dict[str, Any]]:
    whoop = {
        row["date"]: dict(row)
        for row in conn.execute("SELECT * FROM whoop_daily").fetchall()
    }
    jefit_rows = conn.execute("SELECT * FROM jefit_sessions ORDER BY start_time").fetchall()
    jefit_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in jefit_rows:
        jefit_by_date.setdefault(row["date"], []).append(dict(row))
    journal_rows = conn.execute(
        "SELECT date, question, answered_yes FROM whoop_journal"
    ).fetchall()
    journal_by_date: dict[str, dict[str, bool]] = {}
    for row in journal_rows:
        journal_by_date.setdefault(row["date"], {})[row["question"]] = bool(row["answered_yes"])

    merged: list[dict[str, Any]] = []
    for d in _date_range(days):
        key = d.isoformat()
        w = whoop.get(key, {})
        sessions = jefit_by_date.get(key, [])
        if len(sessions) == 1:
            j = sessions[0]
            jefit_session = j.get("day_name")
            duration_min = j.get("duration_min")
            workout_min = j.get("workout_min")
            exercise_count = j.get("exercise_count")
            total_volume = j.get("total_volume")
        elif len(sessions) > 1:
            jefit_session = " + ".join(s["day_name"] for s in sessions if s.get("day_name"))
            duration_min = sum(s.get("duration_min") or 0 for s in sessions)
            workout_min = sum(s.get("workout_min") or 0 for s in sessions)
            exercise_count = sum(s.get("exercise_count") or 0 for s in sessions)
            total_volume = sum(s.get("total_volume") or 0 for s in sessions)
            j = sessions[-1]
        else:
            j = {}
            jefit_session = None
            duration_min = workout_min = exercise_count = total_volume = None
        merged.append(
            {
                "date": key,
                "weekday": _weekday_name(d),
                "recovery": w.get("recovery"),
                "hrv": w.get("hrv"),
                "rhr": w.get("rhr"),
                "day_strain": w.get("day_strain"),
                "sleep_hours": w.get("sleep_hours"),
                "sleep_debt_min": w.get("sleep_debt_min"),
                "deep_min": w.get("deep_min"),
                "jefit_session": jefit_session,
                "duration_min": duration_min,
                "workout_min": workout_min,
                "exercise_count": exercise_count,
                "total_volume": total_volume,
                "journal": journal_by_date.get(key, {}),
            }
        )
    return merged


def fetch_exercises_for_date(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT exercise_name, logs, sets_count, top_weight, top_reps, volume
        FROM jefit_exercises
        WHERE date = ?
        ORDER BY id
        """,
        (day,),
    ).fetchall()


def _insight(category: str, level: str, text: str) -> dict[str, str]:
    return {"category": category, "level": level, "text": text}


def _session_matches_planned(actual: str | None, planned: str) -> bool:
    if not actual or planned == "OFF":
        return planned == "OFF" and not actual
    actual_l = " ".join(actual.lower().split())
    planned_l = " ".join(planned.lower().split())

    planned_parts = planned_l.split()
    planned_token = planned_parts[0]
    planned_variant = planned_parts[1] if len(planned_parts) > 1 else None
    actual_parts = actual_l.split()
    actual_variant = next((p for p in actual_parts[1:] if p in {"a", "b"}), None)

    # Legs A vs Legs B (and similar) must match the letter when present
    if planned_variant in {"a", "b"}:
        if planned_token not in actual_l and not any(
            planned_token in p for p in actual_parts
        ):
            return False
        if actual_variant:
            return actual_variant == planned_variant and planned_token in actual_l
        # Generic labels like "legs" from exercise inference — not enough for A/B
        return False

    if planned_l in actual_l or actual_l in planned_l:
        return True
    return planned_token in actual_l


def _trend(values: list[float]) -> str | None:
    if len(values) < 4:
        return None
    mid = len(values) // 2
    first = mean(values[:mid])
    second = mean(values[mid:])
    diff = second - first
    if abs(diff) < 2:
        return "flat"
    return "up" if diff > 0 else "down"


def _fetch_recent_exercise_history(
    conn: sqlite3.Connection, since: str, limit_per_exercise: int = 3
) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT date, exercise_name, top_weight, top_reps, volume, sets_count
        FROM jefit_exercises
        WHERE date >= ?
        ORDER BY date DESC, id DESC
        """,
        (since,),
    ).fetchall()
    history: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = row["exercise_name"]
        if name.lower() in {"walking", "walk"}:
            continue
        bucket = history.setdefault(name, [])
        if len(bucket) < limit_per_exercise:
            bucket.append(dict(row))
    return history


def _build_insights(
    conn: sqlite3.Connection,
    merged: list[dict[str, Any]],
    config: dict[str, Any],
    thresholds: dict[str, int],
    today_key: str,
    post_gym_recovery: list[dict[str, Any]],
    missed: list[dict[str, str]],
) -> list[dict[str, str]]:
    insights: list[dict[str, str]] = []
    nutrition = _nutrition_settings(config)
    protein_target = nutrition["protein_target_g"]
    planned_today = _planned_session(config, date.fromisoformat(today_key))
    gym_days = [row for row in merged if row.get("jefit_session")]
    recoveries = [row["recovery"] for row in merged if row.get("recovery") is not None]
    sleep_hours = [row["sleep_hours"] for row in merged if row.get("sleep_hours") is not None]
    hrv_values = [row["hrv"] for row in merged if row.get("hrv") is not None]
    today_row = next((row for row in merged if row["date"] == today_key), None)

    # --- Today: link yesterday's session to today's recovery ---
    if today_row:
        today_idx = next((i for i, r in enumerate(merged) if r["date"] == today_key), -1)
        yesterday = merged[today_idx - 1] if today_idx > 0 else None
        if yesterday and yesterday.get("jefit_session") and today_row.get("recovery") is not None:
            rec = today_row["recovery"]
            session = yesterday["jefit_session"]
            if rec < thresholds.get("yellow", 50):
                insights.append(
                    _insight(
                        "recovery",
                        "warn",
                        f"After {session} ({yesterday['date']}), recovery is only {rec:.0f}% today. "
                        "Keep today's session light or take an extra rest day.",
                    )
                )
            elif rec >= thresholds.get("green", 67) and yesterday.get("total_volume"):
                insights.append(
                    _insight(
                        "recovery",
                        "good",
                        f"Good bounce-back: {rec:.0f}% recovery after {session}. You're adapting well.",
                    )
                )

    # --- Recovery averages and trends ---
    if recoveries:
        avg_recovery = mean(recoveries)
        trend = _trend(recoveries)
        if avg_recovery < thresholds.get("yellow", 50):
            insights.append(
                _insight(
                    "recovery",
                    "warn",
                    f"14-day avg recovery is {avg_recovery:.0f}% (low). Protect sleep and keep planned rest days fully off.",
                )
            )
        elif avg_recovery < thresholds.get("green", 67):
            insights.append(
                _insight(
                    "recovery",
                    "warn",
                    f"14-day avg recovery is {avg_recovery:.0f}% (yellow zone). Avoid extra cardio or manual labor on gym mornings.",
                )
            )
        else:
            insights.append(
                _insight(
                    "recovery",
                    "good",
                    f"14-day avg recovery is {avg_recovery:.0f}%. Green zone — use double progression on main lifts.",
                )
            )
        if trend == "down":
            insights.append(
                _insight(
                    "recovery",
                    "warn",
                    "Recovery is trending down this fortnight. Cut 1 accessory set per session until it stabilizes.",
                )
            )
        elif trend == "up":
            insights.append(
                _insight("recovery", "good", "Recovery is trending up — good sign your body is handling the restart.")
            )

    # --- HRV trend ---
    if hrv_values and len(hrv_values) >= 5:
        recent_hrv = mean(hrv_values[-5:])
        prior_hrv = mean(hrv_values[:-5]) if len(hrv_values) > 5 else mean(hrv_values[: len(hrv_values) // 2])
        hrv_change = recent_hrv - prior_hrv
        if hrv_change <= -5:
            insights.append(
                _insight(
                    "recovery",
                    "warn",
                    f"HRV dropped ~{abs(hrv_change):.0f} ms recently ({recent_hrv:.0f} ms avg). Signs of accumulated fatigue.",
                )
            )
        elif hrv_change >= 5:
            insights.append(
                _insight(
                    "recovery",
                    "good",
                    f"HRV is rising ({recent_hrv:.0f} ms avg). Autonomic recovery is improving.",
                )
            )

    # --- Sleep ---
    if sleep_hours:
        avg_sleep = mean(sleep_hours)
        if avg_sleep < 6.5:
            insights.append(
                _insight(
                    "sleep",
                    "warn",
                    f"Average sleep is {avg_sleep:.1f} h — critically short. Aim for 7.5–8 h, especially before leg days.",
                )
            )
        elif avg_sleep < 7:
            insights.append(
                _insight(
                    "sleep",
                    "warn",
                    f"Average sleep is {avg_sleep:.1f} h. Add 30–45 min on training nights to support recovery.",
                )
            )

    sleep_debts = [row["sleep_debt_min"] for row in merged if row.get("sleep_debt_min") is not None]
    if sleep_debts and mean(sleep_debts) >= 60:
        insights.append(
            _insight(
                "sleep",
                "warn",
                f"Sleep debt averaging {mean(sleep_debts):.0f} min. One early night this week will help HRV and recovery.",
            )
        )

    # Short sleep on training mornings (Whoop sleep is keyed to wake date)
    short_pre_gym = sum(
        1
        for row in gym_days
        if row.get("sleep_hours") is not None and row["sleep_hours"] < 6.5
    )
    if short_pre_gym >= 2:
        insights.append(
            _insight(
                "sleep",
                "warn",
                f"{short_pre_gym} sessions followed a night under 6.5 h sleep. Prioritize bedtime the night before training.",
            )
        )

    # --- Training on low recovery ---
    low_recovery_gym = [
        row
        for row in gym_days
        if row.get("recovery") is not None and row["recovery"] < thresholds.get("yellow", 50)
    ]
    if low_recovery_gym:
        dates = ", ".join(r["date"] for r in low_recovery_gym[-3:])
        insights.append(
            _insight(
                "training",
                "warn",
                f"You trained on sub-50% recovery ({dates}). On red days, cut volume 50% or swap to mobility.",
            )
        )

    # --- Post-gym recovery impact ---
    if post_gym_recovery:
        impacts = [
            p
            for p in post_gym_recovery
            if p.get("next_day_recovery") is not None and p["next_day_recovery"] < 55
        ]
        if impacts:
            worst = min(impacts, key=lambda p: p["next_day_recovery"] or 100)
            insights.append(
                _insight(
                    "training",
                    "warn",
                    f"{worst['session']} ({worst['gym_date']}) hit next-day recovery hardest "
                    f"({worst['next_day_recovery']:.0f}%). Expect more fatigue after leg/pull days early in a block.",
                )
            )
        lows = [p for p in post_gym_recovery if p.get("next_day_recovery") is not None and p["next_day_recovery"] < 60]
        if len(lows) >= 2:
            insights.append(
                _insight(
                    "training",
                    "warn",
                    "Multiple sessions dropped next-day recovery below 60%. Space hard days with full rest or lighter accessories.",
                )
            )

    # --- Plan adherence ---
    if missed:
        recent = missed[-3:]
        missed_str = "; ".join(f"{m['date']} ({m['planned']})" for m in recent)
        adapt = config.get("adaptation_rules") or {}
        adapt_note = adapt.get(
            "summary",
            "Adapt and suggest the plan ahead — don't blindly force the calendar.",
        )
        insights.append(
            _insight(
                "adherence",
                "info",
                f"Missed planned sessions: {missed_str}. Catch up only if recovery is green — don't stack fatigue. {adapt_note}",
            )
        )

    wrong_day: list[str] = []
    odd_off: list[str] = []
    changed: list[str] = []
    for row in gym_days:
        d = date.fromisoformat(row["date"])
        planned = _planned_session(config, d)
        actual = row.get("jefit_session")
        if not actual:
            continue
        if planned == "OFF":
            odd_off.append(f"{row['date']}: {actual} on OFF")
        elif not _session_matches_planned(actual, planned):
            if "new workout" in str(actual).lower():
                changed.append(f"{row['date']}: {actual} (planned {planned})")
            else:
                wrong_day.append(f"{row['date']}: did {actual}, planned {planned}")
    if wrong_day:
        insights.append(
            _insight(
                "adherence",
                "info",
                "Schedule drift: "
                + "; ".join(wrong_day[-2:])
                + ". Adapt the plan ahead — realign to the weekly block without stacking catch-ups.",
            )
        )
    if odd_off:
        insights.append(
            _insight(
                "adherence",
                "info",
                "Odd training days: "
                + "; ".join(odd_off[-2:])
                + ". Count them as real work; lighten or protect the next hard day.",
            )
        )
    if changed:
        insights.append(
            _insight(
                "adherence",
                "info",
                "Workout changed: "
                + "; ".join(changed[-2:])
                + ". Use the latest logged session as the new target going forward.",
            )
        )

    if gym_days:
        first_gym_date = min(row["date"] for row in gym_days)
        active_span = sum(1 for row in merged if row["date"] >= first_gym_date)
        sessions_per_week = len(gym_days) / max(active_span / 7, 1)
        if sessions_per_week >= 3.5:
            insights.append(
                _insight(
                    "adherence",
                    "good",
                    f"{len(gym_days)} gym sessions in {len(merged)} days — solid consistency for a gym restart.",
                )
            )
        elif sessions_per_week < 2:
            insights.append(
                _insight(
                    "adherence",
                    "warn",
                    f"Only {len(gym_days)} sessions in {len(merged)} days. Aim for 4–5 per week on the PPL block.",
                )
            )

    # --- Volume and session load ---
    recent_gym = [row for row in gym_days if row.get("total_volume")]
    if len(recent_gym) >= 2:
        volumes = [row["total_volume"] for row in recent_gym]
        if max(volumes) - min(volumes) > 2000:
            heaviest = max(recent_gym, key=lambda r: r["total_volume"] or 0)
            insights.append(
                _insight(
                    "training",
                    "info",
                    f"Highest volume session: {heaviest['jefit_session']} ({heaviest['date']}, "
                    f"{heaviest['total_volume']:.0f} kg·reps). Watch recovery after high-volume leg days.",
                )
            )

    long_sessions = [row for row in gym_days if row.get("duration_min") and row["duration_min"] > 70]
    if long_sessions:
        insights.append(
            _insight(
                "training",
                "info",
                f"{len(long_sessions)} session(s) ran 70+ min. Keep most workouts to 60–75 min while rebuilding.",
            )
        )

    # --- Exercise progression (recent block) ---
    since = merged[0]["date"] if merged else today_key
    history = _fetch_recent_exercise_history(conn, since)
    progress_notes: list[str] = []
    for name, entries in history.items():
        if len(entries) < 2:
            continue
        latest, prior = entries[0], entries[1]
        lw, pw = latest.get("top_weight") or 0, prior.get("top_weight") or 0
        lr, pr = latest.get("top_reps") or 0, prior.get("top_reps") or 0
        if lw > pw and lw > 0:
            progress_notes.append(f"{name}: {pw:g}→{lw:g} kg")
        elif lw == pw and lr > pr and lw > 0:
            progress_notes.append(f"{name}: {lw:g} kg × {pr}→{lr} reps")
    if progress_notes:
        insights.append(
            _insight(
                "progression",
                "good",
                "Load progressing: " + "; ".join(progress_notes[:4])
                + ("." if len(progress_notes) <= 4 else f" (+{len(progress_notes) - 4} more)."),
            )
        )
    elif gym_days:
        insights.append(
            _insight(
                "progression",
                "info",
                "No clear weight/rep progression yet — normal for week 1–2 back. Focus on form and logging every set.",
            )
        )

    # --- Data quality: zero-weight compounds ---
    zero_weight = conn.execute(
        """
        SELECT DISTINCT exercise_name FROM jefit_exercises
        WHERE date >= ? AND top_weight = 0
          AND exercise_name NOT LIKE '%Bodyweight%'
          AND exercise_name NOT LIKE '%Push-Up%'
          AND exercise_name NOT LIKE '%Pull-Up%'
          AND exercise_name NOT LIKE '%Walking%'
          AND exercise_name NOT LIKE '%Calf%'
        """,
        (since,),
    ).fetchall()
    if zero_weight:
        names = ", ".join(r["exercise_name"] for r in zero_weight[:3])
        insights.append(
            _insight(
                "progression",
                "info",
                f"Weight not logged for: {names}. Update Jefit logs so progression tracking works.",
            )
        )

    # --- Nutrition: protein journal, carbs, hydration extras ---
    protein_days = sum(1 for row in merged if row.get("journal", {}).get("Consumed protein?"))
    if protein_days < max(3, len(merged) // 2):
        insights.append(
            _insight(
                "nutrition",
                "warn",
                f"Protein journal logged on only {protein_days}/{len(merged)} days. "
                f"Target ~{protein_target} g/day every day — mark Whoop “Consumed protein?”.",
            )
        )
    else:
        insights.append(
            _insight(
                "nutrition",
                "info",
                f"Daily protein target is ~{protein_target} g across {nutrition['meals_per_day']} meals.",
            )
        )

    if planned_today == "OFF":
        insights.append(
            _insight(
                "nutrition",
                "info",
                f"OFF day carbs: {nutrition['carb_off']}. Keep protein steady.",
            )
        )
        insights.append(
            _insight("nutrition", "info", f"Alcohol: {nutrition['alcohol']}")
        )
    else:
        insights.append(
            _insight(
                "nutrition",
                "info",
                f"Gym-day carbs: {nutrition['carb_gym']}. Pre-workout: {nutrition['pre_workout']}.",
            )
        )

    insights.append(
        _insight(
            "nutrition",
            "info",
            f"Hit ~{nutrition['hydration_liters']:g} L water, {nutrition['creatine_g']:g} g creatine, "
            f"and ~{nutrition['fiber_g']} g fiber today.",
        )
    )

    # --- Whoop data freshness ---
    if not recoveries:
        insights.append(
            _insight("recovery", "info", "No Whoop data in this window. Upload a fresh Whoop export for recovery-guided training.")
        )
    elif today_row and today_row.get("recovery") is None:
        insights.append(
            _insight("recovery", "info", "Today's Whoop recovery is missing — upload latest Whoop export.")
        )

    # Sort: warn first, then info, then good; cap at 10 (keep ≥2 nutrition tips)
    order = {"warn": 0, "info": 1, "good": 2}
    insights.sort(key=lambda i: order.get(i["level"], 1))
    seen_text: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in insights:
        if item["text"] not in seen_text:
            seen_text.add(item["text"])
            unique.append(item)

    cap = 10
    nutrition_items = [i for i in unique if i.get("category") == "nutrition"]
    reserved = nutrition_items[:2]
    others = [i for i in unique if i.get("category") != "nutrition"]
    merged_out = others[: cap - len(reserved)] + reserved
    merged_out.sort(key=lambda i: order.get(i["level"], 1))
    return merged_out[:cap]


def _training_recommendation(recovery: float | None, thresholds: dict[str, int], planned: str) -> str:
    zone = _recovery_zone(recovery, thresholds)
    if planned == "OFF":
        return "Rest day in your plan. Easy walk only."
    if zone == "green":
        return f"Full session: {planned}. Compounds 1-2 RIR, accessories 0-2 RIR."
    if zone == "yellow":
        return f"Train {planned}, but cut 1 set per accessory and keep compounds at RIR 3."
    if zone == "red":
        return f"Recovery is low. Do a light {planned} (50% sets) or swap to mobility and walking."
    return f"Whoop data missing. Train {planned} at RIR 3 and stop if performance drops."


def build_report(conn: sqlite3.Connection, config_path: Path, days: int = 14) -> dict[str, Any]:
    config = load_config(config_path)
    thresholds = config.get("recovery_thresholds", {"green": 67, "yellow": 50})
    merged = fetch_merged_days(conn, days=days)
    today = datetime.now(IST).date()
    today_key = today.isoformat()
    today_row = next((row for row in merged if row["date"] == today_key), merged[-1] if merged else None)

    gym_days = [row for row in merged if row.get("jefit_session")]
    recoveries = [row["recovery"] for row in merged if row.get("recovery") is not None]
    sleep_hours = [row["sleep_hours"] for row in merged if row.get("sleep_hours") is not None]

    post_gym_recovery: list[dict[str, Any]] = []
    for i, row in enumerate(merged):
        if not row.get("jefit_session"):
            continue
        if i + 1 < len(merged):
            nxt = merged[i + 1]
            post_gym_recovery.append(
                {
                    "gym_date": row["date"],
                    "session": row["jefit_session"],
                    "next_day_recovery": nxt.get("recovery"),
                }
            )

    missed: list[dict[str, str]] = []
    first_gym_date = min((row["date"] for row in gym_days), default=None)
    for row in merged:
        d = date.fromisoformat(row["date"])
        planned = _planned_session(config, d)
        if planned == "OFF" or row.get("jefit_session"):
            continue
        if first_gym_date and row["date"] < first_gym_date:
            continue
        if row["date"] <= today_key:
            missed.append({"date": row["date"], "planned": planned})

    insights = _build_insights(
        conn, merged, config, thresholds, today_key, post_gym_recovery, missed
    )

    planned_today = _planned_session(config, today)
    today_recovery = today_row.get("recovery") if today_row else None
    recommendation = _training_recommendation(today_recovery, thresholds, planned_today)

    return {
        "generated_at": datetime.now(IST).isoformat(),
        "timezone": config.get("timezone", "Asia/Kolkata"),
        "today": {
            "date": today_key,
            "weekday": _weekday_name(today),
            "planned_session": planned_today,
            "recovery": today_recovery,
            "recovery_zone": _recovery_zone(today_recovery, thresholds),
            "hrv": today_row.get("hrv") if today_row else None,
            "sleep_hours": today_row.get("sleep_hours") if today_row else None,
            "recommendation": recommendation,
        },
        "summary": {
            "days_analyzed": days,
            "gym_sessions": len(gym_days),
            "avg_recovery": round(mean(recoveries), 1) if recoveries else None,
            "avg_sleep_hours": round(mean(sleep_hours), 2) if sleep_hours else None,
            "missed_planned_sessions": missed[-5:],
        },
        "daily_log": merged,
        "post_gym_recovery": post_gym_recovery[-7:],
        "insights": insights,
    }


def format_report_text(report: dict[str, Any], conn: sqlite3.Connection | None = None) -> str:
    lines: list[str] = []
    today = report["today"]
    lines.append("=" * 60)
    lines.append("GYM + WHOOP DAILY INSIGHTS")
    lines.append("=" * 60)
    lines.append(f"Generated: {report['generated_at']} ({report['timezone']})")
    lines.append("")
    lines.append("TODAY")
    lines.append("-" * 60)
    lines.append(f"Date: {today['date']} ({today['weekday'].title()})")
    lines.append(f"Planned: {today['planned_session']}")
    if today["recovery"] is not None:
        lines.append(f"Whoop recovery: {today['recovery']:.0f}% ({today['recovery_zone'].upper()})")
    else:
        lines.append("Whoop recovery: no data (export Whoop again)")
    if today["hrv"] is not None:
        lines.append(f"HRV: {today['hrv']:.0f} ms")
    if today["sleep_hours"] is not None:
        lines.append(f"Sleep: {today['sleep_hours']:.1f} h")
    lines.append(f"Recommendation: {today['recommendation']}")
    lines.append("")

    summary = report["summary"]
    lines.append("RECENT SUMMARY")
    lines.append("-" * 60)
    lines.append(f"Gym sessions ({summary['days_analyzed']}d): {summary['gym_sessions']}")
    if summary["avg_recovery"] is not None:
        lines.append(f"Avg recovery: {summary['avg_recovery']}%")
    if summary["avg_sleep_hours"] is not None:
        lines.append(f"Avg sleep: {summary['avg_sleep_hours']} h")
    if summary["missed_planned_sessions"]:
        lines.append("Recent missed plan days:")
        for item in summary["missed_planned_sessions"]:
            lines.append(f"  - {item['date']}: planned {item['planned']}")
    lines.append("")

    lines.append("DAILY LOG")
    lines.append("-" * 60)
    for row in report["daily_log"]:
        gym = row["jefit_session"] or "-"
        rec = f"{row['recovery']:.0f}%" if row.get("recovery") is not None else "n/a"
        sleep = f"{row['sleep_hours']:.1f}h" if row.get("sleep_hours") is not None else "n/a"
        lines.append(f"{row['date']}  rec={rec}  sleep={sleep}  gym={gym}")

    if report["post_gym_recovery"]:
        lines.append("")
        lines.append("POST-GYM RECOVERY")
        lines.append("-" * 60)
        for item in report["post_gym_recovery"]:
            nxt = item["next_day_recovery"]
            nxt_s = f"{nxt:.0f}%" if nxt is not None else "n/a"
            lines.append(f"{item['gym_date']} {item['session']} -> next day recovery {nxt_s}")

    if report["insights"]:
        lines.append("")
        lines.append("INSIGHTS")
        lines.append("-" * 60)
        for item in report["insights"]:
            text = item["text"] if isinstance(item, dict) else item
            tag = ""
            if isinstance(item, dict):
                tag = f"[{item.get('category', 'general').upper()}] "
            lines.append(f"- {tag}{text}")

    if conn is not None:
        latest_gym = conn.execute(
            "SELECT date, day_name FROM jefit_sessions ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if latest_gym:
            lines.append("")
            lines.append("LAST GYM SESSION")
            lines.append("-" * 60)
            lines.append(f"{latest_gym['date']} - {latest_gym['day_name']}")
            for ex in fetch_exercises_for_date(conn, latest_gym["date"]):
                top = ""
                if ex["top_weight"] is not None and ex["top_reps"] is not None:
                    top = f" (top: {ex['top_weight']} x {ex['top_reps']})"
                lines.append(f"  - {ex['exercise_name']}: {ex['logs']}{top}")

    lines.append("")
    return "\n".join(lines)
