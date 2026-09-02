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

"""Tests for the browser tool's dispatch/protocol layer, using a fake window.

None of these open a real pywebview window -- that needs a display and is not
something this suite can verify. What is tested is everything that does not:
the JSON framing, the JS-result parsing, ref lookup failures, and the
permission gating in the tool itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codelite.browser.host import BrowserActionError, BrowserSession, dispatch
from codelite.permission.manager import PermissionDenied, PermissionManager
from codelite.permission.modes import Mode
from codelite.tools.base import ToolError
from codelite.tools.browser import _run_browser
from codelite.tools.context import ToolContext


class FakeEvent:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True

    def wait(self, _timeout: float) -> bool:
        return self.result


class FakeWindow:
    """Stands in for a pywebview window: scripted `evaluate_js` results."""

    def __init__(self) -> None:
        self.loaded_url: str | None = None
        self.scripts: list[str] = []
        self._responses: list[Any] = []

    def queue(self, *responses: Any) -> "FakeWindow":
        self._responses.extend(responses)
        return self

    def load_url(self, url: str) -> None:
        self.loaded_url = url

    def evaluate_js(self, script: str) -> Any:
        self.scripts.append(script)
        return self._responses.pop(0) if self._responses else None


def test_navigate_loads_and_reports_the_url_and_title() -> None:
    window = FakeWindow().queue("complete", "https://example.com/", "Example")
    session = BrowserSession(window)
    result = session.navigate("https://example.com")
    assert window.loaded_url == "https://example.com"
    assert result == {"url": "https://example.com/", "title": "Example"}


def test_navigate_uses_native_loaded_event_before_evaluating() -> None:
    window = FakeWindow().queue("https://example.com/", "Example")
    window.events = type("Events", (), {"loaded": FakeEvent()})()
    session = BrowserSession(window)

    result = session.navigate("https://example.com")

    assert window.events.loaded.cleared is True
    assert result == {"url": "https://example.com/", "title": "Example"}
    assert "document.readyState" not in window.scripts


def test_navigate_timeout_does_not_enter_the_js_bridge() -> None:
    window = FakeWindow()
    window.events = type("Events", (), {"loaded": FakeEvent(False)})()
    session = BrowserSession(window)

    assert session.navigate("https://slow.example", timeout=0) == {
        "url": "https://slow.example",
        "title": "",
    }
    assert window.scripts == []


def test_navigate_back_uses_history_not_load_url() -> None:
    window = FakeWindow().queue(None, "complete", "https://a/", "A")
    session = BrowserSession(window)
    session.navigate("back")
    assert window.loaded_url is None
    assert "history.back()" in window.scripts[0]


def test_navigate_rejects_an_empty_url() -> None:
    session = BrowserSession(FakeWindow())
    try:
        session.navigate("   ")
    except BrowserActionError:
        pass
    else:
        raise AssertionError("expected BrowserActionError")


def test_snapshot_truncates_to_the_requested_budget() -> None:
    elements = [f"[ref_{i}] button \"Button {i}\"" for i in range(20)]
    window = FakeWindow().queue({"url": "u", "title": "t", "elements": elements})
    session = BrowserSession(window)
    result = session.snapshot(max_chars=50)
    assert len(result["elements"]) < 20
    assert "more elements omitted" in result["elements"][-1]


def test_click_raises_when_the_ref_no_longer_exists() -> None:
    window = FakeWindow().queue({"ok": False, "error": "No element with that reference."})
    session = BrowserSession(window)
    try:
        session.click("ref_9")
    except BrowserActionError as error:
        assert "No element" in str(error)
    else:
        raise AssertionError("expected BrowserActionError")


def test_fill_accepts_a_string_encoded_js_result() -> None:
    # Some evaluate_js paths hand back a JSON string instead of a dict --
    # _parse_js_result has to cope with both.
    window = FakeWindow()
    window.queue('{"ok": true}')
    session = BrowserSession(window)
    session.fill("ref_1", "hello")  # must not raise


def test_screenshot_uses_the_injected_backend_and_base64_encodes() -> None:
    session = BrowserSession(FakeWindow(), screenshot_fn=lambda _w: b"\x89PNG-fake")
    result = session.screenshot()
    import base64

    assert base64.b64decode(result["png_base64"]) == b"\x89PNG-fake"


def test_screenshot_without_a_backend_reports_clearly() -> None:
    session = BrowserSession(FakeWindow())
    try:
        session.screenshot()
    except BrowserActionError as error:
        assert "not available" in str(error)
    else:
        raise AssertionError("expected BrowserActionError")


def test_dispatch_never_raises_and_always_answers_the_request_id() -> None:
    window = FakeWindow().queue("complete", "https://x/", "X")
    session = BrowserSession(window)
    response = dispatch(session, {"id": 7, "action": "navigate", "url": "https://x"})
    assert response == {"id": 7, "ok": True, "result": {"url": "https://x/", "title": "X"}}


def test_dispatch_reports_an_unknown_action_instead_of_crashing() -> None:
    response = dispatch(BrowserSession(FakeWindow()), {"id": 3, "action": "teleport"})
    assert response == {"id": 3, "ok": False, "error": "Unknown browser action `teleport`."}


def test_dispatch_turns_an_exception_into_an_error_reply() -> None:
    response = dispatch(BrowserSession(FakeWindow()), {"id": 1, "action": "click", "ref": "x"})
    assert response["ok"] is False
    assert "id" in response and response["id"] == 1


# -- the tool itself: permission gating ---------------------------------------


def _context(tmp_path: Path, mode: Mode) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        permissions=PermissionManager(mode, lambda *_: None),
        session=None,
    )


def test_navigate_is_never_gated(tmp_path: Path) -> None:
    # Reading is treated like web_fetch: no confirmation, in any mode.
    ctx = _context(tmp_path, Mode.ASK)
    try:
        _run_browser({"action": "navigate", "url": "http://x"}, ctx)
    except ToolError as error:
        # No BrowserClient is running in this test environment, so the call
        # itself fails -- what matters is that it got past permissions.
        assert "browser process" in str(error) or "Could not start" in str(error)


def test_navigate_never_asks_permission(tmp_path: Path) -> None:
    """Reading is treated like web_fetch: no gate, in any mode."""

    class DenyingPermissions:
        def require_write(self, *_a: Any, **_k: Any) -> None:
            raise PermissionDenied("should never be asked")

    ctx = ToolContext(workspace=tmp_path, permissions=DenyingPermissions(), session=None)
    try:
        _run_browser({"action": "navigate", "url": "http://x"}, ctx)
    except PermissionDenied:
        raise AssertionError("navigate must not go through the permission gate")
    except ToolError:
        pass  # No browser process is running here; that failure is expected.


def test_click_and_fill_and_evaluate_are_gated_like_a_write(tmp_path: Path) -> None:
    class DenyingPermissions:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def require_write(self, label: str, *_a: Any, **_k: Any) -> None:
            self.asked.append(label)
            raise PermissionDenied("denied for the test")

    for action, extra in (
        ("click", {"ref": "ref_1"}),
        ("fill", {"ref": "ref_1", "value": "x"}),
        ("evaluate", {"script": "1"}),
    ):
        permissions = DenyingPermissions()
        ctx = ToolContext(workspace=tmp_path, permissions=permissions, session=None)
        try:
            _run_browser({"action": action, **extra}, ctx)
        except PermissionDenied:
            assert permissions.asked, f"{action} should have gone through require_write"
        else:
            raise AssertionError(f"{action} should have been denied by the test permissions")


def test_evaluate_requires_a_script(tmp_path: Path) -> None:
    ctx = _context(tmp_path, Mode.BYPASS)
    try:
        _run_browser({"action": "evaluate"}, ctx)
    except ToolError as error:
        assert "script" in str(error)
    else:
        raise AssertionError("expected ToolError")


def test_unknown_action_is_rejected_before_touching_permissions(tmp_path: Path) -> None:
    ctx = _context(tmp_path, Mode.ASK)
    try:
        _run_browser({"action": "levitate"}, ctx)
    except ToolError as error:
        assert "Unknown browser action" in str(error)
    else:
        raise AssertionError("expected ToolError")
