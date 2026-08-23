# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""One token-efficient gateway for project skills and MCP tools."""

from __future__ import annotations

import json
import shlex
from typing import Any

from ..integrations.mcp import (
    McpError,
    get_client,
    is_started,
    load_server_configs,
    server_command,
)
from ..project.context import discover_skills, read_skill
from .base import Tool, ToolError, object_schema
from .context import ToolContext


def _compact_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "inputSchema": tool.get("inputSchema", {}),
        }
        for tool in tools
    ]


def _ensure_mcp_permission(ctx: ToolContext, server: str) -> None:
    if is_started(ctx.workspace, server):
        return
    configs = load_server_configs(ctx.workspace)
    config = configs.get(server)
    if config is None:
        raise ToolError(f"No MCP server named `{server}` is configured.")
    command = shlex.join(server_command(config))
    ctx.permissions.require_shell(command, ctx.task_prompt, str(ctx.workspace))


def _run_extensions(args: dict[str, Any], ctx: ToolContext) -> str:
    action = args.get("action")
    try:
        if action == "list":
            skills = [skill.as_dict() for skill in discover_skills(ctx.workspace, ctx.data_dir)]
            servers = sorted(load_server_configs(ctx.workspace))
            return json.dumps({"skills": skills, "mcp_servers": servers}, indent=2)
        if action == "read_skill":
            name = args.get("name")
            if not isinstance(name, str) or not name:
                raise ToolError("`name` is required for read_skill.")
            try:
                return read_skill(ctx.workspace, ctx.data_dir, name)
            except KeyError as error:
                raise ToolError(f"No skill named `{name}` was found.") from error
        if action in {"list_mcp_tools", "call_mcp_tool"}:
            server = args.get("server")
            if not isinstance(server, str) or not server:
                raise ToolError("`server` is required for MCP actions.")
            _ensure_mcp_permission(ctx, server)
            client = get_client(ctx.workspace, server)
            if action == "list_mcp_tools":
                return json.dumps(_compact_tools(client.list_tools()), indent=2)
            name = args.get("name")
            arguments = args.get("arguments") or {}
            if not isinstance(name, str) or not name:
                raise ToolError("`name` is required for call_mcp_tool.")
            if not isinstance(arguments, dict):
                raise ToolError("`arguments` must be an object.")
            return json.dumps(client.call_tool(name, arguments), indent=2)
    except McpError as error:
        raise ToolError(str(error)) from error
    raise ToolError(
        "`action` must be list, read_skill, list_mcp_tools, or call_mcp_tool."
    )


EXTENSIONS = Tool(
    name="extensions",
    description=(
        "Lazily discover project/user skills and configured MCP servers. Read only a "
        "relevant skill, or list/call tools on one MCP server, to minimize context usage."
    ),
    parameters=object_schema(
        {
            "action": {
                "type": "string",
                "enum": ["list", "read_skill", "list_mcp_tools", "call_mcp_tool"],
            },
            "server": {"type": "string"},
            "name": {"type": "string"},
            "arguments": {"type": "object", "additionalProperties": True},
        },
        required=["action"],
    ),
    run=_run_extensions,
)
