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

"""Tests for the Codex history import.

The fixture below is a hand-written rollout in the shape the real Codex CLI
writes, so the mapping can be checked without anyone's actual sessions.
"""

from __future__ import annotations

import json
from pathlib import Path

from codelite.db.store import Store
from codelite.importer import (
    discover_rollouts,
    import_codex_sessions,
    parse_rollout,
    preview_codex_sessions,
)


def _line(kind: str, payload: dict, timestamp: str) -> str:
    return json.dumps({"timestamp": timestamp, "type": kind, "payload": payload})


def _write_rollout(directory: Path, name: str = "rollout-2026-08-21T18-57-14-abc.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        _line(
            "session_meta",
            {
                "session_id": "abc",
                "timestamp": "2026-08-21T18:57:14Z",
                "cwd": "/home/someone/project",
                "cli_version": "1.2.3",
            },
            "2026-08-21T18:57:14Z",
        ),
        _line("turn_context", {"model": "gpt-5.6-sol", "effort": "medium"}, "2026-08-21T18:57:15Z"),
        _line("world_state", {"full": True, "state": {}}, "2026-08-21T18:57:15Z"),
        _line(
            "response_item",
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "SYSTEM PROMPT"}],
            },
            "2026-08-21T18:57:16Z",
        ),
        _line(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Fix the flaky test\nand explain why"},
                    {"type": "input_image", "image_url": "data:..."},
                ],
            },
            "2026-08-21T18:57:17Z",
        ),
        _line(
            "response_item",
            {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
            "2026-08-21T18:57:18Z",
        ),
        _line(
            "response_item",
            {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": "pytest -q"},
            "2026-08-21T18:57:19Z",
        ),
        _line(
            "response_item",
            {"type": "custom_tool_call_output", "call_id": "c1", "output": "1 failed"},
            "2026-08-21T18:57:20Z",
        ),
        _line(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "It is a race condition."}],
            },
            "2026-08-21T18:57:21Z",
        ),
        # A call whose output never arrived, as happens when a session is killed.
        _line(
            "response_item",
            {
                "type": "function_call",
                "name": "wait",
                "call_id": "c2",
                "arguments": '{"seconds": 5}',
            },
            "2026-08-21T18:57:22Z",
        ),
        _line(
            "event_msg",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"total_tokens": 4321},
                    "last_token_usage": {"input_tokens": 900, "output_tokens": 100},
                },
            },
            "2026-08-21T18:57:23Z",
        ),
        "{ this line is not valid json",
    ]
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_rollout_maps_a_session(tmp_path: Path) -> None:
    parsed = parse_rollout(_write_rollout(tmp_path / "sessions" / "2026" / "08" / "21"))
    assert parsed is not None

    # The developer prompt and the encrypted reasoning must not come across.
    texts = [item["content"][0]["text"] for item in parsed.items]
    assert not any("SYSTEM PROMPT" in text for text in texts)
    assert parsed.dropped_reasoning == 1

    roles = [item["role"] for item in parsed.items]
    assert roles == ["user", "assistant", "assistant", "assistant"]

    # An image in the turn is recorded even though the bytes are not carried.
    assert "[image]" in texts[0]
    # The tool call and its output are folded into one readable block.
    assert "pytest -q" in texts[1] and "1 failed" in texts[1]
    # A call without an output is still reported.
    assert "wait" in texts[3] and "seconds" in texts[3]

    assert parsed.title == "Fix the flaky test"
    assert parsed.workspace == "/home/someone/project"
    assert parsed.model == "gpt-5.6-sol"
    assert parsed.created_at == "2026-08-21T18:57:14Z"
    assert parsed.total_tokens == 4321
    assert parsed.context_tokens == 1000
    # Every item keeps the time it actually happened.
    assert parsed.timestamps[0] == "2026-08-21T18:57:17Z"
    assert len(parsed.timestamps) == len(parsed.items)


def test_empty_session_is_not_imported(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    directory.mkdir()
    path = directory / "rollout-2026-08-01T00-00-00-empty.jsonl"
    path.write_text(
        _line("session_meta", {"cwd": "/tmp", "timestamp": "2026-08-01T00:00:00Z"}, "x") + "\n",
        encoding="utf-8",
    )
    assert parse_rollout(path) is None


def test_fenced_content_cannot_break_out(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    directory.mkdir()
    path = directory / "rollout-2026-08-01T00-00-01-fence.jsonl"
    path.write_text(
        "\n".join(
            [
                _line(
                    "response_item",
                    {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": "cat x"},
                    "t",
                ),
                _line(
                    "response_item",
                    {"type": "custom_tool_call_output", "call_id": "c", "output": "```\nhi"},
                    "t",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    parsed = parse_rollout(path)
    assert parsed is not None
    # Exactly the two fences this block opens and closes, and no stray third.
    assert parsed.items[0]["content"][0]["text"].count("```") == 4


def test_import_is_idempotent(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    _write_rollout(codex_home / "sessions" / "2026" / "08" / "21")
    _write_rollout(codex_home / "archived_sessions", "rollout-2026-08-02T10-00-00-old.jsonl")
    assert len(discover_rollouts(codex_home)) == 2

    store = Store(tmp_path / "codelite.db")
    preview = preview_codex_sessions(store, codex_home)
    assert preview == {
        "available": 2,
        "new": 2,
        "already_imported": 0,
        "codex_home": str(codex_home),
        "found": True,
    }

    first = import_codex_sessions(store, codex_home=codex_home, default_model="fallback")
    assert (first.imported, first.skipped, first.failed) == (2, 0, 0)

    conversations = store.list_conversations()
    assert len(conversations) == 2
    imported = conversations[0]
    assert imported.title == "Fix the flaky test"
    assert imported.total_tokens == 4321
    assert imported.context_tokens == 1000
    assert store.count_items(imported.id) == 4

    # The whole point of recording the source: a second run changes nothing.
    second = import_codex_sessions(store, codex_home=codex_home)
    assert (second.imported, second.skipped) == (0, 2)
    assert len(store.list_conversations()) == 2


def test_missing_codex_home_is_reported_not_raised(tmp_path: Path) -> None:
    store = Store(tmp_path / "codelite.db")
    preview = preview_codex_sessions(store, tmp_path / "nope")
    assert preview["found"] is False
    assert preview["available"] == 0
    assert import_codex_sessions(store, codex_home=tmp_path / "nope").imported == 0
