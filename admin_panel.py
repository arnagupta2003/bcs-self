#!/usr/bin/env python3
"""Secure web control panel and process supervisor for the server."""

from __future__ import annotations

import copy
import dataclasses
import hmac
import json
import os
import secrets
import shutil
import signal
import sys
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [
    str(ROOT / "dist" / "ba_data" / "python-site-packages"),
    str(ROOT / "dist" / "ba_data" / "python"),
]

from bacommon.servermanager import ServerConfig
from efro.dataclassio import dataclass_from_dict, dataclass_validate
from flask import Flask, jsonify, make_response, request, send_from_directory
from waitress import serve

CONFIG_PATH = ROOT / "config.json"
MOD_SETTINGS_PATH = ROOT / "dist" / "ba_root" / "mods" / "setting.json"
PLAYERS_PATH = ROOT / "dist" / "ba_root" / "mods" / "playersdata"
BACKUP_PATH = ROOT / ".admin-backups"
UI_PATH = ROOT / "admin_ui"
SESSION_COOKIE = "bcs_admin_session"
SESSION_LIFETIME = 8 * 60 * 60
MAX_JSON_BYTES = 2 * 1024 * 1024

FIELD_GROUPS = {
    "party_name": "Identity",
    "party_is_public": "Identity",
    "stats_url": "Identity",
    "public_ipv4_address": "Network",
    "public_ipv6_address": "Network",
    "port": "Network",
    "max_party_size": "Network",
    "session_max_players_override": "Network",
    "authenticate_clients": "Access",
    "admins": "Access",
    "enable_default_kick_voting": "Access",
    "enable_queue": "Access",
    "protocol_version": "Access",
    "enable_telnet": "Access",
    "session_type": "Playlist",
    "playlist_code": "Playlist",
    "playlist_inline": "Playlist",
    "playlist_shuffle": "Playlist",
    "auto_balance_teams": "Playlist",
    "coop_campaign": "Playlist",
    "coop_level": "Playlist",
    "teams_series_length": "Gameplay",
    "ffa_series_length": "Gameplay",
    "show_tutorial": "Gameplay",
    "team_names": "Gameplay",
    "team_colors": "Gameplay",
    "clean_exit_minutes": "Operations",
    "unclean_exit_minutes": "Operations",
    "idle_exit_minutes": "Operations",
    "player_rejoin_cooldown": "Operations",
    "stress_test_players": "Operations",
    "log_levels": "Operations",
    "dont_write_bytecode": "Operations",
}

FIELD_HELP = {
    "party_name": "Name shown in the public server list.",
    "party_is_public": "List the server publicly; private servers remain joinable by IP.",
    "port": "BombSquad game port. The server uses UDP for gameplay.",
    "max_party_size": "Maximum devices in the party, including the server.",
    "session_max_players_override": "Override the session player limit; leave empty for the playlist default.",
    "session_type": "Used when no shared playlist code is set.",
    "playlist_code": "Shared playlist code. Set to null to use the selected session type.",
    "playlist_inline": "Inline playlist data; set the session type to match.",
    "admins": "Account IDs permitted to use server-admin privileges.",
    "protocol_version": "Protocol 36 enables V2 account IDs; ensure clients support it.",
    "enable_telnet": "Deprecated by Ballistica and no longer provides telnet access.",
    "stats_url": "Optional server-browser link to your stats page or community.",
    "team_names": "Custom team names for teams sessions.",
    "team_colors": "Two RGB colors, each component between 0 and 1.",
    "clean_exit_minutes": "Request a graceful restart after this many minutes; empty disables it.",
    "unclean_exit_minutes": "Force a restart after this many minutes; empty disables it.",
    "idle_exit_minutes": "Restart after this many minutes without players; empty disables it.",
    "enable_queue": "Allow players to queue when the party is full.",
}

SECRET_KEY_PARTS = ("password", "token", "secret", "webhook")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_JSON_BYTES

_sessions: dict[str, dict[str, Any]] = {}
_session_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = defaultdict(list)
_login_lock = threading.Lock()


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; connect-src 'self'; "
        "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'self'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return copy.deepcopy(fallback)


def _server_config() -> dict[str, Any]:
    config = dataclasses.asdict(ServerConfig())
    config.update(_read_json(CONFIG_PATH, {}))
    return config


def _settings_files() -> dict[str, Path]:
    return {
        "roles": PLAYERS_PATH / "roles.json",
        "blacklist": PLAYERS_PATH / "blacklist.json",
    }


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)


