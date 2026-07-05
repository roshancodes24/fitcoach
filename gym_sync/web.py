from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from gym_sync.dashboard_data import build_dashboard_payload, fetch_workout_detail
from gym_sync.db import connect
from gym_sync.jefit_parser import load_jefit_into_db
from gym_sync.whoop_parser import load_whoop_into_db

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "gym.db"
DEFAULT_CONFIG = ROOT / "config.json"
IMPORTS_DIR = ROOT / "imports"
IST = ZoneInfo("Asia/Kolkata")

ALLOWED_JEFIT = {".csv"}
ALLOWED_WHOOP = {".zip", ".csv"}


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

    def get_conn():
        return connect(Path(app.config["DB_PATH"]))

    @app.get("/")
    def index():
        resp = render_template("dashboard.html")
        from flask import make_response
        response = make_response(resp)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/immersive")
    def immersive():
        from flask import make_response
        response = make_response(render_template("immersive.html"))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/api/dashboard")
    def api_dashboard():
        days = request.args.get("days", 14, type=int)
        conn = get_conn()
        payload = build_dashboard_payload(conn, Path(app.config["CONFIG_PATH"]), days=days)
        return jsonify(payload)

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
