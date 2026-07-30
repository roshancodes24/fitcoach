from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .jefit_parser import _infer_day_name
from .insights import (
    _nutrition_settings,
    _planned_session,
    _recovery_zone,
    _session_matches_planned,
    fetch_exercises_for_date,
    load_config,
    nutrition_day_tips,
)

IST = ZoneInfo("Asia/Kolkata")

_SKIP_EXERCISES = {"walking", "walk"}

_SESSION_ORDER = ["Legs A", "Push A", "Pull A", "Legs B", "Upper B"]


def _adaptation_settings(config: dict[str, Any]) -> dict[str, Any]:
    rules = config.get("adaptation_rules") or {}
    return {
        "enabled": rules.get("enabled", True),
        "lookback_days": int(rules.get("lookback_days", 14)),
        "plan_ahead_days": int(rules.get("plan_ahead_days", 7)),
        "summary": rules.get(
            "summary",
            "If workouts change or odd days appear, adapt and suggest the plan ahead.",
        ),
        "principles": rules.get("principles") or [],
    }


def build_adapted_plan_ahead(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    today: date,
    recovery_zone: str,
    planned_today: str,
    already_done: bool,
    today_session_name: str | None = None,
) -> dict[str, Any]:
    """Adapt upcoming days when misses, odd sessions, or workout drift appear."""
    settings = _adaptation_settings(config)
    if not settings["enabled"]:
        return {
            "enabled": False,
            "flags": [],
            "message": "Adaptation disabled in config.",
            "days": [],
        }

    lookback = settings["lookback_days"]
    ahead = settings["plan_ahead_days"]
    start = today - timedelta(days=lookback)
    rows = conn.execute(
        """
        SELECT date, day_name FROM jefit_sessions
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC, start_time ASC
        """,
        (start.isoformat(), today.isoformat()),
    ).fetchall()

    by_date: dict[str, str] = {}
    for row in rows:
        # Keep the latest session label for the day if multiple
        by_date[row["date"]] = row["day_name"] or "New Workout"

    flags: list[str] = []
    missed: list[str] = []
    odd_days: list[str] = []
    wrong_days: list[str] = []
    changed_workouts: list[str] = []

    d = start
    while d <= today:
        key = d.isoformat()
        planned = _planned_session(config, d)
        actual = by_date.get(key)
        if planned != "OFF" and not actual and key < today.isoformat():
            missed.append(f"{key} ({planned})")
        if actual:
            if planned == "OFF":
                odd_days.append(f"{key}: trained {actual} on OFF")
            elif not _session_matches_planned(actual, planned):
                if "new workout" in actual.lower():
                    changed_workouts.append(f"{key}: {actual} (planned {planned})")
                else:
                    wrong_days.append(f"{key}: did {actual}, planned {planned}")
        d += timedelta(days=1)

    if today_session_name and planned_today == "OFF":
        odd_days.append(f"{today.isoformat()}: trained {today_session_name} on OFF")
    elif (
        today_session_name
        and planned_today != "OFF"
        and not _session_matches_planned(today_session_name, planned_today)
    ):
        if "new workout" in today_session_name.lower():
            changed_workouts.append(
                f"{today.isoformat()}: {today_session_name} (planned {planned_today})"
            )
        else:
            wrong_days.append(
                f"{today.isoformat()}: did {today_session_name}, planned {planned_today}"
            )

    if missed:
        flags.append("missed_days")
    if odd_days:
        flags.append("odd_days")
    if wrong_days:
        flags.append("schedule_drift")
    if changed_workouts:
        flags.append("workout_changed")

    needs_adapt = bool(flags)
    catch_up: str | None = None
    if missed and recovery_zone == "green" and not already_done:
        # At most one catch-up, and only when today isn't already a hard back-to-back risk
        catch_up = missed[-1].split("(")[-1].rstrip(")")
        if planned_today != "OFF" and catch_up == planned_today:
            catch_up = None

    protect_next = recovery_zone == "red" or bool(odd_days[-1:] and already_done)

    days_out: list[dict[str, Any]] = []
    notes: list[str] = []

    if needs_adapt:
        notes.append(settings["summary"])
    if missed:
        notes.append(
            "Recent misses: "
            + "; ".join(missed[-3:])
            + ". Catch up at most one session if green — don't stack."
        )
    if odd_days:
        notes.append("Odd days: " + "; ".join(odd_days[-2:]) + ".")
    if wrong_days:
        notes.append("Schedule drift: " + "; ".join(wrong_days[-2:]) + ".")
    if changed_workouts:
        notes.append(
            "Workout content shifted: "
            + "; ".join(changed_workouts[-2:])
            + ". Use the latest logged version as the target."
        )

    inserted_catch_up = False
    for offset in range(0 if not already_done else 1, ahead + 1):
        day = today + timedelta(days=offset)
        key = day.isoformat()
        calendar = _planned_session(config, day)
        suggested = calendar
        reason = "Calendar plan"

        if offset == 0 and not already_done:
            if protect_next and calendar != "OFF":
                suggested = "OFF / light mobility"
                reason = "Protect recovery after fatigue or odd session"
            elif catch_up and calendar == "OFF" and not inserted_catch_up:
                suggested = catch_up
                reason = f"Optional single catch-up ({catch_up}) — only if green"
                inserted_catch_up = True
            elif (
                catch_up
                and calendar != "OFF"
                and recovery_zone == "green"
                and not inserted_catch_up
            ):
                # Stay on calendar today; offer catch-up later rather than replacing today
                reason = "Stay on today's calendar session"
            else:
                reason = "Train as planned" if calendar != "OFF" else "Planned rest"
        elif offset > 0:
            if protect_next and offset == 1 and calendar != "OFF":
                suggested = "Light / mobility or keep OFF if needed"
                reason = "Buffer after odd/hard day"
                protect_next = False
            elif (
                catch_up
                and calendar == "OFF"
                and recovery_zone in ("green", "yellow", "unknown")
                and not inserted_catch_up
            ):
                suggested = f"Optional: {catch_up} or rest"
                reason = "Single catch-up window — skip if not green"
                inserted_catch_up = True
            elif needs_adapt and calendar != "OFF":
                reason = "Return to weekly block"
            else:
                reason = "Calendar plan"

        note = config.get("session_notes", {}).get(suggested)
        if not note and suggested == calendar and calendar != "OFF":
            note = config.get("session_notes", {}).get(calendar)

        days_out.append(
            {
                "date": key,
                "weekday": day.strftime("%A"),
                "calendar_session": calendar,
                "suggested_session": suggested,
                "reason": reason,
                "session_note": note,
                "is_today": offset == 0 and not already_done,
            }
        )

    if not notes:
        notes.append("On track — follow the calendar. Plan ahead shown for clarity.")

    return {
        "enabled": True,
        "needs_adaptation": needs_adapt,
        "flags": flags,
        "message": " ".join(notes),
        "catch_up_candidate": catch_up,
        "days": days_out,
        "principles": settings["principles"],
    }


