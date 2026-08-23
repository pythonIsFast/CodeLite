# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Token-efficient facade over optional project language servers."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..integrations.lsp import LspError, get_client, is_started, resolve_spec
from .base import Tool, ToolError, object_schema
from .context import ToolContext

MAX_RESULTS = 100
SEVERITIES = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def _position(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    line = raw.get("line")
    character = raw.get("character")
    if not isinstance(line, int) or not isinstance(character, int):
        return None
    return {"line": line + 1, "character": character + 1}


def _range(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    start = _position(raw.get("start"))
    end = _position(raw.get("end"))
    return {"start": start, "end": end} if start and end else None


def _path_from_uri(uri: Any, workspace: Path) -> str | None:
    if not isinstance(uri, str):
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    path = Path(unquote(parsed.path))
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)


def _locations(raw: Any, workspace: Path) -> list[dict[str, Any]]:
    values = raw if isinstance(raw, list) else [raw]
    results: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("targetUri")
        location_range = item.get("range") or item.get("targetSelectionRange")
        results.append(
            {
                "path": _path_from_uri(uri, workspace),
                "range": _range(location_range),
            }
        )
    return results[:MAX_RESULTS]


def _symbols(raw: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def visit(items: Any, container: str = "") -> None:
        if not isinstance(items, list) or len(results) >= MAX_RESULTS:
            return
        for item in items:
            if not isinstance(item, dict) or len(results) >= MAX_RESULTS:
                continue
            name = item.get("name")
            if isinstance(name, str):
                results.append(
                    {
                        "name": name,
                        "container": item.get("containerName") or container or None,
                        "kind": item.get("kind"),
                        "range": _range(item.get("selectionRange") or item.get("range")),
                    }
                )
                visit(item.get("children"), name)

    visit(raw)
    return results


def _diagnostics(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "severity": SEVERITIES.get(item.get("severity"), "unknown"),
            "message": item.get("message"),
            "source": item.get("source"),
            "code": item.get("code"),
            "range": _range(item.get("range")),
        }
        for item in raw[:MAX_RESULTS]
    ]


def _run_code_intelligence(args: dict[str, Any], ctx: ToolContext) -> str:
    action = args.get("action")
    if action not in {"diagnostics", "definition", "references", "symbols"}:
        raise ToolError("`action` must be diagnostics, definition, references, or symbols.")
    line = args.get("line")
    character = args.get("character")
    if action in {"definition", "references"}:
        if not isinstance(line, int) or line < 1:
            raise ToolError("`line` must be a positive 1-based integer.")
        if not isinstance(character, int) or character < 1:
            raise ToolError("`character` must be a positive 1-based integer.")
    path = ctx.resolve(args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"{ctx.relative(path)} is not a file.")
    try:
        spec = resolve_spec(ctx.workspace, path)
        if not is_started(ctx.workspace, spec):
            command = shlex.join(spec.command)
            ctx.permissions.require_shell(command, ctx.task_prompt, str(ctx.workspace))
        client = get_client(ctx.workspace, spec)
        uri = client.sync_document(path) if action != "diagnostics" else None
        if action == "diagnostics":
            result: Any = _diagnostics(client.diagnostics(path))
        elif action in {"definition", "references"}:
            assert isinstance(line, int) and isinstance(character, int)
            params: dict[str, Any] = {
                "textDocument": {"uri": uri},
                "position": {"line": line - 1, "character": character - 1},
            }
            method = "textDocument/definition"
            if action == "references":
                method = "textDocument/references"
                params["context"] = {"includeDeclaration": True}
            result = _locations(client.request(method, params), ctx.workspace)
        elif action == "symbols":
            result = _symbols(
                client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
            )
    except LspError as error:
        raise ToolError(str(error)) from error
    return json.dumps(
        {"server": spec.name, "path": ctx.relative(path), "results": result},
        indent=2,
    )


CODE_INTELLIGENCE = Tool(
    name="code_intelligence",
    description=(
        "Use an installed language server for precise diagnostics, definitions, references, "
        "or document symbols. Prefer this over text search when semantic code knowledge matters."
    ),
    parameters=object_schema(
        {
            "action": {
                "type": "string",
                "enum": ["diagnostics", "definition", "references", "symbols"],
            },
            "path": {"type": "string"},
            "line": {"type": "integer", "description": "1-based line for definition/references."},
            "character": {
                "type": "integer",
                "description": "1-based character for definition/references.",
            },
        },
        required=["action", "path"],
    ),
    run=_run_code_intelligence,
)
