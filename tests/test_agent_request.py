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

"""What the agent actually puts on the wire.

These assert the request body rather than the outcome of a real call: the
reasoning level and the service tier are single keys that are either present
and correct or silently absent, and a live run cannot tell the difference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codelite.agent.loop import AgentRunner
from codelite.config import AppConfig
from codelite.db.store import Store
from codelite.permission.manager import PermissionManager
from codelite.permission.modes import Mode


class FakeSession:
    """Records the body it was handed and never talks to anything."""

    def __init__(self, efforts: list[str] | None = None, fast: bool = True) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._efforts = efforts if efforts is not None else ["low", "medium", "high"]
        self._fast = fast

    def send_responses(self, body: dict[str, Any], *, stream: bool = False) -> Any:
        self.bodies.append(body)
        # An empty stream: _request_turn returns None and the caller stops.
        return iter(())

    def model_capabilities(self, model: str) -> dict[str, Any]:
        return {"efforts": list(self._efforts), "default_effort": "medium", "fast": self._fast}

    def context_window(self, model: str) -> int:
        return 272_000


def _runner(
    tmp_path: Path,
    session: FakeSession,
    publish: Any = None,
    **conversation: Any,
) -> AgentRunner:
    store = Store(tmp_path / "db.sqlite")
    created = store.create_conversation(
        workspace=str(tmp_path),
        model=conversation.pop("model", "gpt-5.6-luna"),
        permission_mode="ask",
        **conversation,
    )
    return AgentRunner(
        session=session,
        store=store,
        conversation=created,
        permissions=PermissionManager(Mode.ASK, lambda *_: None),
        publish=publish or (lambda *_: None),
        config=AppConfig(data_dir=tmp_path),
    )


def test_no_reasoning_or_tier_is_sent_by_default(tmp_path: Path) -> None:
    session = FakeSession()
    runner = _runner(tmp_path, session)
    runner._request_turn([])
    body = session.bodies[0]
    # Absent, not empty: the transport then fills in the model's own default.
    assert "reasoning" not in body
    assert "service_tier" not in body


def test_user_message_is_published_with_its_client_id(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runner = _runner(tmp_path, FakeSession(), lambda event, data: events.append((event, data)))

    runner.run("hello", message_id="browser-message-1")

    assert events[0] == (
        "user_message",
        {"text": "hello", "attachments": [], "message_id": "browser-message-1"},
    )
    entry = runner._store.load_entries(runner._conversation.id)[0]
    assert entry["meta"]["message_id"] == "browser-message-1"


def test_chosen_effort_is_sent(tmp_path: Path) -> None:
    session = FakeSession()
    runner = _runner(tmp_path, session, reasoning_effort="high")
    runner._request_turn([])
    assert session.bodies[0]["reasoning"] == {"effort": "high"}


def test_fast_mode_requests_the_priority_tier(tmp_path: Path) -> None:
    session = FakeSession()
    runner = _runner(tmp_path, session, fast_mode=1)
    runner._request_turn([])
    assert session.bodies[0]["service_tier"] == "priority"


def test_an_unsupported_effort_is_dropped_not_sent(tmp_path: Path) -> None:
    # Luna has no "ultra". Sending it anyway is an HTTP 400 on the first turn,
    # so the level has to fall back to the model's own default instead.
    session = FakeSession(efforts=["low", "medium", "high", "xhigh", "max"])
    runner = _runner(tmp_path, session, reasoning_effort="ultra")
    runner._run_effort = runner._supported_effort(runner._run_effort)
    runner._request_turn([])
    assert "reasoning" not in session.bodies[0]


def test_a_supported_effort_survives_the_clamp(tmp_path: Path) -> None:
    session = FakeSession(efforts=["low", "medium", "high", "xhigh", "max", "ultra"])
    runner = _runner(tmp_path, session, reasoning_effort="ultra")
    runner._run_effort = runner._supported_effort(runner._run_effort)
    runner._request_turn([])
    assert session.bodies[0]["reasoning"] == {"effort": "ultra"}


def test_fast_is_omitted_for_a_model_without_the_tier() -> None:
    """gpt-5.4-mini has no fast tier, and the backend does not object.

    Sending `priority` to it returns 200 and the response still reads
    `service_tier: "default"` -- exactly what a supported model returns too.
    Nothing downstream can tell the difference, so the request has to be
    filtered here rather than relying on the server to refuse it.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        session = FakeSession(fast=False)
        runner = _runner(Path(tmp), session, fast_mode=1)
        runner._request_turn([])
        assert "service_tier" not in session.bodies[0]
