from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codelite.agent.loop import AgentRunner
from codelite.config import AppConfig
from codelite.db.store import Store
from codelite.permission.manager import PermissionManager
from codelite.permission.modes import Mode
from codelite.project.plugins import LocalExtensionHost
from codelite.tools.context import ToolContext
from codelite.tools.web import _PageText, _SearchResults, _validate_public_url


class FakeSession:
    rate_limits = None

    def context_window(self, _model: str) -> int:
        return 10_000

    def send_responses(self, _body, stream=False):
        self.last_stream = stream
        return {
            "output_text": "Compact working summary.",
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }


class AgentExtensionTests(unittest.TestCase):
    def test_html_search_and_page_text_are_compact(self) -> None:
        search = _SearchResults()
        search.feed(
            '<li class="b_algo"><h2><a href="https://example.com/page">Example result</a></h2>'
            '<div class="b_caption"><p class="b_lineclamp2"> A useful <b>snippet</b>. </p></div></li>'
        )
        self.assertEqual(search.results[0]["title"], "Example result")
        self.assertEqual(search.results[0]["url"], "https://example.com/page")
        self.assertIn("useful", search.results[0]["snippet"])

        page = _PageText()
        page.feed("<h1>Title</h1><script>secret()</script><p>Hello   world</p>")
        self.assertEqual(page.text(), "Title\nHello world")

    def test_web_fetch_rejects_private_addresses(self) -> None:
        with patch(
            "codelite.tools.web.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
        ):
            with self.assertRaisesRegex(Exception, "Private"):
                _validate_public_url("http://example.test")

    def test_local_tools_and_hooks_load_only_when_used(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            directory = workspace / ".codelite" / "tools"
            directory.mkdir(parents=True)
            extension = directory / "decorate.py"
            extension.write_text(
                """\
TOOL = {
    "name": "local_echo",
    "description": "Echo a value.",
    "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False},
}
HOOKS = ["before_tool", "after_tool"]

def before_tool(name, arguments, context):
    return {**arguments, "value": "before:" + arguments.get("value", "")}

def run(arguments, context):
    return arguments["value"]

def after_tool(name, arguments, output, context):
    return "[" + output + "]"
""",
                encoding="utf-8",
            )
            host = LocalExtensionHost(workspace, set())
            self.assertEqual(host.names(), ["local_echo"])
            context = ToolContext(
                workspace=workspace,
                permissions=PermissionManager(Mode.BYPASS, lambda *_: None),
                session=FakeSession(),
                task_prompt="test",
            )
            arguments = host.before_tool("local_echo", {"value": "hello"}, context)
            output = host.get("local_echo").run(arguments, context)  # type: ignore[union-attr]
            self.assertEqual(host.after_tool("local_echo", arguments, output, context), "[before:hello]")

    def test_manual_compaction_preserves_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            workspace = base / "workspace"
            workspace.mkdir()
            store = Store(base / "codelite.db")
            conversation = store.create_conversation(
                str(workspace), "fixture", Mode.BYPASS.value
            )
            items = [
                {"role": "user", "content": [{"type": "input_text", "text": f"message {index}"}]}
                for index in range(6)
            ]
            store.append_items(conversation.id, items)
            events: list[tuple[str, dict]] = []
            runner = AgentRunner(
                session=FakeSession(),
                store=store,
                conversation=conversation,
                permissions=PermissionManager(Mode.BYPASS, lambda *_: None),
                publish=lambda event, data: events.append((event, data)),
                config=AppConfig(data_dir=base, compaction_recent_items=2),
            )

            runner.compact()

            refreshed = store.get_conversation(conversation.id)
            self.assertEqual(store.count_items(conversation.id), 6)
            self.assertEqual(refreshed.compacted_item_count, 4)  # type: ignore[union-attr]
            self.assertEqual(refreshed.compaction_summary, "Compact working summary.")  # type: ignore[union-attr]
            self.assertIn("compacted", [event for event, _ in events])
            self.assertEqual(events[-1][0], "compaction_finished")


if __name__ == "__main__":
    unittest.main()
