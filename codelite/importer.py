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

"""Imports conversation history from the official Codex CLI.

Codex writes one JSONL "rollout" file per session under ``~/.codex/sessions``
(and ``archived_sessions``), named ``rollout-<timestamp>-<uuid>.jsonl``. Every
line is an envelope::

    {"timestamp": "...", "type": "...", "payload": {...}}

with five envelope types: ``session_meta`` (once, first), ``turn_context``,
``world_state``, ``response_item`` (the actual Responses-API items) and
``event_msg`` (UI events, including ``token_count``).

Why the transcript is rebuilt as plain messages
-----------------------------------------------
Code Lite keeps one item list per conversation and sends it straight back to
the model, so anything imported has to remain a *valid request*. Codex's items
are not:

* ``reasoning`` items carry ``encrypted_content`` bound to the response chain
  they came from. Replaying them in a new chat is not something the API
  accepts, so they are dropped rather than imported and later rejected.
* Tool calls reference Codex's own tools (``exec``, ``wait``), which Code Lite
  does not declare. A ``function_call`` naming a tool that is not in the
  request is an error, not a display quirk.

So a tool call and its output are folded into the assistant's message as a
fenced block. Nothing of the conversation's content is lost -- it stops being
a replayable tool call and becomes readable history, which is what an import
of somebody else's session can honestly be.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

#: Where Codex keeps its rollouts, both directories treated the same way.
SESSION_DIRS = ("sessions", "archived_sessions")

#: A single tool output can be a megabyte of build log. The transcript keeps
#: the head of it, which is the part that says what happened.
MAX_TOOL_OUTPUT_CHARS = 4_000
MAX_TITLE_CHARS = 70

#: Guards against a truncated or corrupted rollout: a line this long is not
#: something Codex wrote, and json.loads on it would just burn memory.
MAX_LINE_BYTES = 8 * 1024 * 1024

_BACKTICK_RUN = re.compile("`{3,}")


def default_codex_home() -> Path:
    """Codex's data directory, honoring ``$CODEX_HOME`` as its CLI does."""
    home = os.environ.get("CODEX_HOME")
    return Path(home) if home else Path.home() / ".codex"


@dataclass
class ParsedRollout:
    """One Codex session, already reduced to what Code Lite can store."""

    source_id: str
    path: Path
    title: str
    workspace: str
    model: str
    created_at: str
    items: list[dict[str, Any]] = field(default_factory=list)
    timestamps: list[str] = field(default_factory=list)
    context_tokens: int = 0
    total_tokens: int = 0
    dropped_reasoning: int = 0

    @property
    def message_count(self) -> int:
        return len(self.items)


@dataclass
class ImportReport:
    """What an import run did, in the terms the UI reports back."""

    imported: int = 0
    skipped: int = 0
    failed: int = 0
    empty: int = 0
    titles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "empty": self.empty,
            "titles": self.titles,
            "errors": self.errors,
        }


def discover_rollouts(codex_home: Path | None = None) -> list[Path]:
    """Every rollout file, newest first. Missing directories are not an error."""
    root = codex_home or default_codex_home()
    found: list[Path] = []
    for name in SESSION_DIRS:
        directory = root / name
        if directory.is_dir():
            found.extend(directory.rglob("rollout-*.jsonl"))
    # The name starts with an ISO timestamp, so it sorts chronologically --
    # no need to stat every file.
    return sorted(found, key=lambda p: p.name, reverse=True)


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    """Yield decoded envelopes, skipping anything unreadable.

    Rollouts are appended to while a session runs, so the last line of a file
    from a crashed session is routinely half-written. One bad line must not
    cost the whole conversation.
    """
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip() or len(line) > MAX_LINE_BYTES:
                continue
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
            if isinstance(envelope, dict):
                yield envelope


def _text_from_content(content: Any) -> str:
    """Flatten a Responses-API content array into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for element in content:
        if not isinstance(element, dict):
            continue
        kind = element.get("type")
        if kind in ("input_text", "output_text", "text", "summary_text"):
            text = element.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind in ("input_image", "image"):
            # The bytes are not carried over, but silently dropping the fact
            # that an image was part of the turn would misrepresent it.
            parts.append("[image]")
    return "\n".join(parts)


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Shorten, and make the text safe to put inside a fenced block.

    The renderer splits on triple backticks, so a command or output that
    contains its own fence would close the block early and spill the rest as
    markup. Collapsing runs of three or more backticks to two costs a
    character of fidelity and keeps the transcript readable.
    """
    safe = _BACKTICK_RUN.sub("``", text)
    if len(safe) <= limit:
        return safe
    return f"{safe[:limit]}\n… [{len(safe) - limit:,} more characters]"


def _tool_invocation(payload: dict[str, Any]) -> str:
    """The command or arguments of a call, as the transcript should show it."""
    # `custom_tool_call` carries a raw string in `input`; `function_call` a
    # JSON string in `arguments`. Both are shown verbatim -- pretty-printing
    # arguments we do not know the shape of would only obscure them.
    raw = payload.get("input")
    if isinstance(raw, str) and raw:
        return raw
    arguments = payload.get("arguments")
    if isinstance(arguments, str) and arguments:
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return arguments
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    return ""


