from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .jefit_parser import _infer_day_name
from .insights import (
    _planned_session,
    _recovery_zone,
    _session_matches_planned,
    fetch_exercises_for_date,
    load_config,
)

IST = ZoneInfo("Asia/Kolkata")

_SKIP_EXERCISES = {"walking", "walk"}


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


def _session_exercises(conn: sqlite3.Connection, session_date: str) -> list[dict[str, Any]]:
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

    exercises = _session_exercises(conn, last["date"])
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
        for ex in _session_exercises(conn, today_key):
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

    protein_target = user.get("protein_target_g", 140)
    protein_block = {
        "target_g": protein_target,
        "logged_yesterday": bool(protein_yesterday and protein_yesterday["answered_yes"]),
        "message": (
            f"Hit ~{protein_target} g protein today."
            if not protein_yesterday or not protein_yesterday["answered_yes"]
            else f"Protein logged yesterday — aim for ~{protein_target} g again today."
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
    }
