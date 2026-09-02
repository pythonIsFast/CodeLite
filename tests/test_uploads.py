from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from codelite.app.server import create_app
from codelite.config import AppConfig


class UploadTests(unittest.TestCase):
    def test_uploads_are_central_and_can_be_served(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            workspace.mkdir()
            data_dir = root_path / "data"
            app = create_app(AppConfig(data_dir=data_dir))
            self.assertEqual(app.config["MAX_CONTENT_LENGTH"], 50 * 1024 * 1024)
            client = app.test_client()
            conversation = client.post("/api/conversations", json={"workspace": str(workspace)}).get_json()
            conversation_id = conversation["id"]

            response = client.post(
                f"/api/conversations/{conversation_id}/uploads",
                data={"file": (io.BytesIO(b"upload content"), "notes.txt")},
            )

            self.assertEqual(response.status_code, 201)
            upload = response.get_json()
            self.assertTrue(upload["path"].startswith(f"uploads/{conversation_id}/"))
            stored = data_dir / upload["path"]
            self.assertEqual(stored.read_bytes(), b"upload content")
            self.assertFalse((workspace / "uploads").exists())
            served = client.get(f"/api/conversations/{conversation_id}/files/{upload['path']}")
            direct = client.get(
                f"/api/conversations/{conversation_id}/uploads/{Path(upload['path']).name}"
            )
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.data, b"upload content")
            self.assertEqual(direct.status_code, 200)
            self.assertEqual(direct.data, b"upload content")

    def test_oversized_requests_are_rejected_before_route_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            app = create_app(AppConfig(data_dir=Path(root)))
            app.config["MAX_CONTENT_LENGTH"] = 32
            response = app.test_client().post(
                "/api/conversations", data=b"x" * 33, content_type="application/json"
            )

            self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
