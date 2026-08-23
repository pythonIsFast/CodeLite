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

"""Wires the pieces together and owns the per-conversation live state.

Everything persistent lives in SQLite; what lives here is the state that only
makes sense while the app is running: which run is in flight, who is waiting
on a permission answer, and which UI streams to push events to.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..agent.judge import make_shell_judge
from ..agent.loop import AgentRunner
from .. import settings as behaviour
from ..config import AppConfig, normalize_effort
from ..db.store import Conversation, Store
from ..permission.manager import PermissionManager
from ..permission.modes import Mode
from ..provider.config import ProviderConfig
from ..provider.session import Session
from ..questions import QuestionManager

logger = logging.getLogger(__name__)

#: How long an idle SSE stream waits before emitting a keepalive comment.
KEEPALIVE_SECONDS = 15.0

#: Where the last plan-usage snapshot is cached in the store.
PLAN_USAGE_KEY = "plan_usage"
BEHAVIOUR_KEY = "behaviour"


class RunInProgress(Exception):
    """Raised when a second message arrives while a run is still going."""


@dataclass
class ConversationRuntime:
    """Live state for one conversation."""

    permissions: PermissionManager
    questions: QuestionManager
    subscribers: list[queue.Queue] = field(default_factory=list)
    runner: AgentRunner | None = None
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def busy(self) -> bool:
        thread = self.thread
        return thread is not None and thread.is_alive()


class Runtime:
    """The app's single entry point for everything the HTTP layer needs."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self.store = Store(self.config.db_path)
        self.session = Session(
            ProviderConfig(auth_file_path=self.config.data_dir / "auth.json")
        )
        self._conversations: dict[str, ConversationRuntime] = {}
        self._lock = threading.Lock()
        self._apply_behaviour(self.store.get_state(BEHAVIOUR_KEY) or {})

    # -- behaviour settings --------------------------------------------------

    def _apply_behaviour(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Fold stored settings onto the config, clamped by the schema.

        Applied at startup and after every save, and mirrored into the
        `settings` module for the LSP/MCP singletons that cannot reach the
        config. Anything the schema rejects falls back to its default, so a
        hand-edited database cannot produce an unrunnable app.
        """
        values = behaviour.coerce(raw if isinstance(raw, dict) else {})
        for key, value in values.items():
            if hasattr(self.config, key):
                setattr(self.config, key, tuple(value) if isinstance(value, list) else value)
        behaviour.apply(values)
        return values

    def behaviour_settings(self) -> dict[str, Any]:
        return behaviour.coerce(self.store.get_state(BEHAVIOUR_KEY) or {})

    def save_behaviour_settings(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist and activate. Raises ValueError on a bad type."""
        try:
            models = self.session.list_models()
        except Exception:  # noqa: BLE001 - a catalog outage must not block a save
            models = []
        values = behaviour.coerce(raw, models)
        self.store.set_state(BEHAVIOUR_KEY, values)
        return self._apply_behaviour(values)

    # -- conversations -------------------------------------------------------

    def create_conversation(
        self,
        workspace: str | None = None,
        model: str | None = None,
        mode: Mode | None = None,
        reasoning_effort: str | None = None,
        fast_mode: bool = False,
    ) -> Conversation:
        resolved_workspace = Path(workspace).expanduser().resolve() if workspace else Path.cwd()
        if not resolved_workspace.is_dir():
            raise ValueError(f"{resolved_workspace} is not a directory.")
        return self.store.create_conversation(
            workspace=str(resolved_workspace),
            model=model or self.config.agent_model,
            permission_mode=(mode or self.config.default_permission_mode).value,
            reasoning_effort=normalize_effort(
                reasoning_effort
                if reasoning_effort is not None
                else self.config.default_reasoning_effort
            ),
            fast_mode=1 if fast_mode else 0,
        )

    def set_mode(self, conversation: Conversation, mode: Mode) -> None:
        """Change the mode for future tool calls, and persist it."""
        self.store.update_conversation(conversation.id, permission_mode=mode.value)
        conversation.permission_mode = mode.value
        state = self._conversations.get(conversation.id)
        if state is not None:
            state.permissions.set_mode(mode)

    def delete_conversation(self, conversation_id: str) -> None:
        self.cancel_run(conversation_id)
        self.store.delete_conversation(conversation_id)
        with self._lock:
            self._conversations.pop(conversation_id, None)

    # -- live state -------------------------------------------------------------

    def _state_for(self, conversation: Conversation) -> ConversationRuntime:
        with self._lock:
            state = self._conversations.get(conversation.id)
            if state is None:
                mode = Mode.parse(
                    conversation.permission_mode, self.config.default_permission_mode
                )
                state = ConversationRuntime(
                    permissions=PermissionManager(
                        mode=mode,
                        publish=lambda event, data, cid=conversation.id: self.publish(
                            cid, event, data
                        ),
                        judge=make_shell_judge(self.session, self.config.judge_model),
                    ),
                    questions=QuestionManager(
                        publish=lambda event, data, cid=conversation.id: self.publish(
                            cid, event, data
                        )
                    ),
                )
                self._conversations[conversation.id] = state
            return state

    def pending_permissions(self, conversation: Conversation) -> list[dict[str, Any]]:
        return self._state_for(conversation).permissions.list_pending()

    def reply_permission(
        self, conversation: Conversation, request_id: str, reply: str, feedback: str = ""
    ) -> bool:
        state = self._state_for(conversation)
        if reply not in ("once", "session", "deny"):
            return False
        return state.permissions.reply(request_id, reply, feedback)  # type: ignore[arg-type]

    def reply_question(self, conversation: Conversation, question_id: str, answer: str) -> bool:
        return self._state_for(conversation).questions.reply(question_id, answer)

    # -- events ---------------------------------------------------------------------

    def publish(self, conversation_id: str, event: str, data: dict[str, Any]) -> None:
        """Fan an event out to every UI stream watching this conversation."""
        with self._lock:
            state = self._conversations.get(conversation_id)
            subscribers = list(state.subscribers) if state else []
        message = _format_sse(event, data)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:  # pragma: no cover - unbounded queues in practice
                logger.warning("Dropping event for a full subscriber queue")

    def subscribe(self, conversation: Conversation) -> Iterator[str]:
        """Yield SSE frames for this conversation until the client disconnects."""
        state = self._state_for(conversation)
        stream: queue.Queue = queue.Queue()
        with self._lock:
            state.subscribers.append(stream)
        try:
            yield _format_sse("ready", {"busy": state.busy})
            # Re-announce anything already waiting, so a reload does not lose
            # a permission prompt the agent is still blocked on.
            for pending in state.permissions.list_pending():
                yield _format_sse("permission_request", pending)
            for pending in state.questions.list_pending():
                yield _format_sse("question_request", pending)
            while True:
                try:
                    yield stream.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with self._lock:
                if stream in state.subscribers:
                    state.subscribers.remove(stream)

    # -- runs ------------------------------------------------------------------------

    def start_run(
        self,
        conversation: Conversation,
        user_text: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> None:
        state = self._state_for(conversation)
        with state.lock:
            if state.busy:
                raise RunInProgress("A run is already in progress in this conversation.")

            runner = AgentRunner(
                session=self.session,
                store=self.store,
                conversation=conversation,
                permissions=state.permissions,
                questions=state.questions,
                publish=lambda event, data, cid=conversation.id: self.publish(
                    cid, event, data
                ),
                config=self.config,
            )
            state.runner = runner
            thread = threading.Thread(
                target=self._run_and_report,
                args=(runner, user_text, attachments, conversation.id),
                name=f"codelite-run-{conversation.id[:8]}",
                daemon=True,
            )
            state.thread = thread
            thread.start()

    def start_compaction(self, conversation: Conversation) -> None:
        """Compact one conversation in the background like a normal run."""
        state = self._state_for(conversation)
        with state.lock:
            if state.busy:
                raise RunInProgress("A run is already in progress in this conversation.")
            runner = AgentRunner(
                session=self.session,
                store=self.store,
                conversation=conversation,
                permissions=state.permissions,
                questions=state.questions,
                publish=lambda event, data, cid=conversation.id: self.publish(
                    cid, event, data
                ),
                config=self.config,
            )
            state.runner = runner
            thread = threading.Thread(
                target=self._compact_and_report,
                args=(runner, conversation.id),
                name=f"codelite-compact-{conversation.id[:8]}",
                daemon=True,
            )
            state.thread = thread
            thread.start()

    def _run_and_report(
        self,
        runner: AgentRunner,
        user_text: str,
        attachments: list[dict[str, str]] | None,
        cid: str,
    ) -> None:
        """Run a turn, then publish the plan usage the request just revealed."""
        try:
            runner.run(user_text, attachments)
        finally:
            self.refresh_plan_usage(publish_to=cid)

    def _compact_and_report(self, runner: AgentRunner, cid: str) -> None:
        try:
            runner.compact()
        finally:
            self.refresh_plan_usage(publish_to=cid)

    # -- plan usage ------------------------------------------------------------

    def refresh_plan_usage(self, publish_to: str | None = None) -> dict[str, Any] | None:
        """Persist the newest plan-usage snapshot and optionally announce it.

        Codex only reports this in the headers of a real request, so the
        snapshot is written to the database to survive a restart -- otherwise
        the indicator would sit empty until the user happened to send a
        message.
        """
        limits = self.session.rate_limits
        if limits is None:
            return None
        payload = limits.as_dict()
        try:
            self.store.set_state(PLAN_USAGE_KEY, payload)
        except Exception:  # noqa: BLE001 - a telemetry write must not break a run
            logger.warning("Could not persist plan usage", exc_info=True)
        if publish_to:
            self.publish(publish_to, "plan_usage", payload)
        return payload

    def plan_usage(self) -> dict[str, Any] | None:
        """Latest known plan usage: live if we have it, else the stored copy."""
        limits = self.session.rate_limits
        if limits is not None:
            return limits.as_dict()
        return self.store.get_state(PLAN_USAGE_KEY)

    def cancel_run(self, conversation_id: str) -> bool:
        with self._lock:
            state = self._conversations.get(conversation_id)
        if state is None or state.runner is None or not state.busy:
            return False
        state.runner.cancel()
        return True

    def is_busy(self, conversation: Conversation) -> bool:
        return self._state_for(conversation).busy


def _format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
