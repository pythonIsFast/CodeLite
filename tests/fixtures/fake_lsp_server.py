#!/usr/bin/env python3
"""Tiny LSP fixture used by the dependency-free integration tests."""

from __future__ import annotations

import json
import sys


def read_message():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.lower() == "content-length":
            length = int(value.strip())
    return json.loads(sys.stdin.buffer.read(length)) if length is not None else None


def write_message(message):
    body = json.dumps(message, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method in {"textDocument/didOpen", "textDocument/didChange"}:
        document = params.get("textDocument") or {}
        write_message(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": document.get("uri"),
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 1},
                            },
                            "severity": 2,
                            "source": "fixture",
                            "message": "Fixture warning",
                        }
                    ],
                },
            }
        )
        continue
    if request_id is None:
        continue
    if method == "initialize":
        result = {"capabilities": {"definitionProvider": True}}
    elif method == "textDocument/definition":
        result = {
            "uri": params["textDocument"]["uri"],
            "range": {
                "start": {"line": 0, "character": 1},
                "end": {"line": 0, "character": 4},
            },
        }
    elif method == "textDocument/references":
        result = []
    elif method == "textDocument/documentSymbol":
        result = [
            {
                "name": "example",
                "kind": 12,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 0},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 11},
                },
            }
        ]
    else:
        result = None
    write_message({"jsonrpc": "2.0", "id": request_id, "result": result})