def _iso(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def parse_rollout(path: Path) -> ParsedRollout | None:
    """Reduce one rollout file to a storable conversation, or ``None``.

    ``None`` means the file held no user-visible exchange -- an aborted
    session that only ever wrote its metadata, which there is no point in
    importing as an empty chat.
    """
    now = datetime.now(tz=timezone.utc).isoformat()
    source_id = path.stem
    created_at = now
    workspace = ""
    model = ""
    context_tokens = 0
    total_tokens = 0
    dropped_reasoning = 0

    items: list[dict[str, Any]] = []
    timestamps: list[str] = []
    # A tool call and its output arrive as two separate records, so the call
    # waits here until its output shows up (or the session ends without one).
    pending_calls: dict[str, str] = {}

    def emit(role: str, text: str, at: str) -> None:
        if not text.strip():
            return
        items.append(
            {
                "type": "message",
                "role": role,
                "content": [
                    {"type": "input_text" if role == "user" else "output_text", "text": text}
                ],
            }
        )
        timestamps.append(at)

    for envelope in _lines(path):
        kind = envelope.get("type")
        payload = envelope.get("payload")
        at = _iso(envelope.get("timestamp"), now)
        if not isinstance(payload, dict):
            continue

        if kind == "session_meta":
            created_at = _iso(payload.get("timestamp"), created_at)
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                workspace = cwd
            continue

        if kind == "turn_context":
            # Later turns win: a session can switch model mid-conversation,
            # and the last one used is the one to continue with.
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate:
                model = candidate
            continue

        if kind == "event_msg":
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            if isinstance(total, dict) and isinstance(total.get("total_tokens"), int):
                total_tokens = total["total_tokens"]
            if isinstance(last, dict):
                # Same projection the agent loop uses: the next request's input
                # is this turn's input plus its output.
                inputs = last.get("input_tokens")
                outputs = last.get("output_tokens")
                if isinstance(inputs, int) and isinstance(outputs, int):
                    context_tokens = inputs + outputs
            continue

        if kind != "response_item":
            continue

        item_type = payload.get("type")

        if item_type == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                # `developer` is Codex's own system prompt. Importing it would
                # give the model two sets of instructions.
                continue
            emit(role, _text_from_content(payload.get("content")), at)
            continue

        if item_type == "reasoning":
            dropped_reasoning += 1
            continue

        if item_type in ("function_call", "custom_tool_call"):
            name = payload.get("name")
            call_id = payload.get("call_id") or payload.get("id")
            invocation = _tool_invocation(payload)
            if isinstance(call_id, str):
                label = name if isinstance(name, str) and name else "tool"
                pending_calls[call_id] = f"{label}\n{invocation}".strip()
            continue

        if item_type in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            call = pending_calls.pop(call_id, "") if isinstance(call_id, str) else ""
            output = payload.get("output")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False) if output is not None else ""
            header, _, invocation = call.partition("\n")
            block = f"**{header or 'tool'}**\n\n```\n{_clip(invocation)}\n```"
            if output.strip():
                block += f"\n\n```\n{_clip(output)}\n```"
            emit("assistant", block, at)
            continue

    # A call whose output never arrived still happened, and a session killed
    # mid-command is exactly when that matters.
    for leftover in pending_calls.values():
        header, _, invocation = leftover.partition("\n")
        emit("assistant", f"**{header or 'tool'}**\n\n```\n{_clip(invocation)}\n```", now)

    if not items:
        return None

    return ParsedRollout(
        source_id=source_id,
        path=path,
        title=_derive_title(items),
        workspace=workspace or str(Path.home()),
        model=model,
        created_at=created_at,
        items=items,
        timestamps=timestamps,
        context_tokens=context_tokens,
        total_tokens=total_tokens,
        dropped_reasoning=dropped_reasoning,
    )


def _derive_title(items: list[dict[str, Any]]) -> str:
    """Use the first thing the user said, the way a chat list expects."""
    for item in items:
        if item.get("role") != "user":
            continue
        text = _text_from_content(item.get("content")).strip()
        if not text:
            continue
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first_line:
            continue
        if len(first_line) <= MAX_TITLE_CHARS:
            return first_line
        return f"{first_line[:MAX_TITLE_CHARS - 1].rstrip()}…"
    return "Imported Codex session"


def import_codex_sessions(
    store: Any,
    *,
    codex_home: Path | None = None,
    permission_mode: str = "ask",
    default_model: str = "",
) -> ImportReport:
    """Import every Codex session that is not in the database yet.

    Re-running this is safe: each conversation records the rollout it came
    from, and an already-imported one is counted as skipped rather than
    duplicated.
    """
    report = ImportReport()
    known = store.imported_source_ids()

    for path in discover_rollouts(codex_home):
        if path.stem in known:
            report.skipped += 1
            continue
        try:
            parsed = parse_rollout(path)
        except OSError as error:
            report.failed += 1
            report.errors.append(f"{path.name}: {error}")
            logger.warning("Could not read Codex rollout %s: %s", path, error)
            continue
        if parsed is None:
            report.empty += 1
            continue
        try:
            store.import_conversation(
                source_id=parsed.source_id,
                title=parsed.title,
                workspace=parsed.workspace,
                model=parsed.model or default_model,
                permission_mode=permission_mode,
                created_at=parsed.created_at,
                items=parsed.items,
                timestamps=parsed.timestamps,
                context_tokens=parsed.context_tokens,
                total_tokens=parsed.total_tokens,
            )
        except Exception as error:  # noqa: BLE001 - one bad session, not a failed run
            report.failed += 1
            report.errors.append(f"{path.name}: {error}")
            logger.warning("Could not import Codex rollout %s: %s", path, error)
            continue
        known.add(parsed.source_id)
        report.imported += 1
        report.titles.append(parsed.title)

    return report


def preview_codex_sessions(store: Any, codex_home: Path | None = None) -> dict[str, Any]:
    """Count what an import would do, without touching the database."""
    root = codex_home or default_codex_home()
    paths = discover_rollouts(root)
    known = store.imported_source_ids()
    new = [p for p in paths if p.stem not in known]
    return {
        "available": len(paths),
        "new": len(new),
        "already_imported": len(paths) - len(new),
        "codex_home": str(root),
        "found": root.is_dir(),
    }