def _format_top_set(weight: float | None, reps: int | None) -> str:
    w = weight or 0
    r = reps or 0
    if w > 0:
        return f"{w:g} kg × {r}"
    if r > 0:
        return f"{r} reps"
    return "—"


def _progression_target(
    weight: float | None,
    reps: int | None,
    zone: str,
) -> str:
    w, r = weight or 0, reps or 0
    if w <= 0 and r <= 0:
        return "Log every set in Jefit"
    if zone == "red":
        return "Skip or very light (RIR 4+)"
    if zone == "yellow":
        return f"Match: {_format_top_set(weight, reps)}"
    if w > 0:
        return f"Beat: {w:g} kg × {r + 1} or {w + 2.5:g} kg × {r}"
    return f"Beat: {r + 1} reps"


_LEG_KEYWORDS = ("leg press", "squat", "lunge", "leg curl", "leg extension", "calf", "rdl", "deadlift", "hip thrust")
_PUSH_KEYWORDS = ("bench", "fly", "pushdown", "tricep", "shoulder press", "lateral raise", "push-up")
_PULL_KEYWORDS = ("pulldown", "row", "pull-up", "face pull", "curl")


def _dominant_session_type(exercises: list[dict[str, Any]]) -> str | None:
    names = " ".join(e["exercise_name"] for e in exercises).lower()
    scores = {
        "legs": sum(1 for k in _LEG_KEYWORDS if k in names),
        "push": sum(1 for k in _PUSH_KEYWORDS if k in names),
        "pull": sum(1 for k in _PULL_KEYWORDS if k in names),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def _session_label_matches(conn: sqlite3.Connection, row: sqlite3.Row, planned: str) -> bool:
    if _session_matches_planned(row["day_name"], planned):
        return True
    exercises = _session_exercises(conn, row["date"])
    labels: list[str | None] = []
    if row["day_name"] and row["day_name"].lower() not in {"workout"}:
        labels.append(row["day_name"])
    labels.append(_infer_day_name([e["exercise_name"] for e in exercises]))
    labels.append(_dominant_session_type(exercises))
    return any(label and _session_matches_planned(label, planned) for label in labels)


def _find_last_matching_session(
    conn: sqlite3.Connection,
    planned: str,
    before_date: str | None = None,
) -> dict[str, Any] | None:
    query = """
        SELECT session_id, date, day_name, duration_min, total_volume, exercise_count
        FROM jefit_sessions
        ORDER BY date DESC, start_time DESC
    """
    for row in conn.execute(query).fetchall():
        if before_date and row["date"] >= before_date:
            continue
        if _session_label_matches(conn, row, planned):
            return dict(row)
    return None


def _find_prior_matching_session(
    conn: sqlite3.Connection,
    planned: str,
    before_date: str,
) -> dict[str, Any] | None:
    """Second-most-recent matching session strictly before before_date."""
    return _find_last_matching_session(conn, planned, before_date=before_date)


def _exercise_key(name: str) -> str:
    return " ".join(name.lower().replace("-", " ").split())


def _classify_pattern(name: str) -> str:
    n = _exercise_key(name)
    if any(k in n for k in ("pull up", "pulldown", "lat ")):
        return "vertical_pull"
    if any(k in n for k in ("row", "t bar", "t-bar")):
        return "horizontal_pull"
    if "face pull" in n or "rear delt" in n or "rear-delt" in n:
        return "rear_delt"
    if any(k in n for k in ("bench", "chest press", "push up", "push-up", "fly", "flye")):
        return "chest"
    if any(k in n for k in ("shoulder press", "arnold", "overhead press")):
        return "shoulder_press"
    if "lateral" in n:
        return "lateral"
    if any(k in n for k in ("tricep", "pushdown", "kickback", "skull")):
        return "triceps"
    if any(k in n for k in ("curl", "preacher", "bicep")):
        return "biceps"
    if any(k in n for k in ("squat", "lunge", "leg press", "goblet")):
        return "squat_pattern"
    if any(k in n for k in ("rdl", "deadlift", "hinge", "leg curl", "hamstring")):
        return "hinge_ham"
    if "extension" in n and "leg" in n:
        return "quad_iso"
    if "calf" in n:
        return "calves"
    if any(k in n for k in ("crunch", "ab ", "core", "plank")):
        return "core"
    return "other"


def _triceps_head_from_exercise(name: str) -> str | None:
    n = _exercise_key(name)
    if not any(k in n for k in ("tricep", "pushdown", "kickback", "dip", "skull", "extension")):
        return None
    # Practical grouping for your coaching language:
    # "upper" ~= long-head dominant overhead work
    # "lower" ~= pushdown/kickback/pressdown style work
    if "overhead" in n:
        return "upper"
    if "kickback" in n or "pushdown" in n or "pressdown" in n:
        return "lower"
    if "dip" in n or "skull" in n:
        return "lower"
    if "tricep extension" in n:
        return "upper"
    return None


def _muscle_parts_from_exercise(name: str) -> list[tuple[str, str]]:
    n = _exercise_key(name)
    parts: list[tuple[str, str]] = []

    # Chest
    if "incline" in n and any(k in n for k in ("bench", "chest press", "fly")):
        parts.append(("chest", "upper"))
    elif "decline" in n and any(k in n for k in ("bench", "chest press", "fly")):
        parts.append(("chest", "lower"))
    elif any(k in n for k in ("bench", "chest press", "fly", "push up", "push-up")):
        parts.append(("chest", "mid"))

    # Shoulders
    if any(k in n for k in ("rear delt", "rear-delt", "face pull")):
        parts.append(("shoulders", "rear"))
    if "lateral" in n:
        parts.append(("shoulders", "side"))
    if any(k in n for k in ("shoulder press", "arnold", "overhead press")):
        parts.append(("shoulders", "front"))

    # Triceps
    tri = _triceps_head_from_exercise(name)
    if tri:
        parts.append(("triceps", tri))

    # Biceps
    if any(k in n for k in ("hammer curl", "reverse curl")):
        parts.append(("biceps", "brachialis"))
    elif any(k in n for k in ("preacher", "spider curl", "concentration")):
        parts.append(("biceps", "short"))
    elif any(k in n for k in ("incline curl", "drag curl")):
        parts.append(("biceps", "long"))
    elif any(k in n for k in ("curl", "bicep")):
        parts.append(("biceps", "general"))

    # Back
    if any(k in n for k in ("pull up", "pull-up", "pulldown", "lat ")):
        parts.append(("back", "width"))
    if any(k in n for k in ("row", "t bar", "t-bar")):
        parts.append(("back", "thickness"))

    # Legs
    if any(k in n for k in ("squat", "leg press", "leg extension", "lunge", "step up")):
        parts.append(("legs", "quads"))
    if any(k in n for k in ("rdl", "deadlift", "leg curl", "hamstring")):
        parts.append(("legs", "hamstrings"))
    if any(k in n for k in ("hip thrust", "glute bridge", "lunge", "squat")):
        parts.append(("legs", "glutes"))
    if "calf" in n:
        parts.append(("legs", "calves"))

    return parts


def _muscle_balance_feedback(exercises: list[dict[str, Any]]) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    expected_parts: dict[str, set[str]] = {
        "chest": {"upper", "mid", "lower"},
        "shoulders": {"front", "side", "rear"},
        "triceps": {"upper", "lower"},
        "biceps": {"long", "short", "brachialis"},
        "back": {"width", "thickness"},
        "legs": {"quads", "hamstrings", "glutes", "calves"},
    }
    covered: dict[str, set[str]] = {}
    for ex in exercises:
        for group, part in _muscle_parts_from_exercise(ex["exercise_name"]):
            covered.setdefault(group, set()).add(part)

    notes: list[str] = []
    next_focus: dict[str, list[str]] = {}
    covered_out: dict[str, list[str]] = {g: sorted(v) for g, v in covered.items()}
    for group, expected in expected_parts.items():
        got = covered.get(group, set())
        if not got:
            continue
        # "general" curls count as at least some biceps coverage
        effective = set(got)
        if group == "biceps" and "general" in effective:
            effective.update({"long", "short"})
        missing = sorted(expected - effective)
        if missing:
            next_focus[group] = missing
            notes.append(
                f"{group.title()} covered: {', '.join(sorted(got))}; next add {', '.join(missing)} emphasis."
            )
        elif group in {"triceps", "shoulders", "back"}:
            notes.append(f"{group.title()} coverage balanced ({', '.join(sorted(got))}).")
    return notes, covered_out, next_focus


def analyze_workout_variations(
    conn: sqlite3.Connection,
    planned: str,
    recovery_zone: str,
    before_date: str | None = None,
) -> dict[str, Any] | None:
    """Compare latest vs prior log of the same session; suggest next template from latest."""
    if planned == "OFF":
        return None

    latest = _find_last_matching_session(conn, planned, before_date=before_date)
    if not latest:
        return {
            "planned": planned,
            "changed": False,
            "message": f"No logged {planned} yet — use config session notes.",
            "added": [],
            "removed": [],
            "kept": [],
            "suggested_exercises": [],
        }

    latest_ex = _session_exercises(conn, latest["date"], latest.get("session_id"))
    prior = _find_prior_matching_session(conn, planned, before_date=latest["date"])
    # Prefer a prior day with a real exercise list (skip stub combo days)
    while prior and len(_session_exercises(conn, prior["date"], prior.get("session_id"))) < 3:
        prior = _find_prior_matching_session(conn, planned, before_date=prior["date"])
    prior_ex = (
        _session_exercises(conn, prior["date"], prior.get("session_id")) if prior else []
    )

    latest_names = {_exercise_key(e["exercise_name"]): e for e in latest_ex}
    prior_names = {_exercise_key(e["exercise_name"]): e for e in prior_ex}

    added = [latest_names[k]["exercise_name"] for k in latest_names if k not in prior_names]
    removed = [prior_names[k]["exercise_name"] for k in prior_names if k not in latest_names]
    kept = [latest_names[k]["exercise_name"] for k in latest_names if k in prior_names]

    # Pattern-level swaps (same pattern, different exercise)
    swaps: list[str] = []
    if prior_ex:
        prior_by_pat: dict[str, list[str]] = {}
        latest_by_pat: dict[str, list[str]] = {}
        for e in prior_ex:
            prior_by_pat.setdefault(_classify_pattern(e["exercise_name"]), []).append(e["exercise_name"])
        for e in latest_ex:
            latest_by_pat.setdefault(_classify_pattern(e["exercise_name"]), []).append(e["exercise_name"])
        for pat, old_list in prior_by_pat.items():
            if pat in ("other",):
                continue
            new_list = latest_by_pat.get(pat) or []
            old_keys = {_exercise_key(x) for x in old_list}
            new_only = [n for n in new_list if _exercise_key(n) not in old_keys]
            old_only = [o for o in old_list if _exercise_key(o) not in {_exercise_key(n) for n in new_list}]
            if new_only and old_only:
                swaps.append(f"{old_only[0]} -> {new_only[0]}")

    suggested: list[dict[str, Any]] = []
    for ex in latest_ex:
        suggested.append(
            {
                "exercise_name": ex["exercise_name"],
                "last_top_set": _format_top_set(ex.get("top_weight"), ex.get("top_reps")),
                "last_logs": ex.get("logs") or "",
                "target_next": _progression_target(
                    ex.get("top_weight"), ex.get("top_reps"), recovery_zone
                ),
                "pattern": _classify_pattern(ex["exercise_name"]),
            }
        )

    changed = bool(added or removed or swaps)
    parts: list[str] = []
    if swaps:
        parts.append("Swaps: " + "; ".join(swaps[:4]))
    if added:
        parts.append("Added: " + ", ".join(added[:5]))
    if removed:
        parts.append("Dropped vs prior: " + ", ".join(removed[:5]))
    if not changed:
        parts.append(f"Same exercise set as prior {planned} — progress loads on these.")
    else:
        parts.append(f"Next {planned}: use the latest log ({latest['date']}) as your working template.")

    balance_notes, covered_parts, next_focus = _muscle_balance_feedback(latest_ex)
    if balance_notes:
        parts.extend(balance_notes[:3])

    return {
        "planned": planned,
        "changed": changed,
        "latest_date": latest["date"],
        "prior_date": prior["date"] if prior else None,
        "added": added,
        "removed": removed,
        "kept": kept,
        "swaps": swaps,
        "muscle_parts_covered": covered_parts,
        "muscle_parts_focus_next": next_focus,
        "muscle_balance_notes": balance_notes,
        "message": " ".join(parts),
        "suggested_exercises": suggested,
    }


def _session_exercises(
    conn: sqlite3.Connection,
    session_date: str,
    session_id: int | None = None,
) -> list[dict[str, Any]]:
    if session_id is not None:
        rows = conn.execute(
            """
            SELECT exercise_name, sets_count, top_weight, top_reps, volume, logs
            FROM jefit_exercises
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()
    else:
        rows = fetch_exercises_for_date(conn, session_date)
    return [
        dict(row)
        for row in rows
        if row["exercise_name"].lower() not in _SKIP_EXERCISES
    ]


def _build_modifications(
    planned: str,
    zone: str,
    rules: dict[str, Any],
    already_done: bool,
) -> list[str]:
    if planned == "OFF":
        return ["Rest day — easy walk, mobility, and hit protein target."]
    if already_done:
        return ["Session logged. Focus on food, hydration, and sleep tonight."]

    rule = rules.get(zone, rules.get("unknown", {}))
    mods: list[str] = []

    summary = rule.get("summary")
    if summary:
        mods.append(summary)

    if zone == "yellow":
        cut = rule.get("cut_accessory_sets", 1)
        mods.append(f"Cut {cut} set from each accessory exercise.")
        rir = rule.get("compounds_rir", "3")
        mods.append(f"Keep main compounds at RIR {rir}.")
    elif zone == "green":
        mods.append(f"Compounds: RIR {rule.get('compounds_rir', '1-2')}.")
        mods.append(f"Accessories: RIR {rule.get('accessories_rir', '0-2')}.")
    elif zone == "red":
        pct = rule.get("volume_pct", 50)
        alt = rule.get("alternative", "mobility and walking")
        mods.append(f"Reduce total sets to ~{pct}% OR swap to {alt}.")
        mods.append("Skip heavy compounds if joints or energy feel off.")
    else:
        mods.append("Whoop data missing — train at RIR 3 and stop if performance drops.")

    return mods


def build_last_session_comparison(
    conn: sqlite3.Connection,
    config_path: Path,
    planned: str,
    recovery_zone: str,
    today_key: str,
    today_already_done: bool = False,
) -> dict[str, Any] | None:
    if planned == "OFF":
        return None

    before = today_key
    last = _find_last_matching_session(conn, planned, before_date=before)
    if not last:
        config = load_config(config_path)
        notes = config.get("session_notes", {}).get(planned)
        return {
            "planned": planned,
            "last_date": None,
            "last_day_name": None,
            "exercises": [],
            "session_note": notes,
            "message": f"No prior {planned} session in your logs yet. Log every set today.",
        }

    exercises = _session_exercises(conn, last["date"], last.get("session_id"))
    comparison: list[dict[str, Any]] = []
    for ex in exercises:
        comparison.append(
            {
                "exercise_name": ex["exercise_name"],
                "last_top_set": _format_top_set(ex.get("top_weight"), ex.get("top_reps")),
                "last_logs": ex.get("logs") or "",
                "target_today": _progression_target(
                    ex.get("top_weight"), ex.get("top_reps"), recovery_zone
                ),
            }
        )

    today_exercises: list[dict[str, Any]] = []
    if today_already_done:
        today_row = conn.execute(
            """
            SELECT session_id FROM jefit_sessions
            WHERE date = ? ORDER BY start_time DESC LIMIT 1
            """,
            (today_key,),
        ).fetchone()
        sid = today_row["session_id"] if today_row else None
        for ex in _session_exercises(conn, today_key, sid):
            today_exercises.append(
                {
                    "exercise_name": ex["exercise_name"],
                    "top_set": _format_top_set(ex.get("top_weight"), ex.get("top_reps")),
                    "logs": ex.get("logs") or "",
                }
            )

    return {
        "planned": planned,
        "last_date": last["date"],
        "last_day_name": last["day_name"],
        "duration_min": last["duration_min"],
        "total_volume": last["total_volume"],
        "exercises": comparison,
        "today_exercises": today_exercises,
        "today_already_done": today_already_done,
        "variations": analyze_workout_variations(
            conn,
            planned,
            recovery_zone,
            before_date=None if today_already_done else today_key,
        ),
    }


def _belly_fat_reminder(
    config: dict[str, Any],
    planned: str,
    whoop: Any,
    already_done: bool,
) -> dict[str, Any]:
    """Build a belly-fat coaching block based on today's context."""
    bf = config.get("belly_fat_rules") or {}
    if not bf.get("enabled"):
        return {"enabled": False}

    user = config.get("user", {})
    steps_target = int(user.get("steps_target", 8000))
    sleep_target = float(user.get("sleep_target_hours", 7.5))
    waist_cm = user.get("waist_cm")
    waist_target = user.get("waist_target_cm", 80)
    weight_kg = user.get("weight_kg")
    weight_target = user.get("weight_target_kg")
    weight_range = user.get("weight_target_range_kg")
    nutrition = config.get("nutrition", {})
    cal_gym = nutrition.get("calorie_target_gym_days")
    cal_rest = nutrition.get("calorie_target_rest_days")
    reminders = bf.get("coaching_reminders", {})

    alerts: list[str] = []
    tips: list[str] = []

    sleep_hours = whoop["sleep_hours"] if whoop else None
    if sleep_hours is not None and sleep_hours < sleep_target:
        shortfall = sleep_target - sleep_hours
        alerts.append(
            f"Sleep was {sleep_hours:.1f} h (target {sleep_target} h, short by {shortfall:.1f} h). "
            + reminders.get("low_sleep", "Cortisol raised — belly fat stalls.")
        )

    is_rest = planned == "OFF" and not already_done
    is_gym = planned != "OFF"

    if is_rest:
        tips.append(reminders.get("rest_day", f"Rest day: walk {steps_target}+ steps; light dinner."))
    elif is_gym:
        tips.append(reminders.get("gym_day", f"Gym day: lift hard; lighter carbs at dinner."))

    tips.append(f"Night carbs: skip heavy rice/roti after 7 PM — protein + veg at dinner.")
    tips.append(f"Steps goal: {steps_target:,} steps today (rest days count most for visceral fat).")

    waist_note = None
    if waist_cm and waist_target:
        diff = waist_cm - waist_target
        if diff > 0:
            waist_note = f"Waist: {waist_cm} cm → target {waist_target} cm ({diff} cm to go)."
        else:
            waist_note = f"Waist at {waist_cm} cm — target reached!"

    weight_note = None
    if weight_kg and weight_target:
        diff_w = round(weight_kg - weight_target, 1)
        rng = f"{weight_range[0]}–{weight_range[1]} kg" if weight_range else f"{weight_target} kg"
        if diff_w > 0:
            weeks_lo = round(diff_w / 0.5)
            weeks_hi = round(diff_w / 0.3)
            weight_note = f"Weight: {weight_kg} kg → target {rng} ({diff_w} kg to lose, ~{weeks_lo}–{weeks_hi} weeks)."
        else:
            weight_note = f"Weight goal reached! ({weight_kg} kg)"

    calorie_note = None
    if is_gym and cal_gym:
        calorie_note = f"Calorie target today (gym): ~{cal_gym} kcal"
    elif is_rest and cal_rest:
        calorie_note = f"Calorie target today (rest): ~{cal_rest} kcal"

    return {
        "enabled": True,
        "alerts": alerts,
        "tips": tips,
        "waist_note": waist_note,
        "weight_note": weight_note,
        "calorie_note": calorie_note,
        "steps_target": steps_target,
        "sleep_target_hours": sleep_target,
        "rules": bf.get("rules", []),
    }


def build_morning_briefing(
    conn: sqlite3.Connection,
    config_path: Path,
) -> dict[str, Any]:
    config = load_config(config_path)
    user = config.get("user", {})
    thresholds = config.get("recovery_thresholds", {"green": 67, "yellow": 50})
    rules = config.get("training_rules", {})

    today = datetime.now(IST).date()
    today_key = today.isoformat()
    planned = _planned_session(config, today)

    whoop = conn.execute("SELECT * FROM whoop_daily WHERE date = ?", (today_key,)).fetchone()
    recovery = whoop["recovery"] if whoop else None
    zone = _recovery_zone(recovery, thresholds)

    today_session = conn.execute(
        """
        SELECT date, day_name, duration_min, exercise_count, total_volume
        FROM jefit_sessions WHERE date = ?
        ORDER BY start_time DESC LIMIT 1
        """,
        (today_key,),
    ).fetchone()
    already_done = today_session is not None

    yesterday_key = (today - timedelta(days=1)).isoformat()
    yesterday_whoop = conn.execute(
        "SELECT recovery, sleep_hours FROM whoop_daily WHERE date = ?", (yesterday_key,)
    ).fetchone()
    yesterday_gym = conn.execute(
        """
        SELECT day_name, duration_min, total_volume FROM jefit_sessions
        WHERE date = ? ORDER BY start_time DESC LIMIT 1
        """,
        (yesterday_key,),
    ).fetchone()

    protein_yesterday = conn.execute(
        """
        SELECT answered_yes FROM whoop_journal
        WHERE date = ? AND question LIKE '%protein%'
        LIMIT 1
        """,
        (yesterday_key,),
    ).fetchone()

    stale_whoop = conn.execute(
        "SELECT MAX(date) AS latest FROM whoop_daily"
    ).fetchone()
    whoop_stale_days = None
    if stale_whoop and stale_whoop["latest"]:
        latest = date.fromisoformat(stale_whoop["latest"])
        whoop_stale_days = (today - latest).days

    modifications = _build_modifications(planned, zone, rules, already_done)

    if planned == "OFF":
        mode = "rest"
        compare_session = today_session["day_name"] if already_done and today_session else None
        if already_done:
            headline = f"Rest day — extra session logged ({today_session['day_name']})"
            action = "You trained on a planned rest day. Prioritize recovery tonight."
        else:
            headline = "Rest day — recovery and nutrition"
            action = "No gym planned. Easy walk, mobility, and hit your protein target."
    elif already_done:
        mode = "post_gym"
        compare_session = today_session["day_name"]
        headline = f"Done for today — {today_session['day_name']}"
        action = (
            f"Session complete ({today_session['duration_min']:.0f} min, "
            f"{today_session['exercise_count']} exercises). Recover well."
        )
    else:
        mode = "pre_gym"
        compare_session = planned
        rec_pct = f"{recovery:.0f}%" if recovery is not None else "n/a"
        headline = f"{planned} · Recovery {rec_pct} ({zone.upper()})"
        action = modifications[0] if modifications else "Train as planned."

    yesterday_block: dict[str, Any] | None = None
    if yesterday_gym:
        msg = f"{yesterday_gym['day_name']} logged ({yesterday_gym['duration_min']:.0f} min)."
        if recovery is not None and recovery < thresholds.get("yellow", 50):
            msg += f" Today's {recovery:.0f}% recovery may reflect that session."
        yesterday_block = {
            "date": yesterday_key,
            "session": yesterday_gym["day_name"],
            "message": msg,
        }

    protein_target = _nutrition_settings(config)["protein_target_g"]
    nutrition_tips = nutrition_day_tips(config, planned, already_done)
    protein_block = {
        "target_g": protein_target,
        "logged_yesterday": bool(protein_yesterday and protein_yesterday["answered_yes"]),
        "message": (
            f"Hit ~{protein_target} g protein today."
            if not protein_yesterday or not protein_yesterday["answered_yes"]
            else f"Protein logged yesterday — aim for ~{protein_target} g again today."
        ),
        "tips": nutrition_tips,
    }
    nutrition_block = {
        **_nutrition_settings(config),
        "tips": nutrition_tips,
        "journal_prompt": (
            "Log Whoop journal: Consumed protein?"
            if not protein_yesterday or not protein_yesterday["answered_yes"]
            else "Protein journal logged yesterday — keep the streak."
        ),
    }

    comparison = None
    next_session: dict[str, str] | None = None
    session_note = config.get("session_notes", {}).get(
        compare_session if compare_session else planned
    )
    if compare_session:
        comparison = build_last_session_comparison(
            conn, config_path, compare_session, zone, today_key, already_done
        )
    elif planned != "OFF":
        comparison = build_last_session_comparison(
            conn, config_path, planned, zone, today_key, already_done
        )
    elif mode == "rest" and not already_done:
        for offset in range(1, 8):
            next_d = today + timedelta(days=offset)
            next_planned = _planned_session(config, next_d)
            if next_planned != "OFF":
                next_session = {
                    "date": next_d.isoformat(),
                    "weekday": next_d.strftime("%A"),
                    "session": next_planned,
                }
                comparison = build_last_session_comparison(
                    conn, config_path, next_planned, zone, today_key, False
                )
                session_note = config.get("session_notes", {}).get(next_planned)
                break

    today_name = today_session["day_name"] if today_session else None
    plan_ahead = build_adapted_plan_ahead(
        conn,
        config,
        today,
        zone,
        planned,
        already_done,
        today_name,
    )

    variation_session = compare_session or planned
    workout_variations = None
    if variation_session and variation_session != "OFF":
        workout_variations = analyze_workout_variations(
            conn, variation_session, zone, before_date=None
        )

    belly_fat_block = _belly_fat_reminder(config, planned, whoop, already_done)

    return {
        "mode": mode,
        "date": today_key,
        "weekday": today.strftime("%A"),
        "planned_session": planned,
        "recovery": recovery,
        "recovery_zone": zone,
        "sleep_hours": whoop["sleep_hours"] if whoop else None,
        "hrv": whoop["hrv"] if whoop else None,
        "sleep_debt_min": whoop["sleep_debt_min"] if whoop else None,
        "headline": headline,
        "action": action,
        "modifications": modifications,
        "session_note": session_note,
        "yesterday": yesterday_block,
        "protein": protein_block,
        "nutrition": nutrition_block,
        "user": {
            "name": user.get("name"),
            "weight_kg": user.get("weight_kg"),
            "goal": user.get("goal"),
            "protein_target_g": protein_target,
        },
        "already_done": already_done,
        "today_session": dict(today_session) if today_session else None,
        "whoop_stale_days": whoop_stale_days,
        "next_session": next_session,
        "last_session_comparison": comparison,
        "plan_ahead": plan_ahead,
        "workout_variations": workout_variations,
        "belly_fat": belly_fat_block,
    }
