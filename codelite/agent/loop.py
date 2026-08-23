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

from ..config import AppConfig, context_window_for
from ..db.store import Conversation, Store
from ..permission.manager import PermissionDenied, PermissionManager
from ..provider.session import Session
from ..provider.sse import iterate_server_sent_events
from ..project.context import build_project_context
from ..tools import registry
from ..tools.base import ToolError
from ..tools.context import PathOutsideWorkspace, ToolContext
from . import system_prompt

logger = logging.getLogger(__name__)

Publisher = Callable[[str, dict[str, Any]], None]

TITLE_MAX_CHARS = 60

COMPACTION_INSTRUCTIONS = """\
Create concise working memory for a coding agent from the conversation items
below. Preserve the user's goal, relevant files and their current state,
decisions, commands and results, unfinished work, errors, and next steps.
Do not include filler or repeat large file contents. This summary replaces
older history, so make it self-contained. Return only the summary text."""


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
        self._run_tokens_used = 0
        self._project_context = ""

    def cancel(self) -> None:
        """Ask the run to stop, and release anything blocked on a permission prompt."""
        self._cancelled.set()
        self._permissions.cancel_pending()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # -- entry point ---------------------------------------------------------

    def run(self, user_text: str, attachments: list[dict[str, str]] | None = None) -> None:
        self._run_tokens_used = 0
        # Discover repository instructions once before the first model turn.
        # A run may contain many tool turns, so rebuilding this bounded context
        # per turn only adds latency and token-accounting work.
        self._project_context = build_project_context(
            Path(self._conversation.workspace), self._config.data_dir
        )
        conversation_id = self._conversation.id
        user_item = {
            "role": "user",
            "content": [{"type": "input_text", "text": user_text}],
        }

        # Repair before appending the new message, so the placeholder outputs
        # land next to the calls they answer instead of behind the user's turn.
        history = self._repair_history(self._store.load_items(conversation_id))
        items = self._effective_history(history)
        items.append(user_item)
        self._store.append_items(
            conversation_id,
            [user_item],
            {"attachments": attachments} if attachments else None,
        )
        self._maybe_set_title(user_text)

        context = ToolContext(
            workspace=Path(self._conversation.workspace),
            permissions=self._permissions,
            session=self._session,
            data_dir=self._config.data_dir,
            model=self._conversation.model,
            task_prompt=user_text,
            shell_timeout_seconds=self._config.shell_timeout_seconds,
            publish=self._publish,
        )

        try:
            self._loop(items, context)
        except Exception as error:  # noqa: BLE001 - the UI must learn about any failure
            logger.exception("Agent run failed")
            self._publish("error", {"message": str(error)})

    def _loop(self, items: list[dict[str, Any]], context: ToolContext) -> None:
        """Run turns until the model stops calling tools.

        There is no step ceiling on purpose -- a task needs however many turns
        it needs, and cutting it off at an arbitrary number just abandons work
        midway. What genuinely bounds a run is the context window, so that is
        what we check. If the upstream reports no usage at all we cannot
        measure it, and the run is instead bounded by the API rejecting an
        over-long request, which surfaces through the normal error path.
        """
        step = 0
        # Direct image inputs from `view_image` stay in memory only until the
        # next turn consumes them. They must never be persisted or replayed.
        ephemeral_inputs: list[dict[str, Any]] = []
        while True:
            step += 1
            if self.cancelled:
                self._publish("cancelled", {})
                return

            compacted = self._maybe_compact_history()
            if compacted is not None:
                items = compacted

            exhausted = self._context_exhausted()
            if exhausted is not None:
                self._publish("error", {"message": exhausted})
                return

            self._publish("step", {"step": step})
            response = self._request_turn(items)
            if response is None:
                self._publish(
                    "error",
                    {"message": "The model stream ended without a completed response."},
                )
                return

            if ephemeral_inputs:
                ephemeral_ids = {id(item) for item in ephemeral_inputs}
                items[:] = [item for item in items if id(item) not in ephemeral_ids]
                ephemeral_inputs.clear()

            turn_meta = self._publish_usage(response.get("usage") or {})

            output_items = [i for i in (response.get("output") or []) if isinstance(i, dict)]
            if output_items:
                items.extend(output_items)
                self._store.append_items(self._conversation.id, output_items, turn_meta)

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
            direct_inputs = context.take_model_inputs()
            if direct_inputs:
                items.extend(direct_inputs)
                ephemeral_inputs.extend(direct_inputs)

            if self.cancelled:
                self._publish("cancelled", {})
                return

    def _context_window(self) -> int:
        """This model's context window, preferring Codex's own catalog figure.

        The catalog reports ``context_window`` per model, which beats a table
        we maintain by hand. It is cached in the transport, so this is a dict
        lookup after the first call; the static table only covers the case
        where the catalog cannot be reached.
        """
        try:
            live = self._session.context_window(self._conversation.model)
        except Exception:  # noqa: BLE001 - never fail a run over a display figure
            live = None
        return live or context_window_for(self._conversation.model)

    def _needs_compaction(self) -> bool:
        used = self._conversation.context_tokens
        return bool(
            used
            and used >= self._context_window() * self._config.context_compact_fraction
        )

    def _effective_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the compact working history while preserving the transcript in SQLite."""
        count = max(0, min(self._conversation.compacted_item_count, len(history)))
        tail = history[count:]
        summary = self._conversation.compaction_summary.strip()
        if not summary:
            return tail
        return [
            {
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Working memory from earlier conversation:\n" + summary,
                    }
                ],
            },
            *tail,
        ]

    def _maybe_compact_history(self) -> list[dict[str, Any]] | None:
        """Summarize older history before the input window becomes a hard stop.

        The original items deliberately stay in the database for the UI. Only
        the model's working set is replaced, and the summary is refreshed from
        the previous summary plus the newly accumulated items on later passes.
        """
        if not self._needs_compaction():
            return None

        history = self._store.load_items(self._conversation.id)
        already_compacted = max(
            0, min(self._conversation.compacted_item_count, len(history))
        )
        uncompressed = history[already_compacted:]
        keep = max(1, self._config.compaction_recent_items)
        if len(uncompressed) <= keep:
            return None

        prefix = uncompressed[:-keep]
        summary_input: list[dict[str, Any]] = []
        if self._conversation.compaction_summary.strip():
            summary_input.append(
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Earlier working memory:\n"
                            + self._conversation.compaction_summary,
                        }
                    ],
                }
            )
        summary_input.extend(prefix)

        self._publish(
            "compaction_started",
            {"context_tokens": self._conversation.context_tokens},
        )
        try:
            response = self._session.send_responses(
                {
                    "model": self._conversation.model,
                    "instructions": COMPACTION_INSTRUCTIONS,
                    "input": summary_input,
                },
                stream=False,
            )
            if not isinstance(response, dict):
                raise RuntimeError("The compaction request did not return a response.")
            summary = self._response_text(response)
            if not summary:
                raise RuntimeError("The compaction request returned no summary text.")
            self._publish_usage(response.get("usage") or {})
        except Exception as error:  # noqa: BLE001 - fall back to the hard stop safely
            logger.warning("Could not compact conversation history", exc_info=True)
            self._publish("compaction_failed", {"message": str(error)})
            return None

        compacted_count = len(history) - keep
        self._store.save_compaction(self._conversation.id, summary, compacted_count)
        self._conversation.compaction_summary = summary
        self._conversation.compacted_item_count = compacted_count
        self._conversation.context_tokens = 0
        self._publish(
            "compacted",
            {"compacted_items": compacted_count, "kept_items": keep},
        )
        return self._effective_history(history)

    @staticmethod
    def _response_text(response: dict[str, Any]) -> str:
        """Extract plain text from either common Responses output shape."""
        direct = response.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        parts: list[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text") or block.get("value")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)

    def _context_exhausted(self) -> str | None:
        """Return a message to stop on if the context window is nearly full.

        Returns ``None`` both when there is room left and when we have no
        usage figures to judge by -- guessing would either cut a healthy run
        short or invent a limit that is not there.
        """
        used = self._conversation.context_tokens
        if not used:
            return None
        window = self._context_window()
        if used < window * self._config.context_stop_fraction:
            return None
        return (
            f"Stopping: automatic context compaction could not free enough "
            f"space after this conversation reached {used:,} of about "
            f"{window:,} context tokens. Start a new chat to continue."
        )

    def _publish_usage(self, usage: dict[str, Any]) -> dict[str, Any]:
        """Report this turn's token cost, and persist it against the conversation.

        ``context_window`` is an *input* budget (272000 = 400000 total less the
        128000 reserved for output). We resend the whole history every turn, so
        the next request's input is this turn's input plus this turn's output --
        which is why those two are summed here. It is a projection of the next
        call, not a measure of the last one, and that is the number worth
        showing: it answers "will the next turn still fit".

        Stores the model's output-only count with its response items. The UI
        uses that for the small counter beneath an assistant message; the
        total is still tracked separately for conversation accounting.
        """
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        turn_total = usage.get("total_tokens")
        if not isinstance(turn_total, int):
            if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
                return {}
            turn_total = input_tokens + output_tokens

        self._run_tokens_used += turn_total
        context_tokens = (
            input_tokens + output_tokens
            if isinstance(input_tokens, int) and isinstance(output_tokens, int)
            else None
        )
        window = self._context_window()

        if context_tokens is not None:
            self._conversation.context_tokens = context_tokens
        self._conversation.total_tokens += turn_total
        self._store.record_usage(
            self._conversation.id, self._conversation.context_tokens, turn_total
        )

        self._publish(
            "usage",
            {
                "run_tokens": self._run_tokens_used,
                "turn_tokens": turn_total,
                "output_tokens": output_tokens,
                "total_tokens": self._conversation.total_tokens,
                "context_tokens": context_tokens,
                "context_window": window,
            },
        )
        meta = {"context_tokens": context_tokens}
        if isinstance(output_tokens, int) and output_tokens > 0:
            meta["output_tokens"] = output_tokens
        return meta

    # -- model turn ------------------------------------------------------------

    def _request_turn(self, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        body = {
            "model": self._conversation.model,
            "instructions": system_prompt.build(
                self._permissions.mode,
                self._conversation.workspace,
                self._project_context,
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
