#!/usr/bin/env python3
"""Tiny MCP stdio fixture used by the dependency-free integration tests."""

from __future__ import annotations

import json
import sys


for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    request_id = message.get("id")
    if request_id is None:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo arguments",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": json.dumps(message["params"]["arguments"])}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()
