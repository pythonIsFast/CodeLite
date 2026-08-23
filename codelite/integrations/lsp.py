# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Small persistent Language Server Protocol client over stdio."""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import settings
from ..project.context import LSP_CONFIG_PATH

RPC_TIMEOUT_SECONDS = 15.0
DIAGNOSTIC_WAIT_SECONDS = 1.0


class LspError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerSpec:
    name: str
    command: tuple[str, ...]
    extensions: tuple[str, ...]
    language_id: str


BUILTIN_SPECS: tuple[ServerSpec, ...] = (
    ServerSpec("python", ("pyright-langserver", "--stdio"), (".py", ".pyi"), "python"),
    ServerSpec(
        "typescript",
        ("typescript-language-server", "--stdio"),
        (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        "typescript",
    ),
    ServerSpec("rust", ("rust-analyzer",), (".rs",), "rust"),
    ServerSpec("go", ("gopls",), (".go",), "go"),
    ServerSpec("clang", ("clangd",), (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"), "cpp"),
)


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Any = None


def _uri(path: Path) -> str:
    return path.resolve().as_uri()


class LspClient:
    def __init__(self, spec: ServerSpec, workspace: Path) -> None:
        self.spec = spec
        self.workspace = workspace.resolve()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._versions: dict[str, int] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diagnostic_condition = threading.Condition()
        try:
            self._process = subprocess.Popen(  # noqa: S603 - explicit argv, no shell
                list(spec.command),
                cwd=self.workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as error:
            raise LspError(f"Could not start {spec.name} language server: {error}") from error
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"codelite-lsp-{spec.name}",
            daemon=True,
        )
        self._reader.start()
        self.request(
            "initialize",
            {
                "processId": None,
                "clientInfo": {"name": "Code Lite", "version": "0.1"},
                "rootUri": _uri(self.workspace),
                "workspaceFolders": [{"uri": _uri(self.workspace), "name": self.workspace.name}],
                "capabilities": {
                    "textDocument": {
                        "definition": {"linkSupport": True},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                    "workspace": {"workspaceFolders": True, "configuration": True},
                },
            },
        )
        self.notify("initialized", {})

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def _write(self, message: dict[str, Any]) -> None:
        if not self.alive or self._process.stdin is None:
            raise LspError(f"{self.spec.name} language server is not running.")
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            try:
                self._process.stdin.write(framed)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise LspError(f"{self.spec.name} language server closed its input.") from error

    def request(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        if timeout is None:
            timeout = float(settings.active("lsp_timeout_seconds"))
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _Pending()
            self._pending[request_id] = pending
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if not pending.event.wait(timeout):
                raise LspError(f"Language server timed out during `{method}`.")
            if pending.error is not None:
                message = pending.error.get("message") if isinstance(pending.error, dict) else pending.error
                raise LspError(f"Language server `{method}` failed: {message}")
            return pending.result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_message(self) -> dict[str, Any] | None:
        stdout = self._process.stdout
        if stdout is None:
            return None
        length: int | None = None
        while True:
            line = stdout.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            key, separator, value = line.decode("ascii", errors="ignore").partition(":")
            if separator and key.lower() == "content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    return None
        if length is None or length < 0:
            return None
        body = stdout.read(length)
        try:
            message = json.loads(body)
        except ValueError:
            return None
        return message if isinstance(message, dict) else None

    def _server_request_result(self, method: str, params: Any) -> Any:
        if method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else []
            return [None for _ in items or []]
        if method == "workspace/workspaceFolders":
            return [{"uri": _uri(self.workspace), "name": self.workspace.name}]
        if method in {"client/registerCapability", "client/unregisterCapability"}:
            return None
        return None

    def _read_loop(self) -> None:
        while self.alive:
            message = self._read_message()
            if message is None:
                break
            request_id = message.get("id")
            if request_id is not None and ("result" in message or "error" in message):
                with self._pending_lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    pending.result = message.get("result")
                    pending.error = message.get("error")
                    pending.event.set()
                continue
            method = message.get("method")
            if method == "textDocument/publishDiagnostics":
                params = message.get("params") or {}
                uri = params.get("uri")
                diagnostics = params.get("diagnostics")
                if isinstance(uri, str) and isinstance(diagnostics, list):
                    with self._diagnostic_condition:
                        self._diagnostics[uri] = [item for item in diagnostics if isinstance(item, dict)]
                        self._diagnostic_condition.notify_all()
            elif request_id is not None and isinstance(method, str):
                try:
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": self._server_request_result(method, message.get("params")),
                        }
                    )
                except LspError:
                    break
        with self._pending_lock:
            pending_calls = list(self._pending.values())
        for pending in pending_calls:
            pending.error = {"message": "Language server stopped."}
            pending.event.set()

    def sync_document(self, path: Path) -> str:
        uri = _uri(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise LspError(f"Could not read {path}: {error}") from error
        version = self._versions.get(uri, 0) + 1
        self._versions[uri] = version
        if version == 1:
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self.spec.language_id,
                        "version": version,
                        "text": text,
                    }
                },
            )
        else:
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        return uri

    def diagnostics(self, path: Path) -> list[dict[str, Any]]:
        uri = _uri(path)
        with self._diagnostic_condition:
            self._diagnostics.pop(uri, None)
        self.sync_document(path)
        with self._diagnostic_condition:
            self._diagnostic_condition.wait_for(
                lambda: uri in self._diagnostics,
                timeout=float(settings.active("lsp_diagnostic_wait_seconds")),
            )
            return list(self._diagnostics.get(uri, []))

    def close(self) -> None:
        if self.alive:
            try:
                self.notify("exit", {})
            except LspError:
                pass
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()


def _custom_specs(workspace: Path) -> list[ServerSpec]:
    root = Path(workspace).resolve()
    path = (root / LSP_CONFIG_PATH).resolve()
    if root not in path.parents:
        raise LspError(f"{LSP_CONFIG_PATH} resolves outside the workspace.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise LspError(f"Could not read {LSP_CONFIG_PATH}: {error}") from error
    servers = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        raise LspError(f"{LSP_CONFIG_PATH} must contain a `servers` object.")
    specs: list[ServerSpec] = []
    for name, config in servers.items():
        if not isinstance(config, dict):
            continue
        command = config.get("command")
        args = config.get("args") or []
        extensions = config.get("extensions") or []
        language_id = config.get("languageId") or name
        if (
            isinstance(command, str)
            and isinstance(args, list)
            and all(isinstance(value, str) for value in args)
            and isinstance(extensions, list)
            and all(isinstance(value, str) for value in extensions)
        ):
            specs.append(
                ServerSpec(
                    str(name),
                    (command, *args),
                    tuple(value if value.startswith(".") else f".{value}" for value in extensions),
                    str(language_id),
                )
            )
    return specs


def _command_available(workspace: Path, command: str) -> bool:
    candidate = Path(command)
    if candidate.is_absolute():
        return candidate.is_file()
    if "/" in command or "\\" in command:
        return (Path(workspace) / candidate).is_file()
    return shutil.which(command) is not None


def resolve_spec(workspace: Path, path: Path) -> ServerSpec:
    suffix = path.suffix.lower()
    custom = _custom_specs(workspace)
    for spec in (*custom, *BUILTIN_SPECS):
        if suffix in spec.extensions and _command_available(workspace, spec.command[0]):
            return spec
    candidates = [spec.command[0] for spec in (*custom, *BUILTIN_SPECS) if suffix in spec.extensions]
    if candidates:
        raise LspError(
            f"No language server for `{suffix}` is installed. Expected one of: "
            + ", ".join(candidates)
        )
    raise LspError(
        f"No language server is configured for `{suffix or path.name}`. "
        f"Add one to {LSP_CONFIG_PATH}."
    )


_clients: dict[tuple[str, str], LspClient] = {}
_clients_lock = threading.Lock()


def is_started(workspace: Path, spec: ServerSpec) -> bool:
    key = (str(Path(workspace).resolve()), spec.name)
    with _clients_lock:
        client = _clients.get(key)
    return bool(client and client.alive and client.spec == spec)


def get_client(workspace: Path, spec: ServerSpec) -> LspClient:
    workspace = Path(workspace).resolve()
    key = (str(workspace), spec.name)
    with _clients_lock:
        existing = _clients.get(key)
        if existing and existing.alive and existing.spec == spec:
            return existing
        if existing:
            existing.close()
        client = LspClient(spec, workspace)
        _clients[key] = client
        return client


def _close_all() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


atexit.register(_close_all)
