"""WHOOP API v2 client: OAuth2, token storage/refresh, paginated fetchers."""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SECRETS_PATH = DATA_DIR / "whoop_secrets.json"
TOKENS_PATH = DATA_DIR / "whoop_tokens.json"
OAUTH_STATE_PATH = DATA_DIR / "whoop_oauth_state.json"

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"

DEFAULT_SCOPES = (
    "read:recovery read:cycles read:sleep read:workout offline"
)
DEFAULT_REDIRECT_URI = (
    "https://roshancodes24.github.io/fitcoach/whoop-callback.html"
)


class WhoopApiError(Exception):
    """Raised when WHOOP OAuth or API calls fail."""


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_data_dir()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_secrets() -> dict[str, str]:
    """Load client_id / client_secret from env or data/whoop_secrets.json."""
    file_secrets = _read_json(SECRETS_PATH)
    client_id = (
        os.environ.get("WHOOP_CLIENT_ID")
        or file_secrets.get("client_id")
        or ""
    ).strip()
    client_secret = (
        os.environ.get("WHOOP_CLIENT_SECRET")
        or file_secrets.get("client_secret")
        or ""
    ).strip()
    return {"client_id": client_id, "client_secret": client_secret}


def save_secrets(client_id: str, client_secret: str) -> Path:
    _write_json(
        SECRETS_PATH,
        {"client_id": client_id.strip(), "client_secret": client_secret.strip()},
    )
    return SECRETS_PATH


def load_tokens() -> dict[str, Any]:
    return _read_json(TOKENS_PATH)


