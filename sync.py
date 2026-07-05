#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gym_sync.db import connect
from gym_sync.insights import build_report, format_report_text
from gym_sync.jefit_parser import load_jefit_into_db
from gym_sync.whoop_parser import load_whoop_into_db
from gym_sync.web import run_dashboard

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "gym.db"
DEFAULT_CONFIG = ROOT / "config.json"
IMPORTS_DIR = ROOT / "imports"
REPORTS_DIR = ROOT / "reports"


def _resolve_import_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.exists():
        return candidate
    alt = IMPORTS_DIR / path
    if alt.exists():
        return alt
    raise FileNotFoundError(f"File not found: {path}")


def cmd_import_jefit(args: argparse.Namespace) -> int:
    path = _resolve_import_path(args.file)
    conn = connect(Path(args.db))
    count = load_jefit_into_db(conn, path)
    print(f"Imported Jefit data from {path.name}: {count} records")
    return 0


def cmd_import_whoop(args: argparse.Namespace) -> int:
    path = _resolve_import_path(args.file)
    conn = connect(Path(args.db))
    count = load_whoop_into_db(conn, path)
    print(f"Imported Whoop data from {path.name}: {count} records")
    return 0


def cmd_import_all(args: argparse.Namespace) -> int:
    conn = connect(Path(args.db))
    total = 0
    jefit_dir = IMPORTS_DIR / "jefit"
    whoop_dir = IMPORTS_DIR / "whoop"
    for path in sorted(jefit_dir.glob("*.csv")):
        total += load_jefit_into_db(conn, path)
        print(f"Jefit: {path.name}")
    for path in sorted(whoop_dir.glob("*")):
        if path.is_dir():
            continue
        if path.suffix.lower() not in {".zip", ".csv"}:
            continue
        total += load_whoop_into_db(conn, path)
        print(f"Whoop: {path.name}")
    print(f"Total imported records: {total}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print("No synced data yet. Run: python sync.py import-all", file=sys.stderr)
        return 1
    conn = connect(db_path)
    report = build_report(conn, Path(args.config), days=args.days)
    text = format_report_text(report, conn=conn)
    print(text)
    if args.save:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = report["today"]["date"]
        txt_path = REPORTS_DIR / f"report_{stamp}.txt"
        json_path = REPORTS_DIR / f"report_{stamp}.json"
        txt_path.write_text(text, encoding="utf-8")
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Saved: {txt_path}")
        print(f"Saved: {json_path}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    run_dashboard(host=args.host, port=args.port, debug=args.debug)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync Jefit + Whoop exports and generate combined training insights."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Config JSON path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_jefit = sub.add_parser("import-jefit", help="Import a Jefit CSV export")
    p_jefit.add_argument("file", help="Path to Jefit export CSV")
    p_jefit.set_defaults(func=cmd_import_jefit)

    p_whoop = sub.add_parser("import-whoop", help="Import a Whoop zip or CSV folder")
    p_whoop.add_argument("file", help="Path to Whoop zip or extracted folder")
    p_whoop.set_defaults(func=cmd_import_whoop)

    p_all = sub.add_parser("import-all", help="Import everything in imports/jefit and imports/whoop")
    p_all.set_defaults(func=cmd_import_all)

    p_report = sub.add_parser("report", help="Generate combined insights report")
    p_report.add_argument("--days", type=int, default=14, help="Days of history to include")
    p_report.add_argument("--save", action="store_true", help="Save report to reports/")
    p_report.set_defaults(func=cmd_report)

    p_dash = sub.add_parser("dashboard", help="Start the web dashboard")
    p_dash.add_argument("--host", default="127.0.0.1", help="Host to bind")
    p_dash.add_argument("--port", type=int, default=5000, help="Port to bind")
    p_dash.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    p_dash.set_defaults(func=cmd_dashboard)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (IMPORTS_DIR / "jefit").mkdir(exist_ok=True)
    (IMPORTS_DIR / "whoop").mkdir(exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
