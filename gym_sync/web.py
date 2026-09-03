from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request
from werkzeug.utils import secure_filename

from gym_sync.dashboard_data import (
    VOLUME_RANGES,
    build_dashboard_payload,
    build_sleep_detail,
    build_volume_trends,
    fetch_workout_detail,
)
from gym_sync.muscle_recovery import build_muscle_recovery
from gym_sync.db import connect
from gym_sync.insights import load_config, save_config
from gym_sync.jefit_parser import load_jefit_into_db
from gym_sync.jefit_auto import sync_jefit
from gym_sync.measurements import (
    MEASUREMENT_KEYS,
    list_measurements,
    seed_from_user_if_empty,
    today_iso,
    upsert_measurements,
    with_deltas,
)
from gym_sync.whoop_api import (
    WhoopApiError,
    build_authorization_url,
    exchange_code,
    expected_oauth_state,
    has_credentials,
)
from gym_sync.whoop_auto import sync_whoop, whoop_settings, whoop_status
from gym_sync.whoop_parser import load_whoop_into_db

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "gym.db"
DEFAULT_CONFIG = ROOT / "config.json"
IMPORTS_DIR = ROOT / "imports"
IST = ZoneInfo("Asia/Kolkata")

ALLOWED_JEFIT = {".csv"}
ALLOWED_WHOOP = {".zip", ".csv"}

BODY_STAT_KEYS = {
    "weight_kg",
    "weight_target_kg",
    "height_cm",
    "body_fat_pct",
    "waist_cm",
    "waist_target_cm",
    "belly_navel_cm",
    "chest_cm",
    "arms_cm",
    "forearms_cm",
    "shoulders_cm",
    "hips_cm",
    "upper_leg_cm",
    "lower_leg_cm",
    "neck_cm",
}

PROFILE_STRING_KEYS = {
    "name": 80,
    "goal": 400,
}

ALLOWED_SEX_VALUES = {"male", "female"}


def _parse_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid number: {value!r}") from exc


