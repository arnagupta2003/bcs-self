from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ADMIN_UI_PASSWORD", "unit-test-password-long")

import admin_panel


class StubSupervisor:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.restarted = threading.Event()

    def status(self) -> dict[str, object]:
        return {"running": self.running, "pid": 123 if self.running else None}

    def restart(self) -> None:
        self.restarted.set()


class AdminPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        admin_panel.app.config["TESTING"] = True
        self.client = admin_panel.app.test_client()
        with admin_panel._session_lock:
            admin_panel._sessions.clear()
        with admin_panel._login_lock:
            admin_panel._login_attempts.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_path = root / "config.json"
        self.mod_settings_path = root / "mods" / "setting.json"
        self.players_path = root / "mods" / "playersdata"
        self.backup_path = root / "backups"
        self.config_path.write_text('{"party_name":"Old name","port":43210}\n')
        self.mod_settings_path.parent.mkdir(parents=True)
        self.mod_settings_path.write_text(
            json.dumps(
                {
                    "ballistica_web": {
                        "enable": True,
                        "server_password": "saved-web-secret",
                    },
                    "discordbot": {"enable": False, "token": "saved-bot-token"},
                }
            )
        )
        self.players_path.mkdir(parents=True)
        (self.players_path / "roles.json").write_text('{"owner":{"commands":["ALL"]}}')
        (self.players_path / "blacklist.json").write_text('{"banned-ids":{}}')
        self.supervisor = StubSupervisor()
        self.patches = [
            patch.object(admin_panel, "CONFIG_PATH", self.config_path),
            patch.object(admin_panel, "MOD_SETTINGS_PATH", self.mod_settings_path),
            patch.object(admin_panel, "PLAYERS_PATH", self.players_path),
            patch.object(admin_panel, "BACKUP_PATH", self.backup_path),
            patch.object(admin_panel, "SUPERVISOR", self.supervisor),
            patch.dict(os.environ, {"ADMIN_UI_PASSWORD": "unit-test-password-long"}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    def login(self) -> str:
        response = self.client.post(
            "/api/login", json={"password": "unit-test-password-long"}
        )
        self.assertEqual(response.status_code, 200)
        return response.json["csrf_token"]

    def test_schema_covers_all_server_config_fields(self) -> None:
        self.assertEqual(
            len(admin_panel._server_schema()),
            len(admin_panel.dataclasses.fields(admin_panel.ServerConfig)),
        )
        admin_panel._validate_server_config(admin_panel._server_config())

    def test_state_requires_login_and_csrf_for_writes(self) -> None:
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.login()
        response = self.client.put("/api/state", json={"server_config": {}})
        self.assertEqual(response.status_code, 403)

    def test_save_preserves_masked_secrets_and_backs_up_data(self) -> None:
        csrf = self.login()
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        state = response.json
        self.assertEqual(state["mod_settings"]["discordbot"]["token"], "")
        self.assertIn("discordbot.token", state["secret_paths"])
        state["server_config"]["party_name"] = "Managed name"
        state["roles"]["owner"]["commands"] = ["ALL", "ban"]
        response = self.client.put(
            "/api/state",
            json=state,
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(self.supervisor.restarted.wait(timeout=1))
        saved_config = json.loads(self.config_path.read_text())
        saved_mods = json.loads(self.mod_settings_path.read_text())
        saved_roles = json.loads((self.players_path / "roles.json").read_text())
        self.assertEqual(saved_config["party_name"], "Managed name")
        self.assertEqual(saved_mods["ballistica_web"]["server_password"], "saved-web-secret")
        self.assertEqual(saved_mods["discordbot"]["token"], "saved-bot-token")
        self.assertEqual(saved_roles["owner"]["commands"], ["ALL", "ban"])
        self.assertTrue(list(self.backup_path.rglob("config.json")))

    def test_invalid_config_is_rejected_without_writing(self) -> None:
        csrf = self.login()
        response = self.client.put(
            "/api/state",
            json={"server_config": {"port": "not-a-number"}},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(self.config_path.read_text())["party_name"], "Old name")
        self.assertFalse(self.supervisor.restarted.is_set())

    def test_admin_secrets_are_explicitly_clearable(self) -> None:
        csrf = self.login()
        state = self.client.get("/api/state").json
        response = self.client.put(
            "/api/state",
            json={
                "mod_settings": state["mod_settings"],
                "clear_secrets": ["ballistica_web.server_password"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(self.supervisor.restarted.wait(timeout=1))
        saved = json.loads(self.mod_settings_path.read_text())
        self.assertEqual(saved["ballistica_web"]["server_password"], "")

    def test_saving_while_stopped_does_not_start_server(self) -> None:
        csrf = self.login()
        stopped = StubSupervisor(running=False)
        with patch.object(admin_panel, "SUPERVISOR", stopped):
            response = self.client.put(
                "/api/state",
                json={"server_config": {"party_name": "Saved offline"}},
                headers={"X-CSRF-Token": csrf},
            )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json["restarting"])
        self.assertFalse(stopped.restarted.is_set())
        self.assertEqual(json.loads(self.config_path.read_text())["party_name"], "Saved offline")


if __name__ == "__main__":
    unittest.main()