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
# The request/reply correlation approach (a pending request keyed by id that
# blocks the calling tool until a reply arrives) is inspired by sst/opencode's
# packages/opencode/src/permission/index.ts. The implementation here is
# independent: threads and Events instead of Effect-TS Deferreds, and a
# four-mode policy instead of a wildcard ruleset.

"""Decides whether an agent tool call may proceed.

One :class:`PermissionManager` exists per conversation. Tools call
:meth:`require_write` / :meth:`require_shell` right before doing something
consequential; depending on the conversation's :class:`~codelite.permission.modes.Mode`
that either returns immediately, blocks until the user answers in the UI, or
(in ``auto`` mode) asks a judge model first.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from .modes import Mode

Kind = Literal["write", "shell"]
Reply = Literal["once", "session", "deny"]

#: A judge callback: given (command, task_prompt) it returns
#: ``(allowed, reason)``. Kept as a plain tuple so this module needs no
#: dependency on the agent/provider layers.
ShellJudge = Callable[[str, str], "tuple[bool, str]"]

#: Publishes an event to the conversation's UI stream.
Publisher = Callable[[str, dict[str, Any]], None]


class PermissionDenied(Exception):
    """Raised inside a tool when its action was not permitted.

    The agent loop turns this into the tool's output, so the model learns
    *why* it was blocked and can react, rather than the run just dying.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    #: Where the decision came from -- useful for the UI and for logs.
    source: Literal["mode", "judge", "user", "session"] = "mode"


@dataclass
class PendingRequest:
    """A permission question currently waiting on the user."""

    id: str
    kind: Kind
    detail: str
    #: Set when a judge model denied the action and we escalated to the user.
    judge_reason: str = ""
    #: Unified diff of the pending change, for write requests.
    diff: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _reply: Reply | None = field(default=None, repr=False)
    _feedback: str = field(default="", repr=False)

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "detail": self.detail,
            "judge_reason": self.judge_reason,
            "diff": self.diff,
            "created_at": self.created_at,
        }


class PermissionManager:
    """Per-conversation permission gate."""

    def __init__(
        self,
        mode: Mode,
        publish: Publisher,
        judge: ShellJudge | None = None,
    ) -> None:
        self._mode = mode
        self._publish = publish
        self._judge = judge
        self._lock = threading.Lock()
        self._pending: dict[str, PendingRequest] = {}
        #: Categories the user granted for the rest of this conversation.
        self._session_grants: set[Kind] = set()

    # -- mode ---------------------------------------------------------------

    @property
    def mode(self) -> Mode:
        with self._lock:
            return self._mode

    def set_mode(self, mode: Mode) -> None:
        with self._lock:
            self._mode = mode

    # -- gates ---------------------------------------------------------------

    def require_write(self, path: str, diff: str = "") -> None:
        """Gate a file write. Raises :class:`PermissionDenied` if refused.

        ``diff`` is shown to the user so they approve a change rather than a
        bare filename -- without it the dialog asks them to trust a path.
        """
        decision = self._decide_write(path, diff)
        if not decision.allowed:
            raise PermissionDenied(
                decision.reason or f"Permission to write {path} was denied by the user."
            )

    def require_shell(self, command: str, task_prompt: str = "") -> None:
        """Gate a shell command. Raises :class:`PermissionDenied` if refused."""
        decision = self._decide_shell(command, task_prompt)
        if not decision.allowed:
            raise PermissionDenied(
                decision.reason
                or f"Permission to run `{command}` was denied by the user."
            )

    def _decide_write(self, path: str, diff: str = "") -> Decision:
        mode = self.mode
        if not mode.writes_need_approval:
            return Decision(allowed=True, reason=f"{mode.label} mode", source="mode")
        if self._has_session_grant("write"):
            return Decision(allowed=True, reason="Approved for this session", source="session")
        return self._ask_user("write", path, diff=diff)

    def _decide_shell(self, command: str, task_prompt: str) -> Decision:
        mode = self.mode
        if mode is Mode.BYPASS:
            return Decision(allowed=True, reason="Bypass mode", source="mode")
        if self._has_session_grant("shell"):
            return Decision(allowed=True, reason="Approved for this session", source="session")

        if mode is Mode.AUTO:
            allowed, reason = self._run_judge(command, task_prompt)
            if allowed:
                return Decision(allowed=True, reason=reason, source="judge")
            # The judge said no. That is not a silent abort: escalate to the
            # user right away, carrying the judge's reasoning so they can see
            # what the agent was told and decide for themselves.
            return self._ask_user("shell", command, judge_reason=reason)

        return self._ask_user("shell", command)

    def _run_judge(self, command: str, task_prompt: str) -> tuple[bool, str]:
        if self._judge is None:
            return False, (
                "Auto mode is active but no judge model is available, so this "
                "shell command could not be evaluated automatically."
            )
        self._publish("judge_started", {"command": command})
        try:
            allowed, reason = self._judge(command, task_prompt)
        except Exception as error:  # noqa: BLE001 - judge failure must not allow by default
            allowed, reason = False, f"The judge model could not be reached ({error})."
        self._publish("judge_finished", {"command": command, "allowed": allowed, "reason": reason})
        return allowed, reason

    # -- user round-trip -------------------------------------------------------

    def _ask_user(
        self, kind: Kind, detail: str, judge_reason: str = "", diff: str = ""
    ) -> Decision:
        request = PendingRequest(
            id=uuid.uuid4().hex,
            kind=kind,
            detail=detail,
            judge_reason=judge_reason,
            diff=diff,
        )
        with self._lock:
            self._pending[request.id] = request
        self._publish("permission_request", request.as_event_payload())

        request._event.wait()

        with self._lock:
            self._pending.pop(request.id, None)
            reply, feedback = request._reply, request._feedback
            if reply == "session":
                self._session_grants.add(kind)

        if reply in ("once", "session"):
            return Decision(
                allowed=True,
                reason="Approved by the user",
                source="session" if reply == "session" else "user",
            )

        # Denied (or cancelled). Prefer the user's own words, then the judge's.
        parts = [p for p in (feedback.strip(), judge_reason.strip()) if p]
        detail_text = " ".join(parts) if parts else "No reason given."
        noun = "write to" if kind == "write" else "run"
        return Decision(
            allowed=False,
            reason=f"The user did not allow you to {noun} `{detail}`. {detail_text}",
            source="user",
        )

    def reply(self, request_id: str, reply: Reply, feedback: str = "") -> bool:
        """Resolve a pending request, unblocking the waiting tool thread."""
        with self._lock:
            request = self._pending.get(request_id)
            if request is None:
                return False
            request._reply = reply
            request._feedback = feedback
        request._event.set()
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.as_event_payload() for r in self._pending.values()]

    def cancel_pending(self) -> None:
        """Deny everything still waiting -- used when a run is aborted."""
        with self._lock:
            requests = list(self._pending.values())
        for request in requests:
            if request._reply is None:
                request._reply = "deny"
                request._feedback = "The run was cancelled."
            request._event.set()

    def _has_session_grant(self, kind: Kind) -> bool:
        with self._lock:
            return kind in self._session_grants
