#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gym_sync.db import connect
from gym_sync.insights import build_report, format_report_text, load_config
from gym_sync.jefit_parser import load_jefit_into_db
from gym_sync.jefit_auto import sync_jefit
from gym_sync.jefit_scrape import check_public, JefitScrapeError
from gym_sync.whoop_parser import load_whoop_into_db
from gym_sync.whoop_api import (
    WhoopApiError,
    build_authorization_url,
    exchange_code,
    extract_code_from_redirect,
    expected_oauth_state,
    save_secrets,
)
from gym_sync.whoop_auto import sync_whoop, whoop_settings, whoop_status
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


def cmd_jefit_sync(args: argparse.Namespace) -> int:
    """Auto-import newest Jefit CSV and/or scrape public logs."""
    config = load_config(Path(args.config))
    conn = connect(Path(args.db))
    force = None
    if args.csv_only:
        force = "csv"
    elif args.scrape_only:
        force = "scrape"
    elif args.auto:
        force = "auto"
    result = sync_jefit(conn, config, force=force)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_jefit_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    integ = (config.get("integrations") or {}).get("jefit") or {}
    user_id = str(integ.get("user_id") or "")
    print(f"user_id: {user_id or '(not set)'}")
    print(f"prefer: {integ.get('prefer', 'csv')}")
    if not user_id:
        return 1
    try:
        public, detail = check_public(user_id)
        print(f"public: {public}")
        print(f"detail: {detail}")
        return 0 if public else 2
    except JefitScrapeError as exc:
        print(f"error: {exc}")
        return 1


def cmd_whoop_auth(args: argparse.Namespace) -> int:
    """Save credentials and complete OAuth (URL + optional code exchange)."""
    config = load_config(Path(args.config))
    settings = whoop_settings(config)
    redirect_uri = args.redirect_uri or settings["redirect_uri"]

    if args.client_id and args.client_secret:
        path = save_secrets(args.client_id, args.client_secret)
        print(f"Saved credentials to {path}")
    elif args.client_id or args.client_secret:
        print("Provide both --client-id and --client-secret together.", file=sys.stderr)
        return 1

    if args.code:
        try:
            code, state = extract_code_from_redirect(args.code)
            expected = expected_oauth_state()
            if expected and state and state != expected:
                print(f"Warning: OAuth state mismatch (got {state}, expected {expected})")
            exchange_code(code, redirect_uri=redirect_uri)
            print("WHOOP connected. Tokens saved to data/whoop_tokens.json")
            return 0
        except WhoopApiError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        url, state = build_authorization_url(redirect_uri=redirect_uri)
    except WhoopApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"redirect_uri: {redirect_uri}")
    print(f"state: {state}")
    print("Open this URL in your browser, authorize, then either:")
    print("  1) Let the dashboard callback capture the code (if server is running), or")
    print("  2) Re-run with --code <code-or-full-callback-url>")
    print()
    print(url)
    if args.open:
        import webbrowser

        webbrowser.open(url)
    return 0


def cmd_whoop_sync(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    conn = connect(Path(args.db))
    force = None
    if args.api_only:
        force = "api"
    elif args.export_only:
        force = "export"
    elif args.auto:
        force = "auto"
    result = sync_whoop(conn, config, force=force)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_whoop_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    conn = connect(Path(args.db))
    status = whoop_status(config, conn)
    print(json.dumps(status, indent=2))
    if not status.get("has_credentials"):
        return 1
    if not status.get("connected"):
        return 2
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

    p_jsync = sub.add_parser(
        "jefit-sync",
        help="Auto-sync Jefit (newest CSV from Downloads/imports, optional public scrape)",
    )
    g = p_jsync.add_mutually_exclusive_group()
    g.add_argument("--csv-only", action="store_true", help="Only import newest matching CSV")
    g.add_argument("--scrape-only", action="store_true", help="Only scrape public Jefit logs")
    g.add_argument("--auto", action="store_true", help="Try CSV then scrape")
    p_jsync.set_defaults(func=cmd_jefit_sync)

    p_jstatus = sub.add_parser("jefit-status", help="Check Jefit user_id and public scrape availability")
    p_jstatus.set_defaults(func=cmd_jefit_status)

    p_wauth = sub.add_parser(
        "whoop-auth",
        help="Save WHOOP client credentials and start/complete OAuth",
    )
    p_wauth.add_argument("--client-id", help="WHOOP Developer Dashboard Client ID")
    p_wauth.add_argument("--client-secret", help="WHOOP Developer Dashboard Client Secret")
    p_wauth.add_argument(
        "--redirect-uri",
        help="OAuth redirect URI (must match Developer Dashboard)",
    )
    p_wauth.add_argument(
        "--code",
        help="Authorization code or full callback URL after consent",
    )
    p_wauth.add_argument(
        "--open",
        action="store_true",
        help="Open the authorize URL in the default browser",
    )
    p_wauth.set_defaults(func=cmd_whoop_auth)

    p_wsync = sub.add_parser(
        "whoop-sync",
        help="Sync Whoop via API (or export fallback)",
    )
    wg = p_wsync.add_mutually_exclusive_group()
    wg.add_argument("--api-only", action="store_true", help="Only use WHOOP API")
    wg.add_argument("--export-only", action="store_true", help="Only import newest zip/CSV")
    wg.add_argument("--auto", action="store_true", help="API first, then export fallback")
    p_wsync.set_defaults(func=cmd_whoop_sync)

    p_wstatus = sub.add_parser("whoop-status", help="Check WHOOP credentials, tokens, and last import")
    p_wstatus.set_defaults(func=cmd_whoop_status)

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