def _parse_optional_str(value: object, *, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected a string value")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        raise ValueError(f"Value too long (max {max_len} characters)")
    return cleaned


def _parse_optional_sex(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("sex must be a string")
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned in {"m", "man", "masculine"}:
        cleaned = "male"
    elif cleaned in {"f", "woman", "feminine"}:
        cleaned = "female"
    if cleaned not in ALLOWED_SEX_VALUES:
        raise ValueError("sex must be male or female")
    return cleaned


def _parse_optional_age(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            raise ValueError("age must be a positive integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("age must be a whole number of years")
        age = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("age must be a positive integer") from exc
    if age < 1 or age > 120:
        raise ValueError("age must be between 1 and 120")
    return age


def _apply_user_updates(user: dict, payload: dict) -> dict:
    """Apply allowed profile/body-stat fields from payload onto user. Returns updated keys."""
    updated: dict = {}
    for key, max_len in PROFILE_STRING_KEYS.items():
        if key not in payload:
            continue
        parsed = _parse_optional_str(payload[key], max_len=max_len)
        user[key] = parsed
        updated[key] = parsed
    if "sex" in payload or "gender" in payload:
        raw_sex = payload["sex"] if "sex" in payload else payload["gender"]
        parsed_sex = _parse_optional_sex(raw_sex)
        user["sex"] = parsed_sex
        updated["sex"] = parsed_sex
    if "age" in payload:
        parsed_age = _parse_optional_age(payload["age"])
        user["age"] = parsed_age
        updated["age"] = parsed_age
    for key, raw in payload.items():
        if key not in BODY_STAT_KEYS:
            continue
        parsed = _parse_optional_float(raw)
        user[key] = parsed
        updated[key] = parsed
    return updated


def _timestamp() -> str:
    return datetime.now(IST).strftime("%Y%m%d_%H%M%S")


def _save_upload(file, target_dir: Path, allowed_suffixes: set[str]) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    original = secure_filename(file.filename or "upload")
    suffix = Path(original).suffix.lower()
    if suffix not in allowed_suffixes:
        raise ValueError(f"Unsupported file type: {suffix or 'unknown'}")
    dest = target_dir / f"{_timestamp()}_{original}"
    file.save(dest)
    return dest


def create_app(db_path: Path | None = None, config_path: Path | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.config["DB_PATH"] = str(db_path or DEFAULT_DB)
    app.config["CONFIG_PATH"] = str(config_path or DEFAULT_CONFIG)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
    # Pick up template edits without requiring debug=True / process restart.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

    def get_conn():
        return connect(Path(app.config["DB_PATH"]))

    def _nocache_html(template_name: str):
        from flask import make_response

        response = make_response(render_template(template_name))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/")
    def index():
        return _nocache_html("dashboard.html")

    @app.get("/profile")
    def profile():
        return _nocache_html("profile.html")

    @app.get("/immersive")
    def immersive():
        return _nocache_html("immersive.html")

    @app.get("/mannequin")
    def mannequin():
        return _nocache_html("mannequin.html")

    @app.get("/api/dashboard")
    def api_dashboard():
        days = request.args.get("days", 14, type=int)
        conn = get_conn()
        payload = build_dashboard_payload(conn, Path(app.config["CONFIG_PATH"]), days=days)
        return jsonify(payload)

    @app.get("/api/sleep/detail")
    def api_sleep_detail():
        days = request.args.get("days", 14, type=int)
        conn = get_conn()
        return jsonify(build_sleep_detail(conn, Path(app.config["CONFIG_PATH"]), days=days))

    @app.get("/api/muscle-recovery")
    def api_muscle_recovery():
        conn = get_conn()
        return jsonify(build_muscle_recovery(conn, Path(app.config["CONFIG_PATH"])))

    @app.get("/api/trends/volume")
    def api_trends_volume():
        range_key = (request.args.get("range") or "7d").strip().lower()
        if range_key not in VOLUME_RANGES:
            return jsonify({
                "error": "Invalid range",
                "allowed": sorted(VOLUME_RANGES),
            }), 400
        conn = get_conn()
        return jsonify(build_volume_trends(conn, range_key))

    @app.get("/api/user")
    def api_user():
        config = load_config(Path(app.config["CONFIG_PATH"]))
        conn = get_conn()
        tz = config.get("timezone") or "Asia/Kolkata"
        seed_from_user_if_empty(
            conn, config.get("user") or {}, measured_on=today_iso(tz)
        )
        return jsonify({"ok": True, "user": config.get("user", {})})

    @app.get("/api/measurements")
    def api_measurements():
        config = load_config(Path(app.config["CONFIG_PATH"]))
        conn = get_conn()
        tz = config.get("timezone") or "Asia/Kolkata"
        seed_from_user_if_empty(
            conn, config.get("user") or {}, measured_on=today_iso(tz)
        )
        limit = request.args.get("limit", 60, type=int) or 60
        limit = max(1, min(limit, 365))
        entries = with_deltas(list_measurements(conn, limit=limit))
        return jsonify(
            {
                "ok": True,
                "today": today_iso(tz),
                "keys": list(MEASUREMENT_KEYS),
                "entries": entries,
            }
        )

    @app.post("/api/measurements")
    def api_save_measurements():
        path = Path(app.config["CONFIG_PATH"])
        config = load_config(path)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Expected a JSON object"}), 400

        tz = config.get("timezone") or "Asia/Kolkata"
        measured_on = (payload.get("date") or payload.get("measured_on") or today_iso(tz)).strip()
        try:
            from datetime import date as date_cls

            date_cls.fromisoformat(measured_on)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid date (use YYYY-MM-DD)"}), 400

        # Allow posting only measurement fields; merge with current user for missing keys
        # when updating config snapshot is requested.
        user = config.setdefault("user", {})
        stats_payload = {k: payload[k] for k in MEASUREMENT_KEYS if k in payload}
        if not stats_payload and not any(user.get(k) is not None for k in MEASUREMENT_KEYS):
            return jsonify({"ok": False, "error": "No measurement fields provided"}), 400

        # Update current profile snapshot when saving today's (or explicit) measurements.
        if stats_payload:
            try:
                updated = _apply_user_updates(user, stats_payload)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if updated:
                save_config(path, config)

        values = {**{k: user.get(k) for k in MEASUREMENT_KEYS}, **stats_payload}
        conn = get_conn()
        row = upsert_measurements(
            conn,
            measured_on,
            values,
            note=payload.get("note"),
        )
        entries = with_deltas(list_measurements(conn))
        return jsonify(
            {
                "ok": True,
                "measurement": row,
                "user": user,
                "entries": entries,
            }
        )

    @app.post("/api/user/profile")
    def update_profile():
        path = Path(app.config["CONFIG_PATH"])
        config = load_config(path)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Expected a JSON object"}), 400

        user = config.setdefault("user", {})
        try:
            updated = _apply_user_updates(user, payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not updated:
            return jsonify({"ok": False, "error": "No valid profile fields provided"}), 400

        save_config(path, config)

        # Log body measurements by date whenever measurement fields are present.
        measurement = None
        entries = None
        if any(k in updated for k in MEASUREMENT_KEYS):
            tz = config.get("timezone") or "Asia/Kolkata"
            measured_on = (payload.get("date") or payload.get("measured_on") or today_iso(tz)).strip()
            try:
                from datetime import date as date_cls

                date_cls.fromisoformat(measured_on)
            except ValueError:
                return jsonify({"ok": False, "error": "Invalid measurement date (use YYYY-MM-DD)"}), 400
            conn = get_conn()
            measurement = upsert_measurements(
                conn,
                measured_on,
                user,
                note=payload.get("note"),
            )
            entries = with_deltas(list_measurements(conn))

        return jsonify(
            {
                "ok": True,
                "user": user,
                "updated": updated,
                "measurement": measurement,
                "entries": entries,
            }
        )

    @app.post("/api/user/body-stats")
    def update_body_stats():
        path = Path(app.config["CONFIG_PATH"])
        config = load_config(path)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "Expected a JSON object"}), 400

        # Body-stats endpoint only accepts numeric metrics (not name/goal).
        stats_only = {k: v for k, v in payload.items() if k in BODY_STAT_KEYS}
        user = config.setdefault("user", {})
        try:
            updated = _apply_user_updates(user, stats_only)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not updated:
            return jsonify({"ok": False, "error": "No valid body-stat fields provided"}), 400

        save_config(path, config)

        measurement = None
        entries = None
        if any(k in updated for k in MEASUREMENT_KEYS):
            tz = config.get("timezone") or "Asia/Kolkata"
            measured_on = (payload.get("date") or payload.get("measured_on") or today_iso(tz)).strip()
            try:
                from datetime import date as date_cls

                date_cls.fromisoformat(measured_on)
            except ValueError:
                return jsonify({"ok": False, "error": "Invalid measurement date (use YYYY-MM-DD)"}), 400
            conn = get_conn()
            measurement = upsert_measurements(conn, measured_on, user, note=payload.get("note"))
            entries = with_deltas(list_measurements(conn))

        return jsonify(
            {
                "ok": True,
                "user": user,
                "updated": updated,
                "measurement": measurement,
                "entries": entries,
            }
        )

    @app.get("/api/workout/<day>")
    def api_workout(day: str):
        conn = get_conn()
        detail = fetch_workout_detail(conn, day)
        if not detail:
            return jsonify({"error": "No workout found"}), 404
        return jsonify(detail)

    @app.post("/api/import")
    def api_import():
        conn = get_conn()
        total = 0
        files: list[str] = []

        jefit_dir = IMPORTS_DIR / "jefit"
        whoop_dir = IMPORTS_DIR / "whoop"
        jefit_dir.mkdir(parents=True, exist_ok=True)
        whoop_dir.mkdir(parents=True, exist_ok=True)

        for path in sorted(jefit_dir.glob("*.csv")):
            total += load_jefit_into_db(conn, path)
            files.append(f"jefit:{path.name}")

        for path in sorted(whoop_dir.glob("*")):
            if path.is_dir() or path.suffix.lower() not in {".zip", ".csv"}:
                continue
            total += load_whoop_into_db(conn, path)
            files.append(f"whoop:{path.name}")

        return jsonify({"ok": True, "records": total, "files": files})

    @app.post("/api/jefit/sync")
    def api_jefit_sync():
        """Auto-sync Jefit from Downloads CSV and/or public scrape."""
        config = load_config(Path(app.config["CONFIG_PATH"]))
        payload = request.get_json(silent=True) or {}
        mode = (payload.get("mode") or request.args.get("mode") or "").strip().lower()
        force = mode if mode in {"csv", "scrape", "auto"} else None
        conn = get_conn()
        result = sync_jefit(conn, config, force=force)
        status = 200 if result.get("ok") else 400
        return jsonify(result), status

    @app.post("/api/upload/jefit")
    def upload_jefit():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"ok": False, "error": "No file selected"}), 400
        try:
            saved = _save_upload(file, IMPORTS_DIR / "jefit", ALLOWED_JEFIT)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        conn = get_conn()
        records = load_jefit_into_db(conn, saved)
        return jsonify(
            {
                "ok": True,
                "source": "jefit",
                "filename": saved.name,
                "records": records,
            }
        )

    @app.post("/api/upload/whoop")
    def upload_whoop():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"ok": False, "error": "No file selected"}), 400
        try:
            saved = _save_upload(file, IMPORTS_DIR / "whoop", ALLOWED_WHOOP)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        conn = get_conn()
        records = load_whoop_into_db(conn, saved)
        return jsonify(
            {
                "ok": True,
                "source": "whoop",
                "filename": saved.name,
                "records": records,
            }
        )

    @app.get("/api/whoop/status")
    def api_whoop_status():
        config = load_config(Path(app.config["CONFIG_PATH"]))
        conn = get_conn()
        return jsonify({"ok": True, **whoop_status(config, conn)})

    @app.get("/api/whoop/connect")
    def api_whoop_connect():
        config = load_config(Path(app.config["CONFIG_PATH"]))
        settings = whoop_settings(config)
        if not has_credentials():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            "WHOOP credentials missing. Save Client ID/Secret via "
                            "`python sync.py whoop-auth --client-id … --client-secret …` "
                            "or set WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET."
                        ),
                    }
                ),
                400,
            )
        try:
            url, _state = build_authorization_url(redirect_uri=settings["redirect_uri"])
        except WhoopApiError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return redirect(url)

    @app.get("/api/whoop/callback")
    def api_whoop_callback():
        config = load_config(Path(app.config["CONFIG_PATH"]))
        settings = whoop_settings(config)
        error = request.args.get("error")
        if error:
            desc = request.args.get("error_description") or error
            return redirect(f"/profile?whoop=error&msg={quote(desc)}")
        code = request.args.get("code")
        state = request.args.get("state")
        if not code:
            return redirect("/profile?whoop=error&msg=Missing%20authorization%20code")
        expected = expected_oauth_state()
        if expected and state and state != expected:
            return redirect("/profile?whoop=error&msg=OAuth%20state%20mismatch")
        try:
            exchange_code(code, redirect_uri=settings["redirect_uri"])
        except WhoopApiError as exc:
            return redirect(f"/profile?whoop=error&msg={quote(str(exc))}")
        return redirect("/profile?whoop=connected")

    @app.post("/api/whoop/sync")
    def api_whoop_sync():
        config = load_config(Path(app.config["CONFIG_PATH"]))
        payload = request.get_json(silent=True) or {}
        mode = (payload.get("mode") or request.args.get("mode") or "").strip().lower()
        force = mode if mode in {"api", "export", "auto"} else None
        conn = get_conn()
        result = sync_whoop(conn, config, force=force)
        status = 200 if result.get("ok") else 400
        return jsonify(result), status

    @app.get("/api/health")
    def api_health():
        db = Path(app.config["DB_PATH"])
        return jsonify(
            {
                "ok": True,
                "db_exists": db.exists(),
                "db_path": str(db),
            }
        )

    return app


def run_dashboard(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    app = create_app()
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    app.run(host=host, port=port, debug=debug)
