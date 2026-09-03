"""Dated body measurements for progress comparison."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Actual body metrics (not goals/targets).
MEASUREMENT_KEYS = (
    "weight_kg",
    "height_cm",
    "body_fat_pct",
    "waist_cm",
    "belly_navel_cm",
    "chest_cm",
    "arms_cm",
    "forearms_cm",
    "shoulders_cm",
    "hips_cm",
    "upper_leg_cm",
    "lower_leg_cm",
    "neck_cm",
)

COMPARE_KEYS = (
    "weight_kg",
    "body_fat_pct",
    "waist_cm",
    "belly_navel_cm",
    "chest_cm",
    "arms_cm",
    "hips_cm",
)


def today_iso(tz_name: str = "Asia/Kolkata") -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    return datetime.now(tz).date().isoformat()


def extract_measurements(source: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in MEASUREMENT_KEYS:
        raw = source.get(key)
        if raw is None or raw == "":
            out[key] = None
            continue
        try:
            out[key] = float(raw)
        except (TypeError, ValueError):
            out[key] = None
    return out


def upsert_measurements(
    conn,
    measured_on: str,
    values: dict[str, Any],
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Insert or replace one day's measurements. Returns the stored row."""
    # Validate date
    date.fromisoformat(measured_on)
    cleaned = extract_measurements(values)
    now = datetime.now(timezone.utc).isoformat()
    cols = ", ".join(MEASUREMENT_KEYS)
    placeholders = ", ".join("?" for _ in MEASUREMENT_KEYS)
    updates = ", ".join(f"{k}=excluded.{k}" for k in MEASUREMENT_KEYS)
    conn.execute(
        f"""
        INSERT INTO body_measurements (
            date, {cols}, note, updated_at
        ) VALUES (?, {placeholders}, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            {updates},
            note=COALESCE(excluded.note, body_measurements.note),
            updated_at=excluded.updated_at
        """,
        (
            measured_on,
            *[cleaned[k] for k in MEASUREMENT_KEYS],
            (note.strip() if isinstance(note, str) and note.strip() else None),
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM body_measurements WHERE date = ?", (measured_on,)
    ).fetchone()
    return dict(row) if row else {"date": measured_on, **cleaned}


def list_measurements(conn, *, limit: int = 60) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM body_measurements
        ORDER BY date DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def seed_from_user_if_empty(conn, user: dict[str, Any], *, measured_on: str) -> dict[str, Any] | None:
    """If no history exists, snapshot current config.user metrics as a baseline."""
    count = conn.execute("SELECT COUNT(*) AS n FROM body_measurements").fetchone()["n"]
    if count:
        return None
    values = extract_measurements(user or {})
    if all(v is None for v in values.values()):
        return None
    return upsert_measurements(conn, measured_on, values, note="Baseline from profile")


def with_deltas(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Newest-first list. Each entry gets `delta` vs the next older entry
    for key compare metrics (newer − older).
    """
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        item = dict(entry)
        older = entries[i + 1] if i + 1 < len(entries) else None
        delta: dict[str, float | None] = {}
        if older:
            for key in COMPARE_KEYS:
                cur = entry.get(key)
                prev = older.get(key)
                if cur is None or prev is None:
                    delta[key] = None
                else:
                    try:
                        delta[key] = round(float(cur) - float(prev), 2)
                    except (TypeError, ValueError):
                        delta[key] = None
        item["delta"] = delta
        item["vs_date"] = older["date"] if older else None
        out.append(item)
    return out
