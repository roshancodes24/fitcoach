from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    file_path TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    records INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS whoop_daily (
    date TEXT PRIMARY KEY,
    recovery REAL,
    hrv REAL,
    rhr REAL,
    day_strain REAL,
    sleep_hours REAL,
    sleep_performance REAL,
    sleep_debt_min REAL,
    deep_min REAL,
    rem_min REAL,
    light_min REAL,
    calories INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS whoop_workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    start_time TEXT,
    activity TEXT,
    duration_min REAL,
    strain REAL,
    avg_hr REAL,
    calories REAL,
    UNIQUE(date, start_time, activity)
);

CREATE TABLE IF NOT EXISTS whoop_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    question TEXT NOT NULL,
    answered_yes INTEGER NOT NULL,
    UNIQUE(date, question)
);

CREATE TABLE IF NOT EXISTS jefit_sessions (
    session_id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    duration_min REAL,
    workout_min REAL,
    exercise_count INTEGER,
    total_volume REAL,
    day_name TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jefit_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    exercise_name TEXT NOT NULL,
    logs TEXT,
    sets_count INTEGER,
    top_weight REAL,
    top_reps INTEGER,
    volume REAL,
    FOREIGN KEY (session_id) REFERENCES jefit_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_jefit_exercises_date ON jefit_exercises(date);
CREATE INDEX IF NOT EXISTS idx_whoop_workouts_date ON whoop_workouts(date);

CREATE TABLE IF NOT EXISTS body_measurements (
    date TEXT PRIMARY KEY,
    weight_kg REAL,
    height_cm REAL,
    body_fat_pct REAL,
    waist_cm REAL,
    belly_navel_cm REAL,
    chest_cm REAL,
    arms_cm REAL,
    forearms_cm REAL,
    shoulders_cm REAL,
    hips_cm REAL,
    upper_leg_cm REAL,
    lower_leg_cm REAL,
    neck_cm REAL,
    note TEXT,
    updated_at TEXT NOT NULL
);
"""


def _ensure_whoop_daily_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after initial schema (SQLite has no IF NOT EXISTS for ADD)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(whoop_daily)")}
    if "light_min" not in cols:
        conn.execute("ALTER TABLE whoop_daily ADD COLUMN light_min REAL")
        conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_whoop_daily_columns(conn)
    return conn


def log_import(conn: sqlite3.Connection, source: str, file_path: str, records: int) -> None:
    from datetime import datetime, timezone

    conn.execute(
        "INSERT INTO import_log (source, file_path, imported_at, records) VALUES (?, ?, ?, ?)",
        (source, file_path, datetime.now(timezone.utc).isoformat(), records),
    )
    conn.commit()
