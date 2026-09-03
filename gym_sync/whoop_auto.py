"""Whoop auto-integration: API sync with zip/CSV export fallback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .whoop_api import (
    WhoopApiError,
    connection_status,
    fetch_cycles,
    fetch_recoveries,
    fetch_sleeps,
    fetch_workouts,
    is_connected,
)
from .whoop_parser import load_whoop_into_db, upsert_whoop_data

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMPORTS = ROOT / "imports" / "whoop"
KJ_TO_KCAL = 4.184


def whoop_settings(config: dict[str, Any]) -> dict[str, Any]:
    integ = (config.get("integrations") or {}).get("whoop") or {}
    tz_name = config.get("timezone") or "Asia/Kolkata"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    imports_dir = Path(integ.get("imports_dir") or DEFAULT_IMPORTS)
    if not imports_dir.is_absolute():
        imports_dir = ROOT / imports_dir
    return {
        "enabled": integ.get("enabled", True),
        "prefer": (integ.get("prefer") or "api").lower(),  # api | export | auto
        "lookback_days": int(integ.get("lookback_days") or 14),
        "redirect_uri": integ.get("redirect_uri")
        or "https://roshancodes24.github.io/fitcoach/whoop-callback.html",
        "imports_dir": imports_dir,
        "timezone": tz,
        "timezone_name": tz_name,
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _ms_to_hours(ms: float | int | None) -> float | None:
    if ms is None:
        return None
    try:
        return round(float(ms) / 3_600_000, 2)
    except (TypeError, ValueError):
        return None


def _ms_to_min(ms: float | int | None) -> float | None:
    if ms is None:
        return None
    try:
        return round(float(ms) / 60_000, 1)
    except (TypeError, ValueError):
        return None


def _asleep_ms_from_stages(stages: dict[str, Any]) -> float | None:
    """
    Whoop 'Hours of Sleep' = light + deep (SWS) + REM.

    Prefer summing scored stages. Fall back to in_bed − awake − no_data when any
    stage is missing (no_data must be excluded or hours inflate vs the app).
    """
    light = stages.get("total_light_sleep_time_milli")
    deep = stages.get("total_slow_wave_sleep_time_milli")
    rem = stages.get("total_rem_sleep_time_milli")
    if light is not None and deep is not None and rem is not None:
        try:
            return float(light) + float(deep) + float(rem)
        except (TypeError, ValueError):
            pass

    in_bed = stages.get("total_in_bed_time_milli")
    if in_bed is None:
        return None
    awake = stages.get("total_awake_time_milli") or 0
    no_data = stages.get("total_no_data_time_milli") or 0
    try:
        return float(in_bed) - float(awake) - float(no_data)
    except (TypeError, ValueError):
        return None


def _kj_to_kcal(kj: float | int | None) -> int | None:
    if kj is None:
        return None
    try:
        return int(round(float(kj) / KJ_TO_KCAL))
    except (TypeError, ValueError):
        return None


def _kj_to_kcal_float(kj: float | int | None) -> float | None:
    if kj is None:
        return None
    try:
        return round(float(kj) / KJ_TO_KCAL, 1)
    except (TypeError, ValueError):
        return None


def _scored(record: dict[str, Any]) -> bool:
    return str(record.get("score_state") or "").upper() == "SCORED"


def _cycle_calendar_date(
    cycle: dict[str, Any],
    sleep: dict[str, Any] | None,
    tz: ZoneInfo,
) -> str | None:
    """
    Calendar day for a physiological cycle in the user's timezone.

    Prefer sleep wake (sleep.end) — matches Whoop export "Wake onset" and works for
    in-progress cycles where cycle.end is still null. Then cycle.end, then start.
    """
    candidates: list[datetime] = []
    if sleep:
        wake = _parse_iso(sleep.get("end"))
        if wake:
            candidates.append(wake)
    cycle_end = _parse_iso(cycle.get("end"))
    if cycle_end:
        candidates.append(cycle_end)
    cycle_start = _parse_iso(cycle.get("start"))
    if cycle_start:
        candidates.append(cycle_start)
    if not candidates:
        return None
    return _to_local(candidates[0], tz).date().isoformat()


def map_api_to_rows(
    *,
    cycles: list[dict],
    recoveries: list[dict],
    sleeps: list[dict],
    workouts: list[dict],
    tz: ZoneInfo,
) -> dict[str, list[dict]]:
    """Join WHOOP API collections into whoop_daily / whoop_workouts row shapes."""
    recovery_by_cycle: dict[Any, dict] = {}
    for rec in recoveries:
        if not _scored(rec):
            continue
        cid = rec.get("cycle_id")
        if cid is not None:
            recovery_by_cycle[cid] = rec

    sleep_by_cycle: dict[Any, dict] = {}
    for sleep in sleeps:
        if not _scored(sleep):
            continue
        if sleep.get("nap"):
            continue
        cid = sleep.get("cycle_id")
        if cid is None:
            continue
        # Prefer the longest non-nap sleep (by asleep time) if multiple exist for a cycle.
        existing = sleep_by_cycle.get(cid)
        if existing is None:
            sleep_by_cycle[cid] = sleep
            continue
        new_ms = _asleep_ms_from_stages(
            ((sleep.get("score") or {}).get("stage_summary") or {})
        ) or 0
        old_ms = _asleep_ms_from_stages(
            ((existing.get("score") or {}).get("stage_summary") or {})
        ) or 0
        if new_ms > old_ms:
            sleep_by_cycle[cid] = sleep

    daily_by_date: dict[str, dict] = {}
    for cycle in cycles:
        if not _scored(cycle):
            continue
        cid = cycle.get("id")
        sleep = sleep_by_cycle.get(cid) or {}
        day = _cycle_calendar_date(cycle, sleep or None, tz)
        if not day:
            continue

        cscore = cycle.get("score") or {}
        recovery = recovery_by_cycle.get(cid) or {}
        rscore = recovery.get("score") or {}
        sscore = sleep.get("score") or {}
        stages = sscore.get("stage_summary") or {}
        needed = sscore.get("sleep_needed") or {}
        asleep_ms = _asleep_ms_from_stages(stages) if stages else None

        row = {
            "date": day,
            "recovery": rscore.get("recovery_score"),
            "hrv": rscore.get("hrv_rmssd_milli"),
            "rhr": rscore.get("resting_heart_rate"),
            "day_strain": cscore.get("strain"),
            "sleep_hours": _ms_to_hours(asleep_ms),
            "sleep_performance": sscore.get("sleep_performance_percentage"),
            "sleep_debt_min": _ms_to_min(needed.get("need_from_sleep_debt_milli")),
            "deep_min": _ms_to_min(stages.get("total_slow_wave_sleep_time_milli")),
            "rem_min": _ms_to_min(stages.get("total_rem_sleep_time_milli")),
            "light_min": _ms_to_min(stages.get("total_light_sleep_time_milli")),
            "calories": _kj_to_kcal(cscore.get("kilojoule")),
        }
        # One row per calendar day — prefer the entry that has recovery.
        existing = daily_by_date.get(day)
        if existing is None or (existing.get("recovery") is None and row.get("recovery") is not None):
            daily_by_date[day] = row

    daily = [daily_by_date[k] for k in sorted(daily_by_date)]

    workout_rows: list[dict] = []
    for workout in workouts:
        if not _scored(workout):
            continue
        start_dt = _parse_iso(workout.get("start"))
        end_dt = _parse_iso(workout.get("end"))
        if not start_dt:
            continue
        local_start = _to_local(start_dt, tz)
        day = local_start.date().isoformat()
        duration_min = None
        if end_dt:
            duration_min = round((end_dt - start_dt).total_seconds() / 60, 1)
        wscore = workout.get("score") or {}
        activity = (
            workout.get("sport_name")
            or (f"sport_{workout.get('sport_id')}" if workout.get("sport_id") is not None else "workout")
        )
        # Store start_time in a stable local string matching export style where possible.
        start_time = local_start.strftime("%Y-%m-%d %H:%M:%S")
        workout_rows.append(
            {
                "date": day,
                "start_time": start_time,
                "activity": str(activity),
                "duration_min": duration_min,
                "strain": wscore.get("strain"),
                "avg_hr": wscore.get("average_heart_rate"),
                "calories": _kj_to_kcal_float(wscore.get("kilojoule")),
            }
        )

    return {"daily": daily, "workouts": workout_rows, "journal": []}


def find_latest_whoop_export(settings: dict[str, Any]) -> Path | None:
    imports_dir: Path = settings["imports_dir"]
    if not imports_dir.exists():
        return None
    candidates: list[Path] = []
    for path in imports_dir.iterdir():
        if path.is_dir():
            continue
        if path.suffix.lower() in {".zip", ".csv"}:
            candidates.append(path)
    # Also allow an extracted whoop_export directory next to zips.
    export_dir = imports_dir / "whoop_export"
    if export_dir.is_dir() and any(export_dir.glob("*.csv")):
        candidates.append(export_dir)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def import_from_api(conn, settings: dict[str, Any]) -> dict[str, Any]:
    if not is_connected():
        return {
            "ok": False,
            "method": "api",
            "error": "Not connected to WHOOP. Open Connect Whoop or run whoop-auth.",
        }
    lookback = int(settings.get("lookback_days") or 14)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = end.isoformat().replace("+00:00", "Z")
    tz: ZoneInfo = settings["timezone"]

    try:
        cycles = fetch_cycles(start=start_iso, end=end_iso)
        recoveries = fetch_recoveries(start=start_iso, end=end_iso)
        sleeps = fetch_sleeps(start=start_iso, end=end_iso)
        workouts = fetch_workouts(start=start_iso, end=end_iso)
    except WhoopApiError as exc:
        return {"ok": False, "method": "api", "error": str(exc)}

    mapped = map_api_to_rows(
        cycles=cycles,
        recoveries=recoveries,
        sleeps=sleeps,
        workouts=workouts,
        tz=tz,
    )
    if not mapped["daily"] and not mapped["workouts"]:
        return {
            "ok": False,
            "method": "api",
            "error": f"No scored WHOOP data in the last {lookback} days.",
            "lookback_days": lookback,
            "fetched": {
                "cycles": len(cycles),
                "recoveries": len(recoveries),
                "sleeps": len(sleeps),
                "workouts": len(workouts),
            },
        }

    records = upsert_whoop_data(
        conn,
        mapped["daily"],
        mapped["workouts"],
        mapped["journal"],
        f"whoop-api:{start.date().isoformat()}..{end.date().isoformat()}",
        log_source="whoop-api",
    )
    return {
        "ok": True,
        "method": "api",
        "records": records,
        "daily": len(mapped["daily"]),
        "workouts": len(mapped["workouts"]),
        "lookback_days": lookback,
        "fetched": {
            "cycles": len(cycles),
            "recoveries": len(recoveries),
            "sleeps": len(sleeps),
            "workouts": len(workouts),
        },
    }


def import_from_export(conn, settings: dict[str, Any]) -> dict[str, Any]:
    path = find_latest_whoop_export(settings)
    if not path:
        return {
            "ok": False,
            "method": "export",
            "error": "No Whoop zip/CSV found in imports/whoop.",
        }
    try:
        records = load_whoop_into_db(conn, path)
    except Exception as exc:
        return {"ok": False, "method": "export", "error": str(exc), "file": str(path)}
    return {
        "ok": True,
        "method": "export",
        "file": str(path),
        "records": records,
    }


def sync_whoop(conn, config: dict[str, Any], *, force: str | None = None) -> dict[str, Any]:
    """
    Sync Whoop into the DB.

    prefer/force:
      - api: live WHOOP API
      - export: newest zip/CSV under imports/whoop
      - auto: API first, then export fallback
    """
    settings = whoop_settings(config)
    if not settings["enabled"]:
        return {"ok": False, "error": "Whoop integration disabled in config."}

    mode = (force or settings["prefer"] or "api").lower()
    results: list[dict[str, Any]] = []

    if mode == "api":
        result = import_from_api(conn, settings)
        results.append(result)
        return {"ok": result.get("ok", False), "mode": mode, "steps": results, **result}

    if mode == "export":
        result = import_from_export(conn, settings)
        results.append(result)
        return {"ok": result.get("ok", False), "mode": mode, "steps": results, **result}

    # auto: API first, export as fallback
    api_result = import_from_api(conn, settings)
    results.append(api_result)
    if api_result.get("ok"):
        return {
            "ok": True,
            "mode": "auto",
            "steps": results,
            "api": api_result,
            **{k: v for k, v in api_result.items() if k != "ok"},
        }

    export_result = import_from_export(conn, settings)
    results.append(export_result)
    ok = bool(export_result.get("ok"))
    return {
        "ok": ok,
        "mode": "auto",
        "steps": results,
        "api": api_result,
        "export": export_result,
        "method": export_result.get("method") if ok else "auto",
        "error": None if ok else (
            f"API: {api_result.get('error')}; export: {export_result.get('error')}"
        ),
        **({k: v for k, v in export_result.items() if k not in {"ok", "error"} and ok}),
    }


def whoop_status(config: dict[str, Any], conn=None) -> dict[str, Any]:
    settings = whoop_settings(config)
    status = connection_status()
    status.update(
        {
            "enabled": settings["enabled"],
            "prefer": settings["prefer"],
            "lookback_days": settings["lookback_days"],
            "redirect_uri": settings["redirect_uri"],
            "timezone": settings["timezone_name"],
        }
    )
    latest_export = find_latest_whoop_export(settings)
    status["latest_export"] = str(latest_export) if latest_export else None
    if conn is not None:
        row = conn.execute(
            """
            SELECT source, file_path, imported_at, records
            FROM import_log
            WHERE source IN ('whoop', 'whoop-api')
            ORDER BY imported_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row:
            status["last_import"] = dict(row)
        else:
            status["last_import"] = None
        daily = conn.execute("SELECT MAX(date) AS latest FROM whoop_daily").fetchone()
        status["latest_whoop_date"] = daily["latest"] if daily else None
    return status
