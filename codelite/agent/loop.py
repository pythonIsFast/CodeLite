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
#
# The overall loop shape (stream a turn, execute the tool calls it produced,
# feed the results back, repeat until the model stops calling tools) is the
# standard agent pattern; sst/opencode's packages/opencode/src/session/processor.ts
# was read for inspiration on how to structure the streaming/tool-result
# interleaving. This implementation is independent.

"""The agent loop: model turn -> tool calls -> results -> repeat.

History is a flat list of Responses-API items, which is both what the
provider layer wants as ``input`` and what we persist -- so there is no
translation layer between storage, the model call, and the UI.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import AppConfig
from ..db.store import Conversation, Store
from ..permission.manager import PermissionDenied, PermissionManager
from ..provider.session import Session
from ..provider.sse import iterate_server_sent_events
from ..tools import registry
from ..tools.base import ToolError
from ..tools.context import PathOutsideWorkspace, ToolContext
from . import system_prompt

logger = logging.getLogger(__name__)

Publisher = Callable[[str, dict[str, Any]], None]

TITLE_MAX_CHARS = 60


class AgentRunner:
    """Runs one user turn to completion, publishing progress as it goes."""

    def __init__(
        self,
        session: Session,
        store: Store,
        conversation: Conversation,
        permissions: PermissionManager,
        publish: Publisher,
        config: AppConfig,
    ) -> None:
        self._session = session
        self._store = store
        self._conversation = conversation
        self._permissions = permissions
        self._publish = publish
        self._config = config
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Ask the run to stop, and release anything blocked on a permission prompt."""
        self._cancelled.set()
        self._permissions.cancel_pending()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # -- entry point ---------------------------------------------------------

    def run(self, user_text: str) -> None:
        conversation_id = self._conversation.id
        user_item = {
            "role": "user",
            "content": [{"type": "input_text", "text": user_text}],
        }

        # Repair before appending the new message, so the placeholder outputs
        # land next to the calls they answer instead of behind the user's turn.
        items = self._repair_history(self._store.load_items(conversation_id))
        items.append(user_item)
        self._store.append_items(conversation_id, [user_item])
        self._maybe_set_title(user_text)

        context = ToolContext(
            workspace=Path(self._conversation.workspace),
            permissions=self._permissions,
            task_prompt=user_text,
            shell_timeout_seconds=self._config.shell_timeout_seconds,
        )

        try:
            self._loop(items, context)
        except Exception as error:  # noqa: BLE001 - the UI must learn about any failure
            logger.exception("Agent run failed")
            self._publish("error", {"message": str(error)})

    def _loop(self, items: list[dict[str, Any]], context: ToolContext) -> None:
        for step in range(1, self._config.max_agent_steps + 1):
            if self.cancelled:
                self._publish("cancelled", {})
                return

            self._publish("step", {"step": step})
            response = self._request_turn(items)
            if response is None:
                self._publish(
                    "error",
                    {"message": "The model stream ended without a completed response."},
                )
                return

            output_items = [i for i in (response.get("output") or []) if isinstance(i, dict)]
            if output_items:
                items.extend(output_items)
                self._store.append_items(self._conversation.id, output_items)

            calls = [i for i in output_items if i.get("type") == "function_call"]
            if not calls:
                self._publish(
                    "done",
                    {
                        "usage": response.get("usage") or {},
                        "status": response.get("status"),
                        "steps": step,
                    },
                )
                return

            results = [self._execute_call(call, context) for call in calls]
            items.extend(results)
            self._store.append_items(self._conversation.id, results)

            if self.cancelled:
                self._publish("cancelled", {})
                return

        self._publish(
            "error",
            {
                "message": (
                    f"Stopped after {self._config.max_agent_steps} steps without "
                    "finishing. Send another message to continue."
                )
            },
        )

    # -- model turn ------------------------------------------------------------

    def _request_turn(self, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        body = {
            "model": self._conversation.model,
            "instructions": system_prompt.build(
                self._permissions.mode, self._conversation.workspace
            ),
            "input": items,
            "tools": registry.to_responses_tools(),
        }
        chunks = self._session.send_responses(body, stream=True)
        if isinstance(chunks, dict):  # Defensive: stream=True should never buffer.
            return chunks
        return self._consume_stream(chunks)

    def _consume_stream(self, chunks: Iterator[bytes]) -> dict[str, Any] | None:
        """Publish deltas as they arrive; return the completed response object.

        Only a terminal event produces a result. A stream that dies early must
        not be mistaken for a finished turn -- an early ``response.created``
        payload looks like a valid response but has no output, which would
        silently end the run with an empty answer. So the early payload is
        kept only as a carrier for metadata, and a result is returned solely
        when we either saw a terminal event or actually collected output items.
        """
        terminal: dict[str, Any] | None = None
        latest: dict[str, Any] | None = None
        collected: dict[str, dict[str, Any]] = {}

        for event in iterate_server_sent_events(chunks):
            if self.cancelled:
                break
            if not event.data or event.data == "[DONE]":
                continue
            try:
                payload = json.loads(event.data)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue

            event_type = payload.get("type")

            if event_type == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    self._publish("text_delta", {"delta": delta})

            elif event_type in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            ):
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    self._publish("reasoning_delta", {"delta": delta})

            elif event_type == "response.output_item.added":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    self._publish(
                        "tool_pending",
                        {"call_id": item.get("call_id") or item.get("id"), "name": item.get("name")},
                    )

            elif event_type == "response.output_item.done":
                item = payload.get("item")
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    collected[item["id"]] = item

            elif event_type == "error":
                message = payload.get("message") or "The model reported an error."
                self._publish("error", {"message": str(message)})

            elif event_type in (
                "response.completed",
                "response.failed",
                "response.incomplete",
                "response.cancelled",
                "response.canceled",
            ):
                response = payload.get("response")
                if isinstance(response, dict):
                    terminal = response
                break

            else:
                response = payload.get("response")
                if isinstance(response, dict):
                    latest = response

        if terminal is not None:
            if not terminal.get("output") and collected:
                return {**terminal, "output": list(collected.values())}
            return terminal

        # No terminal event. Salvage the turn only if output actually arrived,
        # otherwise report the truncated stream rather than inventing a result.
        if collected:
            return {**(latest or {}), "output": list(collected.values())}
        return None

    # -- tool execution ---------------------------------------------------------

    def _execute_call(
        self, call: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        call_id = call.get("call_id") or call.get("id") or ""
        name = str(call.get("name") or "")
        raw_arguments = call.get("arguments") or "{}"

        self._publish("tool_started", {"call_id": call_id, "name": name, "arguments": raw_arguments})
        output, ok = self._invoke(name, raw_arguments, context)
        self._publish(
            "tool_finished", {"call_id": call_id, "name": name, "output": output, "ok": ok}
        )
        return {"type": "function_call_output", "call_id": call_id, "output": output}

    def _invoke(
        self, name: str, raw_arguments: str, context: ToolContext
    ) -> tuple[str, bool]:
        """Run one tool, turning every failure into text the model can act on."""
        if self.cancelled:
            return "The run was cancelled before this tool could execute.", False

        tool = registry.get(name)
        if tool is None:
            return (
                f"Unknown tool `{name}`. Available tools: {', '.join(registry.names())}.",
                False,
            )

        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
        except ValueError:
            return (
                f"The arguments for `{name}` were not valid JSON. Call it again "
                "with a well-formed argument object.",
                False,
            )
        if not isinstance(arguments, dict):
            return f"The arguments for `{name}` must be a JSON object.", False

        try:
            output = tool.run(arguments, context)
        except PermissionDenied as denied:
            return denied.message, False
        except (ToolError, PathOutsideWorkspace) as error:
            return f"Error: {error}", False
        except Exception as error:  # noqa: BLE001 - never kill the run over one tool
            logger.exception("Tool %s crashed", name)
            return f"Error: `{name}` failed unexpectedly: {error}", False

        return self._clip(output), True

    def _clip(self, output: str) -> str:
        limit = self._config.max_tool_output_chars
        text = output if isinstance(output, str) else str(output)
        if len(text) <= limit:
            return text
        return (
            text[:limit]
            + f"\n... [output truncated, {len(text) - limit} more characters]"
        )

    # -- history repair ---------------------------------------------------------

    def _repair_history(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Give every dangling ``function_call`` a matching output item.

        If a previous run died between issuing a tool call and recording its
        result -- a crash, a cancel, a closed window -- the stored history
        contains a ``function_call`` with no ``function_call_output``. The
        Responses API rejects that input outright, which would leave the
        conversation permanently unusable. So we fill the gap with an explicit
        placeholder and persist it, making the repair permanent rather than
        re-deriving it on every turn.
        """
        answered = {
            item.get("call_id")
            for item in items
            if item.get("type") == "function_call_output"
        }
        orphans = [
            item
            for item in items
            if item.get("type") == "function_call" and item.get("call_id") not in answered
        ]
        if not orphans:
            return items

        logger.info("Repairing %d unanswered tool call(s) in history", len(orphans))
        placeholders = {
            orphan["call_id"]: {
                "type": "function_call_output",
                "call_id": orphan["call_id"],
                "output": (
                    "This tool call never completed -- the previous run was "
                    "interrupted before it produced a result. Run it again if "
                    "you still need it."
                ),
            }
            for orphan in orphans
            if orphan.get("call_id")
        }
        self._store.append_items(self._conversation.id, list(placeholders.values()))

        repaired: list[dict[str, Any]] = []
        for item in items:
            repaired.append(item)
            if item.get("type") == "function_call":
                placeholder = placeholders.get(item.get("call_id"))
                if placeholder is not None:
                    repaired.append(placeholder)
        return repaired

    # -- misc ------------------------------------------------------------------

    def _maybe_set_title(self, user_text: str) -> None:
        """Derive a title from the first message -- no extra model call needed."""
        if self._conversation.title:
            return
        title = " ".join(user_text.split())[:TITLE_MAX_CHARS].strip()
        if not title:
            return
        self._conversation.title = title
        self._store.update_conversation(self._conversation.id, title=title)
        self._publish("title", {"title": title})