def _mask_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("" if _is_secret_key(str(key)) else _mask_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


def _secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _is_secret_key(str(key)) and item:
                paths.append(path)
            else:
                paths.extend(_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_secret_paths(item, f"{prefix}.{index}"))
    return paths


def _restore_secrets(
    incoming: Any,
    existing: Any,
    clear_paths: set[str],
    path: str = "",
) -> Any:
    if isinstance(incoming, dict) and isinstance(existing, dict):
        restored = {}
        for key, value in incoming.items():
            child_path = f"{path}.{key}" if path else str(key)
            if _is_secret_key(str(key)):
                if child_path in clear_paths:
                    restored[key] = ""
                elif value == "" or value is None:
                    restored[key] = existing.get(key, "")
                else:
                    restored[key] = value
            else:
                restored[key] = _restore_secrets(
                    value, existing.get(key), clear_paths, child_path
                )
        return restored
    if isinstance(incoming, list) and isinstance(existing, list):
        return [
            _restore_secrets(
                value,
                existing[index] if index < len(existing) else None,
                clear_paths,
                f"{path}.{index}",
            )
            for index, value in enumerate(incoming)
        ]
    return incoming


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > 24:
        raise ValueError("JSON nesting is too deep.")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Numbers must be finite.")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Object keys must be strings.")
        for item in value.values():
            _validate_json(item, depth + 1)
        return
    raise ValueError("Settings must contain JSON values only.")


def _validate_server_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Server settings must be an object.")
    allowed = {field.name for field in dataclasses.fields(ServerConfig)}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown server settings: {', '.join(sorted(unknown))}")
    config = dataclasses.asdict(ServerConfig())
    config.update(value)
    parsed = dataclass_from_dict(ServerConfig, config)
    dataclass_validate(parsed)
    _validate_json(config)
    return config


def _schema_type(annotation: Any) -> str:
    args = get_args(annotation)
    if args:
        non_null = [arg for arg in args if arg is not type(None)]
        if len(non_null) == 1:
            annotation = non_null[0]
    origin = get_origin(annotation)
    if origin in (list, tuple, dict):
        return "json"
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"
    if annotation is str:
        return "text"
    return "json"


def _server_schema() -> list[dict[str, Any]]:
    hints = get_type_hints(ServerConfig)
    schema = []
    for field in dataclasses.fields(ServerConfig):
        key = field.name
        kind = _schema_type(hints[key])
        item: dict[str, Any] = {
            "key": key,
            "label": key.replace("_", " ").title(),
            "group": FIELD_GROUPS.get(key, "Other"),
            "type": kind,
            "help": FIELD_HELP.get(key, ""),
            "nullable": type(None) in get_args(hints[key]),
        }
        if key == "session_type":
            item["options"] = ["ffa", "teams", "coop"]
            item["type"] = "select"
        schema.append(item)
    return schema


def _write_json(path: Path, value: Any) -> None:
    _validate_json(value)
    BACKUP_PATH.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            relative_path = path.relative_to(ROOT)
        except ValueError:
            relative_path = Path(path.name)
        backup = BACKUP_PATH / stamp / relative_path
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, ensure_ascii=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _load_state() -> dict[str, Any]:
    mod_settings = _read_json(MOD_SETTINGS_PATH, {})
    return {
        "server_config": _server_config(),
        "mod_settings": _mask_secrets(mod_settings),
        "secret_paths": _secret_paths(mod_settings),
        "roles": _read_json(_settings_files()["roles"], {}),
        "blacklist": _read_json(_settings_files()["blacklist"], {}),
        "server_schema": _server_schema(),
        "status": SUPERVISOR.status(),
    }


