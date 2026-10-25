"""HTTP layer for Garmin's Personal API: key -> JWT exchange, cached Bearer GETs.

Key lookup order: $GARMIN_API_KEY, $GARMIN_PERSONAL_API_KEY, then the raw-key
file Garmin's own Python client uses (~/.config/garmin/connect-personal-api-key).
The key is exchanged for a JWT (about a day long) that is cached, mode 600, in
the data directory. The key itself is never logged or returned by a tool.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.environ.get("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
GARMIN_KEY_FILE = Path.home() / ".config" / "garmin" / "connect-personal-api-key"
REFRESH_WINDOW = 3600

logging.getLogger("httpx").setLevel(logging.WARNING)


class NotExposed(RuntimeError):
    """The Personal API gateway is an allowlist; anything else is a 404."""


def data_dir() -> Path:
    configured = os.environ.get("GARMIN_HEALTH_DATA_DIR", "").strip()
    if not configured or configured.startswith("${"):
        configured = str(Path.home() / ".garmin-health-mcp")
    path = Path(configured).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _api_key() -> str:
    for name in ("GARMIN_API_KEY", "GARMIN_PERSONAL_API_KEY"):
        # An unfilled MCPB/plugin placeholder arrives literally as "${...}".
        if (value := os.environ.get(name, "").strip()) and not value.startswith("${"):
            return value
    if GARMIN_KEY_FILE.exists():
        return GARMIN_KEY_FILE.read_text().strip()
    raise RuntimeError(
        "No Garmin Personal API key. Create one in Garmin Connect (Settings > Personal API) and set "
        f"GARMIN_API_KEY, or save it to {GARMIN_KEY_FILE}."
    )


def _jwt_exp(token: str) -> float:
    payload = token.split(".")[1]
    return float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])


def _token(force: bool = False) -> str:
    cache = data_dir() / "token.json"
    if not force and cache.exists():
        cached = json.loads(cache.read_text())
        if cached["expires_at"] - time.time() > REFRESH_WINDOW:
            return cached["access_token"]
    response = httpx.post(
        BASE_URL + TOKEN_PATH, timeout=30,
        headers={"Accept": "*/*", "Authorization": f"ApiKey {_api_key()}"},
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Garmin token exchange failed: HTTP {response.status_code}. "
            "Check the API key; keys expire a year after creation."
        )
    token = response.text.strip().strip('"')
    cache.touch(mode=0o600, exist_ok=True)
    cache.write_text(json.dumps({"access_token": token, "expires_at": _jwt_exp(token)}))
    return token


def get(path: str, params: dict[str, Any] | None = None) -> Any:
    for attempt in range(2):
        response = httpx.get(
            BASE_URL + path, params=params, timeout=60,
            headers={"Accept": "application/json", "Authorization": f"Bearer {_token(force=attempt > 0)}"},
        )
        if response.status_code == 401 and attempt == 0:
            continue
        if response.status_code == 404:
            raise NotExposed(f"{path}: 404 — not exposed by Garmin's Personal API")
        if response.status_code >= 400:
            raise RuntimeError(f"{path}: HTTP {response.status_code} {response.text[:300]}")
        return response.json() if response.content.strip() else None
    raise RuntimeError(f"{path}: unauthorized even after refreshing the token")
