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

import logging
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from ..config import AppConfig, context_window_for
from ..permission.modes import Mode
from ..provider.auth import AuthError
from ..provider.login import ChatGPTLoginManager
from ..tools import registry
from .runtime import Runtime, RunInProgress

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _context_window(runtime: Runtime, model: str) -> int:
    """Codex's own figure for the model's context window, else the static fallback."""
    try:
        live = runtime.session.context_window(model)
    except Exception:  # noqa: BLE001 - a display figure must not 500 the route
        live = None
    return live or context_window_for(model)


def create_app(config: AppConfig | None = None, runtime: Runtime | None = None) -> Flask:
    app = Flask(__name__)
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

    # -- runs ----------------------------------------------------------------------

    @app.post("/api/conversations/<conversation_id>/messages")
    def send_message(conversation_id: str):
        conversation, error = _conversation_or_404(conversation_id)
        if error:
            return error
        text = str(_body().get("text") or "").strip()
        if not text:
            return jsonify({"error": "`text` must not be empty."}), 400
        try:
            rt().start_run(conversation, text)
        except RunInProgress as busy:
            return jsonify({"error": str(busy)}), 409
        return jsonify({"started": True}), 202

    @app.post("/api/conversations/<conversation_id>/cancel")
    def cancel_run(conversation_id: str):
        return jsonify({"cancelled": rt().cancel_run(conversation_id)})

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

    return app
