# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Ported from openai-oauth (https://github.com/EvanZhouDev/openai-oauth),
# packages/openai-oauth/src/login.ts, Apache-2.0.

"""Interactive ChatGPT login with a short-lived localhost callback server."""

from __future__ import annotations

import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import (
    OAuthRequest,
    create_oauth_request,
    exchange_oauth_code,
    get_auth_status,
    save_auth_tokens,
)
from .config import DEFAULT_OAUTH_SCOPE, ProviderConfig

logger = logging.getLogger(__name__)

LOGIN_CALLBACK_HOST = "127.0.0.1"
LOGIN_REDIRECT_HOST = "localhost"
LOGIN_CALLBACK_PORT = 1455
LOGIN_TIMEOUT_SECONDS = 5 * 60


def _callback_page(title: str, message: str) -> bytes:
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">
<title>{html.escape(title)}</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#faf9f6;color:#2d2a26;
font:16px/1.5 system-ui,sans-serif}}main{{max-width:32rem;padding:2rem;text-align:center}}
h1{{font-size:1.4rem}}p{{color:#6b6558}}
</style></head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></main></body></html>""".encode()


class ChatGPTLoginManager:
    """Own at most one login attempt and expose only secret-free state."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._request: OAuthRequest | None = None
        self._callback = threading.Event()
        self._code: str | None = None
        self._callback_error: str | None = None
        self._phase = "idle"
        self._error: str | None = None

    def status(self) -> dict[str, Any]:
        auth = get_auth_status(self.config)
        with self._lock:
            phase = self._phase
            error = self._error or auth.error
        return {
            "authenticated": auth.authenticated,
            "account_label": auth.account_label,
            "email": auth.email,
            "auth_file": str(auth.source_path) if auth.source_path else None,
            "login_status": phase,
            "error": error,
        }

    def start(self) -> str:
        """Start the callback listener and return the URL to open externally."""
        with self._lock:
            if self._phase == "waiting" and self._request is not None:
                return self._request.authorization_url
            self._reset_attempt_locked()

            redirect_uri = (
                f"http://{LOGIN_REDIRECT_HOST}:{LOGIN_CALLBACK_PORT}/auth/callback"
            )
            oauth_request = create_oauth_request(
                redirect_uri=redirect_uri,
                client_id=self.config.oauth_client_id,
                issuer=self.config.oauth_issuer,
                scope=DEFAULT_OAUTH_SCOPE,
            )
            manager = self

            class CallbackHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                    manager._handle_callback(self)

                def log_message(self, format: str, *args: object) -> None:
                    return

            try:
                server = ThreadingHTTPServer(
                    (LOGIN_CALLBACK_HOST, LOGIN_CALLBACK_PORT), CallbackHandler
                )
            except OSError as error:
                raise RuntimeError(
                    f"ChatGPT login needs localhost port {LOGIN_CALLBACK_PORT}, "
                    "but it is already in use."
                ) from error

            server.daemon_threads = True
            self._server = server
            self._request = oauth_request
            self._phase = "waiting"

            threading.Thread(
                target=server.serve_forever,
                name="codelite-login-callback",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._finish_login,
                name="codelite-login-exchange",
                daemon=True,
            ).start()
            return oauth_request.authorization_url

    def cancel(self) -> None:
        with self._lock:
            if self._phase != "waiting":
                return
            self._callback_error = "ChatGPT login was cancelled."
            self._callback.set()

    def _reset_attempt_locked(self) -> None:
        old_server = self._server
        self._server = None
        if old_server is not None:
            threading.Thread(target=old_server.shutdown, daemon=True).start()
        self._request = None
        self._callback = threading.Event()
        self._code = None
        self._callback_error = None
        self._phase = "idle"
        self._error = None

    def _handle_callback(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if parsed.path != "/auth/callback":
            handler.send_error(404)
            return
        query = parse_qs(parsed.query)
        callback_error = query.get("error", [None])[0]
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        with self._lock:
            request = self._request
            valid = bool(request and code and state == request.state)
            if callback_error:
                self._callback_error = f"ChatGPT login failed: {callback_error}"
            elif not valid:
                self._callback_error = "The ChatGPT login callback was invalid."
            else:
                self._code = code
            self._callback.set()

        success = valid and not callback_error
        body = _callback_page(
            "Sign-in complete" if success else "Sign-in failed",
            "You can close this window and return to Code Lite."
            if success
            else "Return to Code Lite and try again.",
        )
        handler.send_response(200 if success else 400)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)

    def _finish_login(self) -> None:
        completed = self._callback.wait(LOGIN_TIMEOUT_SECONDS)
        with self._lock:
            server = self._server
            request = self._request
            code = self._code
            callback_error = self._callback_error
        if server is not None:
            server.shutdown()
            server.server_close()

        try:
            if not completed:
                raise RuntimeError("ChatGPT login timed out. Please try again.")
            if callback_error:
                raise RuntimeError(callback_error)
            if request is None or code is None:
                raise RuntimeError("ChatGPT login did not return an authorization code.")
            token = exchange_oauth_code(
                code,
                request.code_verifier,
                request.redirect_uri,
                client_id=self.config.oauth_client_id,
                issuer=self.config.oauth_issuer,
                token_url=self.config.oauth_token_url,
            )
            save_auth_tokens(self.config, token)
        except Exception as error:  # noqa: BLE001 - the UI must receive the failure
            logger.warning("ChatGPT login failed: %s", error)
            with self._lock:
                self._phase = "error"
                self._error = str(error)
                self._server = None
            return

        with self._lock:
            self._phase = "success"
            self._error = None
            self._server = None