class ServerSupervisor:
    """Starts, stops, and restarts the normal server-manager wrapper."""

    def __init__(self) -> None:
        self._process: Any = None
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return {
                "running": running,
                "pid": self._process.pid if running else None,
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self.status()
            self._process = subprocess.Popen(
                [sys.executable, str(ROOT / "bombsquad_server")], cwd=ROOT
            )
            return self.status()

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._process = None
                return
            process.terminate()
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            self._process = None

    def restart(self) -> None:
        with self._lock:
            self.stop()
            time.sleep(0.5)
            self.start()


SUPERVISOR = ServerSupervisor()


def _session_from_request() -> tuple[str, dict[str, Any]] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _session_lock:
        session = _sessions.get(token)
        if session is None:
            return None
        if session["expires"] < time.time():
            _sessions.pop(token, None)
            return None
        session["expires"] = time.time() + SESSION_LIFETIME
        return token, session


def _authenticated(*, csrf: bool = False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            session = _session_from_request()
            if session is None:
                return jsonify({"error": "Authentication required."}), 401
            if csrf and not hmac.compare_digest(
                request.headers.get("X-CSRF-Token", ""),
                session[1]["csrf"],
            ):
                return jsonify({"error": "Invalid request token."}), 403
            return function(*args, **kwargs)

        return wrapped

    return decorate


def _request_json() -> dict[str, Any]:
    data = request.get_json(silent=False)
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    _validate_json(data)
    return data


@app.get("/healthz")
def healthcheck():
    return jsonify({"ok": True})


@app.get("/")
def index():
    return send_from_directory(UI_PATH, "index.html")


@app.get("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory(UI_PATH, filename)


@app.post("/api/login")
def login():
    password = os.environ.get("ADMIN_UI_PASSWORD", "")
    source = request.remote_addr or "unknown"
    now = time.time()
    with _login_lock:
        recent = [stamp for stamp in _login_attempts[source] if now - stamp < 300]
        _login_attempts[source] = recent
        if len(recent) >= 8:
            return jsonify({"error": "Too many attempts. Try again in five minutes."}), 429
        _login_attempts[source].append(now)
    try:
        body = _request_json()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid login request."}), 400
    if not hmac.compare_digest(str(body.get("password", "")), password):
        return jsonify({"error": "Incorrect password."}), 401
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with _session_lock:
        _sessions[token] = {"csrf": csrf, "expires": now + SESSION_LIFETIME}
    response = make_response(jsonify({"csrf_token": csrf}))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=os.environ.get("ADMIN_UI_SECURE_COOKIE", "0") == "1",
        samesite="Strict",
        max_age=SESSION_LIFETIME,
        path="/",
    )
    with _login_lock:
        _login_attempts.pop(source, None)
    return response


@app.get("/api/session")
@_authenticated()
def session_info():
    session = _session_from_request()
    return jsonify({"csrf_token": session[1]["csrf"]})


@app.post("/api/logout")
@_authenticated(csrf=True)
def logout():
    token = request.cookies.get(SESSION_COOKIE, "")
    with _session_lock:
        _sessions.pop(token, None)
    response = make_response(jsonify({"ok": True}))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/state")
@_authenticated()
def get_state():
    return jsonify(_load_state())


@app.get("/api/status")
@_authenticated()
def get_status():
    return jsonify(SUPERVISOR.status())


@app.put("/api/state")
@_authenticated(csrf=True)
def save_state():
    try:
        data = _request_json()
        was_running = SUPERVISOR.status()["running"]
        current_mods = _read_json(MOD_SETTINGS_PATH, {})
        clear_secrets = set(data.get("clear_secrets", []))
        updates: list[tuple[Path, Any]] = []
        if "server_config" in data:
            updates.append((CONFIG_PATH, _validate_server_config(data["server_config"])))
        if "mod_settings" in data:
            updated_mods = _restore_secrets(
                data["mod_settings"], current_mods, clear_secrets
            )
            _validate_json(updated_mods)
            if not isinstance(updated_mods, dict):
                raise ValueError("Mod settings must be a JSON object.")
            updates.append((MOD_SETTINGS_PATH, updated_mods))
        for name in ("roles", "blacklist"):
            if name in data:
                value = data[name]
                if not isinstance(value, dict):
                    raise ValueError(f"{name.title()} must be a JSON object.")
                updates.append((_settings_files()[name], value))
        if not updates:
            raise ValueError("No settings were provided.")
        for path, value in updates:
            _write_json(path, value)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    if was_running:
        threading.Thread(target=SUPERVISOR.restart, daemon=True).start()
    return jsonify({"ok": True, "restarting": was_running})


@app.post("/api/server/<action>")
@_authenticated(csrf=True)
def server_action(action: str):
    if action not in {"start", "stop", "restart"}:
        return jsonify({"error": "Unknown server action."}), 404
    operation = getattr(SUPERVISOR, action)
    threading.Thread(target=operation, daemon=True).start()
    return jsonify({"ok": True, "status": "accepted"}), 202


def main() -> None:
    password = os.environ.get("ADMIN_UI_PASSWORD", "")
    if len(password) < 14:
        raise SystemExit(
            "Set ADMIN_UI_PASSWORD to a unique password of at least 14 characters."
        )
    host = os.environ.get("ADMIN_UI_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("ADMIN_UI_PORT", "8080"))
    except ValueError as exc:
        raise SystemExit("ADMIN_UI_PORT must be a valid TCP port.") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("ADMIN_UI_PORT must be between 1 and 65535.")

    if os.environ.get("ADMIN_UI_START_SERVER", "1") == "1":
        SUPERVISOR.start()

    def shutdown(_signum: int, _frame: Any) -> None:
        SUPERVISOR.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Admin panel listening on http://{host}:{port}", flush=True)
    serve(app, host=host, port=port)


if __name__ == "__main__":
    main()