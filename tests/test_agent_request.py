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

    def __init__(self, efforts: list[str] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._efforts = efforts if efforts is not None else ["low", "medium", "high"]

    def send_responses(self, body: dict[str, Any], *, stream: bool = False) -> Any:
        self.bodies.append(body)
        # An empty stream: _request_turn returns None and the caller stops.
        return iter(())

    def model_capabilities(self, model: str) -> dict[str, Any]:
        return {"efforts": list(self._efforts), "default_effort": "medium", "fast": True}

    def context_window(self, model: str) -> int:
        return 272_000


def _runner(tmp_path: Path, session: FakeSession, **conversation: Any) -> AgentRunner:
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
        publish=lambda *_: None,
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


def test_granted_tier_is_reported_once(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    store = Store(tmp_path / "db.sqlite")
    created = store.create_conversation(
        workspace=str(tmp_path), model="gpt-5.6-luna", permission_mode="ask", fast_mode=1
    )
    runner = AgentRunner(
        session=FakeSession(),
        store=store,
        conversation=created,
        permissions=PermissionManager(Mode.ASK, lambda *_: None),
        publish=lambda name, data: events.append((name, data)),
        config=AppConfig(data_dir=tmp_path),
    )

    # Codex answers an unentitled Fast request with the default tier and no error.
    runner._report_service_tier({"service_tier": "default"})
    runner._report_service_tier({"service_tier": "default"})
    assert events == [
        ("service_tier", {"requested": "priority", "granted": "default", "fast": False})
    ]

    runner._granted_tier = ""
    events.clear()
    runner._report_service_tier({"service_tier": "priority"})
    assert events[0][1]["fast"] is True
