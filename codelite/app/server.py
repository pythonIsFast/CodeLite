# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The local HTTP layer the UI talks to.

Handlers stay deliberately thin -- they decode a request, call
:class:`~codelite.app.runtime.Runtime`, and encode the result. All behaviour
lives in the runtime/agent/permission modules so the UI is never the only way
to drive the agent.

Binds to localhost only and has no authentication: it is the private back end
of a desktop window, not a service.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from ..config import AppConfig, context_window_for
from ..permission.modes import Mode
from ..provider.auth import AuthError
from ..provider.login import ChatGPTLoginManager
from ..project.context import (
    GLOBAL_MEMORY_PATH,
    LSP_CONFIG_PATH,
    MAX_MEMORY_CHARS,
    MCP_CONFIG_PATH,
    MEMORY_PATH,
    discover_skills,
)
from ..tools import registry
from .runtime import Runtime, RunInProgress

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _context_window(runtime: Runtime, model: str) -> int:
    """Codex's own figure for the model's context window, else the static fallback."""
    try:
        live = runtime.session.context_window(model)
    except Exception:  # noqa: BLE001 - a display figure must not 500 the route
        live = None
    return live or context_window_for(model)


_asset_root_cache: Path | None = None


def _unpack_zipapp_assets() -> Path:
    """Extract the interface files from a zipapp into a temporary directory.

    Inside a ``.pyz`` nothing is a real file, and neither Jinja's loader nor
    ``send_from_directory`` can read from a zip -- so the two asset folders are
    unpacked once per process. The directory is deliberately left behind for
    the OS to reap: deleting it would have to outlive every request.
    """
    module = Path(__file__).resolve()
    # In a zipapp `__file__` looks like /path/app.pyz/codelite/app/server.py,
    # so the first parent that is an actual file is the archive itself.
    archive = next((parent for parent in module.parents if parent.is_file()), None)
    if archive is None or not zipfile.is_zipfile(archive):
        raise RuntimeError(
            "Code Lite could not locate its interface files. This build looks "
            "incomplete -- please reinstall it."
        )
    target = Path(tempfile.mkdtemp(prefix="codelite-assets-"))
    prefixes = ("codelite/app/templates/", "codelite/app/static/")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target, [n for n in bundle.namelist() if n.startswith(prefixes)])
    return target / "codelite" / "app"


def _asset_root() -> Path:
    """Where ``templates/`` and ``static/`` actually live at runtime.

    Three shapes have to work: a plain checkout, a PyInstaller build (assets
    sit under the bootloader's extraction dir) and a zipapp (see above).
    Flask's own guess is right only for the first, so it is stated explicitly.
    """
    global _asset_root_cache
    if _asset_root_cache is not None:
        return _asset_root_cache
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        root = Path(bundle) / "codelite" / "app"
    else:
        here = Path(__file__).resolve().parent
        root = here if (here / "templates").is_dir() else _unpack_zipapp_assets()
    _asset_root_cache = root
    return root


