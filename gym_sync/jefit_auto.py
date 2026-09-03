"""Jefit auto-integration: CSV from Downloads/imports, or public scrape when privacy allows."""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import log_import
from .jefit_parser import _exercise_stats, _infer_day_name, load_jefit_into_db
from .jefit_scrape import (
    JefitScrapeError,
    ScrapedWorkout,
    check_public,
    fetch_recent_days,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMPORTS = ROOT / "imports" / "jefit"


def jefit_settings(config: dict[str, Any]) -> dict[str, Any]:
    integ = (config.get("integrations") or {}).get("jefit") or {}
    downloads = integ.get("downloads_dirs") or []
    if not downloads:
        home = Path.home()
        downloads = [str(home / "Downloads"), str(DEFAULT_IMPORTS)]
    resolved_dirs: list[Path] = []
    for p in downloads:
        path = Path(p)
        if not path.is_absolute():
            path = ROOT / path
        resolved_dirs.append(path)
    patterns = integ.get("csv_glob_patterns") or [
        "*ambhore*.csv",
        "*jefit*.csv",
        "roshan*.csv",
    ]
    imports_dir = Path(integ.get("imports_dir") or DEFAULT_IMPORTS)
    if not imports_dir.is_absolute():
        imports_dir = ROOT / imports_dir
    return {
        "enabled": integ.get("enabled", True),
        "user_id": str(integ.get("user_id") or ""),
        "prefer": (integ.get("prefer") or "csv").lower(),  # csv | scrape | auto
        "downloads_dirs": resolved_dirs,
        "csv_glob_patterns": patterns,
        "imports_dir": imports_dir,
        "scrape_lookback_days": int(integ.get("scrape_lookback_days") or 7),
        "copy_csv_to_imports": bool(integ.get("copy_csv_to_imports", True)),
    }


def find_latest_jefit_csv(settings: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    for directory in settings["downloads_dirs"]:
        if not directory.exists():
            continue
        for pattern in settings["csv_glob_patterns"]:
            candidates.extend(directory.glob(pattern))
    # Also scan imports dir always
    imports_dir: Path = settings["imports_dir"]
    if imports_dir.exists():
        candidates.extend(imports_dir.glob("*.csv"))
    files = [p for p in candidates if p.is_file() and p.suffix.lower() == ".csv"]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def import_latest_csv(conn, settings: dict[str, Any]) -> dict[str, Any]:
    path = find_latest_jefit_csv(settings)
    if not path:
        return {
            "ok": False,
            "method": "csv",
            "error": "No Jefit CSV found in Downloads or imports/jefit.",
        }
    dest = path
    imports_dir: Path = settings["imports_dir"]
    if settings["copy_csv_to_imports"] and path.parent.resolve() != imports_dir.resolve():
        imports_dir.mkdir(parents=True, exist_ok=True)
        dest = imports_dir / path.name
        if path.resolve() != dest.resolve():
            shutil.copy2(path, dest)
    count = load_jefit_into_db(conn, dest)
    return {
        "ok": True,
        "method": "csv",
        "path": str(dest),
        "source_path": str(path),
        "records": count,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def _synthetic_session_id(user_id: str, day: str) -> int:
    digest = hashlib.sha1(f"jefit-scrape:{user_id}:{day}".encode()).hexdigest()
    return 9_000_000_000 + (int(digest[:8], 16) % 900_000_000)


def load_scraped_workouts_into_db(
    conn,
    workouts: list[ScrapedWorkout],
    *,
    user_id: str,
    source_label: str = "jefit-scrape",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for w in workouts:
        if not w.date or not w.exercises:
            continue
        session_id = _synthetic_session_id(user_id, w.date)
        duration_min = round((w.session_length_sec or 0) / 60, 1) if w.session_length_sec else None
        workout_min = round((w.actual_workout_sec or 0) / 60, 1) if w.actual_workout_sec else None
        names = [e.name for e in w.exercises]
        day_name = _infer_day_name(names)
        total_volume = w.weight_lifted
        if total_volume is None:
            total_volume = 0.0
            for e in w.exercises:
                stats = _exercise_stats(e.logs_string())
                total_volume += float(stats.get("volume") or 0)

        conn.execute(
            """
            INSERT INTO jefit_sessions (
                session_id, date, start_time, end_time, duration_min, workout_min,
                exercise_count, total_volume, day_name, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                date=excluded.date,
                duration_min=COALESCE(excluded.duration_min, jefit_sessions.duration_min),
                workout_min=COALESCE(excluded.workout_min, jefit_sessions.workout_min),
                exercise_count=excluded.exercise_count,
                total_volume=excluded.total_volume,
                day_name=COALESCE(excluded.day_name, jefit_sessions.day_name),
                updated_at=excluded.updated_at
            """,
            (
                session_id,
                w.date,
                None,
                None,
                duration_min,
                workout_min,
                len(w.exercises),
                total_volume,
                day_name,
                now,
            ),
        )
        conn.execute("DELETE FROM jefit_exercises WHERE session_id = ?", (session_id,))
        for e in w.exercises:
            logs = e.logs_string()
            stats = _exercise_stats(logs)
            conn.execute(
                """
                INSERT INTO jefit_exercises (
                    session_id, date, exercise_name, logs, sets_count, top_weight, top_reps, volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    w.date,
                    e.name,
                    logs,
                    stats.get("sets_count"),
                    stats.get("top_weight"),
                    stats.get("top_reps"),
                    stats.get("volume"),
                ),
            )
            count += 1
        count += 1
    conn.commit()
    log_import(conn, "jefit-scrape", source_label, count)
    return count


def import_from_scrape(conn, settings: dict[str, Any]) -> dict[str, Any]:
    user_id = settings.get("user_id") or ""
    if not user_id:
        return {
            "ok": False,
            "method": "scrape",
            "error": "integrations.jefit.user_id is not set in config.json",
        }
    public, detail = check_public(user_id)
    if not public:
        return {"ok": False, "method": "scrape", "error": detail, "public": False}

    lookback = int(settings.get("scrape_lookback_days") or 7)
    try:
        workouts = fetch_recent_days(user_id, days=lookback)
    except JefitScrapeError as exc:
        return {"ok": False, "method": "scrape", "error": str(exc), "public": True}

    if not workouts:
        return {
            "ok": False,
            "method": "scrape",
            "error": f"No public workouts found in the last {lookback} days.",
            "public": True,
        }

    records = load_scraped_workouts_into_db(conn, workouts, user_id=user_id)
    return {
        "ok": True,
        "method": "scrape",
        "public": True,
        "days": [w.date for w in workouts],
        "workouts": len(workouts),
        "records": records,
        "lookback_days": lookback,
    }


def sync_jefit(conn, config: dict[str, Any], *, force: str | None = None) -> dict[str, Any]:
    """
    Sync Jefit into the DB.

    prefer/force:
      - csv: only auto-import newest CSV
      - scrape: only public scrape
      - auto: try scrape if public, else CSV (or prefer CSV first if prefer=csv)
    """
    settings = jefit_settings(config)
    if not settings["enabled"]:
        return {"ok": False, "error": "Jefit integration disabled in config."}

    mode = (force or settings["prefer"] or "csv").lower()
    results: list[dict[str, Any]] = []

    if mode == "csv":
        result = import_latest_csv(conn, settings)
        results.append(result)
        return {"ok": result.get("ok", False), "mode": mode, "steps": results, **result}

    if mode == "scrape":
        result = import_from_scrape(conn, settings)
        results.append(result)
        return {"ok": result.get("ok", False), "mode": mode, "steps": results, **result}

    # auto: CSV first (richer day_name / session_ids), scrape as supplement if public
    csv_result = import_latest_csv(conn, settings)
    results.append(csv_result)
    scrape_result = import_from_scrape(conn, settings)
    results.append(scrape_result)
    ok = bool(csv_result.get("ok") or scrape_result.get("ok"))
    return {
        "ok": ok,
        "mode": "auto",
        "steps": results,
        "csv": csv_result,
        "scrape": scrape_result,
    }


def default_downloads_hint() -> str:
    return str(Path.home() / "Downloads")