def save_tokens(token_payload: dict[str, Any]) -> Path:
    """Persist tokens; compute expires_at from expires_in when present."""
    data = dict(token_payload)
    expires_in = data.get("expires_in")
    if expires_in is not None:
        try:
            seconds = int(expires_in)
            data["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=seconds)
            ).isoformat()
        except (TypeError, ValueError):
            pass
    data["saved_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(TOKENS_PATH, data)
    return TOKENS_PATH


def clear_tokens() -> None:
    if TOKENS_PATH.exists():
        TOKENS_PATH.unlink()


def has_credentials() -> bool:
    secrets_data = load_secrets()
    return bool(secrets_data.get("client_id") and secrets_data.get("client_secret"))


def is_connected() -> bool:
    tokens = load_tokens()
    return bool(tokens.get("access_token") or tokens.get("refresh_token"))


def token_expires_at() -> datetime | None:
    tokens = load_tokens()
    raw = tokens.get("expires_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def token_needs_refresh(*, skew_seconds: int = 120) -> bool:
    tokens = load_tokens()
    if not tokens.get("access_token"):
        return bool(tokens.get("refresh_token"))
    expires = token_expires_at()
    if expires is None:
        return False
    now = datetime.now(timezone.utc)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return now >= (expires - timedelta(seconds=skew_seconds))


def build_authorization_url(
    *,
    redirect_uri: str,
    scopes: str | None = None,
    state: str | None = None,
) -> tuple[str, str]:
    secrets_data = load_secrets()
    client_id = secrets_data.get("client_id") or ""
    if not client_id:
        raise WhoopApiError(
            "WHOOP client_id missing. Set WHOOP_CLIENT_ID or run whoop-auth with --client-id."
        )
    state_val = state or secrets.token_urlsafe(8)[:8]
    _write_json(OAUTH_STATE_PATH, {"state": state_val})
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes or DEFAULT_SCOPES,
        "state": state_val,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state_val


def expected_oauth_state() -> str | None:
    return _read_json(OAUTH_STATE_PATH).get("state")


def clear_oauth_state() -> None:
    if OAUTH_STATE_PATH.exists():
        OAUTH_STATE_PATH.unlink()


def _form_post(url: str, fields: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WhoopApiError(f"Token request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise WhoopApiError(f"Token request network error: {exc}") from exc


def exchange_code(code: str, *, redirect_uri: str) -> dict[str, Any]:
    secrets_data = load_secrets()
    if not secrets_data.get("client_id") or not secrets_data.get("client_secret"):
        raise WhoopApiError("WHOOP client credentials missing.")
    payload = _form_post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": secrets_data["client_id"],
            "client_secret": secrets_data["client_secret"],
            "redirect_uri": redirect_uri,
        },
    )
    if "access_token" not in payload:
        raise WhoopApiError(f"Unexpected token response: {payload}")
    save_tokens(payload)
    clear_oauth_state()
    return payload


def refresh_access_token() -> dict[str, Any]:
    secrets_data = load_secrets()
    tokens = load_tokens()
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise WhoopApiError("No refresh_token stored. Re-run whoop-auth / Connect Whoop.")
    if not secrets_data.get("client_id") or not secrets_data.get("client_secret"):
        raise WhoopApiError("WHOOP client credentials missing.")
    payload = _form_post(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": secrets_data["client_id"],
            "client_secret": secrets_data["client_secret"],
            "scope": "offline",
        },
    )
    if "access_token" not in payload:
        raise WhoopApiError(f"Unexpected refresh response: {payload}")
    # Whoop rotates refresh tokens — keep prior refresh if response omits it.
    if "refresh_token" not in payload and refresh:
        payload["refresh_token"] = refresh
    save_tokens(payload)
    return payload


def ensure_access_token(*, force_refresh: bool = False) -> str:
    tokens = load_tokens()
    if force_refresh or token_needs_refresh():
        if tokens.get("refresh_token"):
            tokens = refresh_access_token()
        elif not tokens.get("access_token"):
            raise WhoopApiError("Not connected to WHOOP. Run whoop-auth first.")
    access = tokens.get("access_token")
    if not access:
        raise WhoopApiError("No access_token available. Re-authorize WHOOP.")
    return str(access)


def extract_code_from_redirect(url_or_code: str) -> tuple[str, str | None]:
    """Accept a bare code or a full callback URL; return (code, state)."""
    text = url_or_code.strip()
    if "://" not in text and "code=" not in text:
        return text, None
    parsed = urlparse(text)
    qs = parse_qs(parsed.query)
    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    if not code:
        raise WhoopApiError("No authorization code found in URL.")
    return code, state


def _api_get(path: str, params: dict[str, Any], access_token: str) -> dict[str, Any]:
    query = {k: v for k, v in params.items() if v is not None}
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WhoopApiError(f"API GET {path} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise WhoopApiError(f"API network error on {path}: {exc}") from exc


def fetch_collection(
    path: str,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Paginate a WHOOP v2 collection endpoint until exhausted."""
    records: list[dict[str, Any]] = []
    next_token: str | None = None
    access = ensure_access_token()

    while True:
        params: dict[str, Any] = {"limit": min(limit, 25)}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if next_token:
            params["nextToken"] = next_token
        try:
            payload = _api_get(path, params, access)
        except WhoopApiError as exc:
            if "401" in str(exc) and load_tokens().get("refresh_token"):
                access = ensure_access_token(force_refresh=True)
                payload = _api_get(path, params, access)
            else:
                raise
        batch = payload.get("records") or []
        records.extend(batch)
        next_token = payload.get("next_token")
        if not next_token or not batch:
            break
    return records


def fetch_recoveries(*, start: str | None = None, end: str | None = None) -> list[dict]:
    return fetch_collection("/v2/recovery", start=start, end=end)


def fetch_cycles(*, start: str | None = None, end: str | None = None) -> list[dict]:
    return fetch_collection("/v2/cycle", start=start, end=end)


def fetch_sleeps(*, start: str | None = None, end: str | None = None) -> list[dict]:
    return fetch_collection("/v2/activity/sleep", start=start, end=end)


def fetch_workouts(*, start: str | None = None, end: str | None = None) -> list[dict]:
    return fetch_collection("/v2/activity/workout", start=start, end=end)


def connection_status() -> dict[str, Any]:
    secrets_data = load_secrets()
    tokens = load_tokens()
    expires = token_expires_at()
    return {
        "has_credentials": bool(secrets_data.get("client_id") and secrets_data.get("client_secret")),
        "connected": is_connected(),
        "has_access_token": bool(tokens.get("access_token")),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "expires_at": expires.isoformat() if expires else None,
        "needs_refresh": token_needs_refresh() if is_connected() else False,
        "scope": tokens.get("scope"),
        "secrets_path": str(SECRETS_PATH),
        "tokens_path": str(TOKENS_PATH),
    }