def create_app(config: AppConfig | None = None, runtime: Runtime | None = None) -> Flask:
    assets = _asset_root()
    app = Flask(
        __name__,
        template_folder=str(assets / "templates"),
        static_folder=str(assets / "static"),
    )
    app.config["runtime"] = runtime or Runtime(config)
    app.config["login_manager"] = ChatGPTLoginManager(
        app.config["runtime"].session.config
    )

    def rt() -> Runtime:
        return app.config["runtime"]

    def login_manager() -> ChatGPTLoginManager:
        return app.config["login_manager"]

    def _conversation_or_404(conversation_id: str):
        conversation = rt().store.get_conversation(conversation_id)
        if conversation is None:
            return None, (jsonify({"error": "Conversation not found."}), 404)
        return conversation, None

    def _body() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    # -- pages ---------------------------------------------------------------

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    # -- meta ------------------------------------------------------------------

    @app.get("/api/meta")
    def meta():
        runtime = rt()
        return jsonify(
            {
                "cwd": str(Path.cwd()),
                "home": str(Path.home()),
                "default_model": runtime.config.agent_model,
                "judge_model": runtime.config.judge_model,
                "default_mode": runtime.config.default_permission_mode.value,
                "modes": [
                    {"value": mode.value, "label": mode.label} for mode in Mode
                ],
                "tools": registry.names(),
            }
        )

    @app.get("/api/models")
    def models():
        try:
            return jsonify({"models": rt().session.list_models()})
        except AuthError as error:
            return jsonify({"error": str(error), "kind": "auth"}), 401
        except Exception as error:  # noqa: BLE001 - surfaced to the UI as-is
            return jsonify({"error": str(error)}), 502

    # -- ChatGPT account ------------------------------------------------------

    @app.get("/api/auth")
    def auth_status():
        return jsonify(login_manager().status())

    @app.post("/api/auth/login")
    def start_auth_login():
        try:
            authorization_url = login_manager().start()
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 409
        return jsonify({"authorization_url": authorization_url}), 202

    @app.post("/api/auth/cancel")
    def cancel_auth_login():
        login_manager().cancel()
        return jsonify({"cancelled": True})

    @app.get("/api/usage")
    def usage():
        """The ChatGPT plan's allowance, as last reported by Codex.

        Returns ``{"usage": null}`` rather than an error when nothing is known
        yet -- the figures only arrive on the headers of a real request, so a
        fresh install genuinely has none until the first message.
        """
        return jsonify({"usage": rt().plan_usage()})

    # -- conversations ------------------------------------------------------------

    @app.get("/api/conversations")
    def list_conversations():
        return jsonify(
            {"conversations": [c.as_dict() for c in rt().store.list_conversations()]}
        )

    @app.post("/api/conversations")
    def create_conversation():
        body = _body()
        try:
            conversation = rt().create_conversation(
                workspace=body.get("workspace"),
                model=body.get("model"),
                mode=Mode.parse(body.get("mode"), rt().config.default_permission_mode),
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        return jsonify(conversation.as_dict()), 201

    @app.get("/api/conversations/<conversation_id>")
    def get_conversation(conversation_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        runtime = rt()
        return jsonify(
            {
                **conversation.as_dict(),
                # Entries rather than bare payloads: the UI needs each item's
                # timestamp and token count for the per-message footer.
                "entries": runtime.store.load_entries(conversation_id),
                "context_window": _context_window(runtime, conversation.model),
                "busy": runtime.is_busy(conversation),
                "pending_permissions": runtime.pending_permissions(conversation),
            }
        )

    @app.get("/api/conversations/<conversation_id>/files/<path:file_path>")
    def conversation_file(conversation_id: str, file_path: str):
        """Serve one workspace file for an explicitly requested chat showcase."""
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        workspace = Path(conversation.workspace).resolve()
        candidate = (workspace / file_path).resolve()
        if candidate == workspace or workspace not in candidate.parents:
            return jsonify({"error": "File path is outside the workspace."}), 400
        if not candidate.is_file():
            return jsonify({"error": "File not found."}), 404
        return send_file(candidate, as_attachment=False, conditional=True)

    @app.patch("/api/conversations/<conversation_id>")
    def update_conversation(conversation_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        body = _body()
        runtime = rt()

        if "mode" in body:
            runtime.set_mode(conversation, Mode.parse(body.get("mode"), Mode.ASK))
        fields = {
            key: str(body[key]) for key in ("title", "model") if body.get(key) is not None
        }
        if fields:
            runtime.store.update_conversation(conversation_id, **fields)

        refreshed = runtime.store.get_conversation(conversation_id)
        return jsonify(refreshed.as_dict() if refreshed else {})

    @app.delete("/api/conversations/<conversation_id>")
    def delete_conversation(conversation_id: str):
        rt().delete_conversation(conversation_id)
        return jsonify({"deleted": conversation_id})

    @app.get("/api/settings")
    def global_settings():
        data_root = rt().config.data_dir.resolve()
        path = (data_root / GLOBAL_MEMORY_PATH).resolve()
        try:
            memory = path.read_text(encoding="utf-8") if path.is_file() else ""
        except (OSError, UnicodeDecodeError):
            memory = ""
        return jsonify({"memory": memory, "memory_path": str(path)})

    @app.put("/api/settings/memory")
    def save_global_memory():
        content = _body().get("content")
        if not isinstance(content, str):
            return jsonify({"error": "`content` must be a string."}), 400
        if len(content) > MAX_MEMORY_CHARS:
            return jsonify(
                {"error": f"Global memory is limited to {MAX_MEMORY_CHARS:,} characters."}
            ), 400
        data_root = rt().config.data_dir.resolve()
        target = (data_root / GLOBAL_MEMORY_PATH).resolve()
        if data_root not in target.parents:
            return jsonify({"error": "Global memory path leaves the data directory."}), 400
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": f"Could not save global memory: {exc}"}), 500
        return jsonify({"saved": str(target), "content": content})

    @app.get("/api/conversations/<conversation_id>/project-settings")
    def project_settings(conversation_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        workspace = Path(conversation.workspace).resolve()

        def read_or(relative: Path, fallback: str) -> str:
            path = (workspace / relative).resolve()
            if workspace not in path.parents:
                return fallback
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return fallback

        return jsonify(
            {
                "memory": read_or(MEMORY_PATH, ""),
                "mcp": read_or(MCP_CONFIG_PATH, '{\n  "mcpServers": {}\n}\n'),
                "lsp": read_or(LSP_CONFIG_PATH, '{\n  "servers": {}\n}\n'),
                "skills": [
                    skill.as_dict()
                    for skill in discover_skills(workspace, rt().config.data_dir)
                ],
                "project_dir": str(workspace / ".codelite"),
            }
        )

    @app.put("/api/conversations/<conversation_id>/project-settings/<kind>")
    def save_project_settings(conversation_id: str, kind: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        content = _body().get("content")
        if not isinstance(content, str):
            return jsonify({"error": "`content` must be a string."}), 400
        paths = {"memory": MEMORY_PATH, "mcp": MCP_CONFIG_PATH, "lsp": LSP_CONFIG_PATH}
        relative = paths.get(kind)
        if relative is None:
            return jsonify({"error": "Unknown project setting."}), 404
        if kind == "memory":
            if len(content) > MAX_MEMORY_CHARS:
                return jsonify(
                    {"error": f"Project memory is limited to {MAX_MEMORY_CHARS:,} characters."}
                ), 400
        else:
            try:
                parsed = json.loads(content)
            except ValueError as exc:
                return jsonify({"error": f"Invalid JSON: {exc}"}), 400
            required_key = "mcpServers" if kind == "mcp" else "servers"
            if not isinstance(parsed, dict) or not isinstance(parsed.get(required_key), dict):
                return jsonify({"error": f"Config requires a `{required_key}` object."}), 400
            content = json.dumps(parsed, indent=2) + "\n"
        workspace = Path(conversation.workspace).resolve()
        target = (workspace / relative).resolve()
        if workspace not in target.parents:
            return jsonify({"error": "Project setting path leaves the workspace."}), 400
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return jsonify({"error": f"Could not save {relative}: {exc}"}), 500
        return jsonify({"saved": str(relative), "content": content})

    @app.post("/api/conversations/<conversation_id>/uploads")
    def upload_file(conversation_id: str):
        """Accept a user-pasted file and save it inside that chat's workspace."""
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "Attach one file in the `file` field."}), 400
        filename = secure_filename(uploaded.filename) or "pasted-file"
        workspace = Path(conversation.workspace).resolve()
        upload_dir = (workspace / "uploads").resolve()
        if workspace not in upload_dir.parents:
            return jsonify({"error": "Upload directory leaves the workspace."}), 400
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"{uuid.uuid4().hex}_{filename}"
        temporary = None
        size = 0
        try:
            with tempfile.NamedTemporaryFile(dir=upload_dir, delete=False) as handle:
                temporary = Path(handle.name)
                while chunk := uploaded.stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("Files must be 50 MB or smaller.")
                    handle.write(chunk)
            temporary.replace(target)
        except ValueError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            return jsonify({"error": str(exc)}), 413
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            logger.warning("Could not save uploaded file", exc_info=True)
            return jsonify({"error": f"Could not save upload: {exc}"}), 500

        return jsonify(
            {
                "path": str(target.relative_to(workspace)),
                "name": filename,
                "size": size,
                "type": uploaded.mimetype or "application/octet-stream",
            }
        ), 201

    # -- runs ----------------------------------------------------------------------

    @app.post("/api/conversations/<conversation_id>/messages")
    def send_message(conversation_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        body = _body()
        text = str(body.get("text") or "").strip()
        if not text:
            return jsonify({"error": "`text` must not be empty."}), 400
        workspace = Path(conversation.workspace).resolve()
        attachments: list[dict[str, str]] = []
        supplied_attachments = body.get("attachments")
        if supplied_attachments is not None and not isinstance(supplied_attachments, list):
            return jsonify({"error": "`attachments` must be a list."}), 400
        for item in supplied_attachments or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                return jsonify({"error": "Each attachment needs a file path."}), 400
            candidate = (workspace / item["path"]).resolve()
            if candidate == workspace or workspace not in candidate.parents or not candidate.is_file():
                return jsonify({"error": "An attachment is not a workspace file."}), 400
            attachments.append(
                {
                    "path": str(candidate.relative_to(workspace)),
                    "name": str(item.get("name") or candidate.name),
                    "type": str(item.get("type") or "application/octet-stream"),
                }
            )
        try:
            rt().start_run(conversation, text, attachments)
        except RunInProgress as busy:
            return jsonify({"error": str(busy)}), 409
        return jsonify({"started": True}), 202

    @app.post("/api/conversations/<conversation_id>/cancel")
    def cancel_run(conversation_id: str):
        return jsonify({"cancelled": rt().cancel_run(conversation_id)})

    @app.post("/api/conversations/<conversation_id>/compact")
    def compact_conversation(conversation_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        try:
            rt().start_compaction(conversation)
        except RunInProgress as busy:
            return jsonify({"error": str(busy)}), 409
        return jsonify({"started": True}), 202

    @app.get("/api/conversations/<conversation_id>/events")
    def events(conversation_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        return Response(rt().subscribe(conversation), headers=SSE_HEADERS)

    # -- permissions -----------------------------------------------------------------

    @app.post("/api/conversations/<conversation_id>/permission/<request_id>")
    def reply_permission(conversation_id: str, request_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        body = _body()
        accepted = rt().reply_permission(
            conversation,
            request_id,
            str(body.get("reply") or ""),
            str(body.get("feedback") or ""),
        )
        if not accepted:
            return jsonify({"error": "No such pending request, or invalid reply."}), 404
        return jsonify({"ok": True})

    @app.post("/api/conversations/<conversation_id>/question/<question_id>")
    def reply_question(conversation_id: str, question_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        accepted = rt().reply_question(conversation, question_id, str(_body().get("answer") or ""))
        if not accepted:
            return jsonify({"error": "No such pending question, or invalid answer."}), 404
        return jsonify({"ok": True})

    return app
