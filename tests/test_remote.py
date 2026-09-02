from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codelite.app.server import create_app
from codelite.config import AppConfig
from codelite.remote import REMOTE_HOST_HEADER, RemoteManager


class RemoteControlTests(unittest.TestCase):
    def test_password_creates_an_expiring_session(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = RemoteManager(Path(root), "http://127.0.0.1:10532")
            password = manager._new_password()

            self.assertIsNone(manager.login("wrong-password"))
            token = manager.login(password)
            self.assertIsNotNone(token)
            self.assertTrue(manager.authenticated(token))
            manager.stop()
            self.assertFalse(manager.authenticated(token))

    def test_remote_host_requires_login_but_localhost_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            app = create_app(AppConfig(data_dir=Path(root)))
            client = app.test_client()

            local = client.get("/api/meta", headers={"Host": "127.0.0.1:10532"})
            remote = client.get("/api/meta", headers={"Host": REMOTE_HOST_HEADER})

            self.assertEqual(local.status_code, 200)
            self.assertEqual(remote.status_code, 401)

    def test_remote_login_unlocks_the_normal_app(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            app = create_app(AppConfig(data_dir=Path(root)))
            manager = app.config["remote_manager"]
            password = manager._new_password()
            client = app.test_client()

            login = client.post(
                "/remote/login",
                data={"password": password},
                headers={"Host": REMOTE_HOST_HEADER},
            )
            opened = client.get("/api/meta", headers={"Host": REMOTE_HOST_HEADER})

            self.assertEqual(login.status_code, 302)
            self.assertEqual(opened.status_code, 200)

    def test_download_verifies_and_installs_binary(self) -> None:
        binary = b"verified cloudflared"
        release = json.dumps(
            {
                "assets": [
                    {
                        "name": "cloudflared-test",
                        "browser_download_url": "https://example.test/cloudflared",
                        "digest": f"sha256:{hashlib.sha256(binary).hexdigest()}",
                    }
                ]
            }
        ).encode()
        responses = [io.BytesIO(release), io.BytesIO(binary)]
        with tempfile.TemporaryDirectory() as root, patch(
            "codelite.remote._asset_name", return_value="cloudflared-test"
        ), patch(
            "codelite.remote.urllib.request.urlopen", side_effect=responses
        ):
            manager = RemoteManager(Path(root), "http://127.0.0.1:10532")
            status = manager.download()

            self.assertTrue(status["installed"])
            self.assertEqual(manager.binary.read_bytes(), binary)

    def test_tunnel_start_returns_url_and_password(self) -> None:
        class FakeProcess:
            stderr = iter(["INF https://quiet-field.trycloudflare.com ready\n"])

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as root:
            manager = RemoteManager(Path(root), "http://127.0.0.1:10532")
            manager.binary.parent.mkdir(parents=True)
            manager.binary.touch()
            calls = []
            with patch("codelite.remote.subprocess.Popen", side_effect=lambda args, **kwargs: calls.append(args) or FakeProcess()):
                started = manager.start()

            self.assertEqual(started["url"], "https://quiet-field.trycloudflare.com")
            self.assertTrue(started["password"])
            self.assertIn(REMOTE_HOST_HEADER, calls[0])

    def test_status_is_unsupported_on_other_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch(
            "codelite.remote.sys_platform", return_value="darwin"
        ):
            manager = RemoteManager(Path(root), "http://127.0.0.1:10532")
            self.assertFalse(manager.status()["supported"])


if __name__ == "__main__":
    unittest.main()
