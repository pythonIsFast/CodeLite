from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from codelite.permission.manager import PermissionManager
from codelite.permission.modes import Mode
from codelite.project.context import build_project_context, discover_skills, read_skill
from codelite.tools.code_intelligence import CODE_INTELLIGENCE
from codelite.tools.context import ToolContext
from codelite.tools.extensions import EXTENSIONS
from codelite.tools.memory import PROJECT_MEMORY

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSession:
    pass


class ProjectIntelligenceTests(unittest.TestCase):
    def context(self, workspace: Path, data_dir: Path) -> ToolContext:
        return ToolContext(
            workspace=workspace,
            permissions=PermissionManager(Mode.BYPASS, lambda *_: None),
            session=FakeSession(),
            data_dir=data_dir,
            model="fixture",
        )

    def test_memory_and_lazy_skills(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            data_dir = Path(root) / "data"
            workspace.mkdir()
            context = self.context(workspace, data_dir)
            PROJECT_MEMORY.run(
                {"action": "append", "content": "- Test with `python -m unittest`."},
                context,
            )
            skill_dir = workspace / ".codelite" / "skills" / "review"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\nname: review\ndescription: Review changes carefully.\n---\n\n# Review\nRead the diff.",
                encoding="utf-8",
            )
            prompt = build_project_context(workspace, data_dir)
            self.assertIn("python -m unittest", prompt)
            self.assertIn("review: Review changes carefully.", prompt)
            self.assertEqual(discover_skills(workspace, data_dir)[0].name, "review")
            self.assertIn("Read the diff", read_skill(workspace, data_dir, "review"))

    def test_mcp_stdio_client(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            config_dir = workspace / ".codelite"
            config_dir.mkdir()
            config_dir.joinpath("mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "fixture": {
                                "command": sys.executable,
                                "args": [str(FIXTURES / "fake_mcp_server.py")],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            context = self.context(workspace, Path(root) / "data")
            tools = json.loads(
                EXTENSIONS.run(
                    {"action": "list_mcp_tools", "server": "fixture"}, context
                )
            )
            self.assertEqual(tools[0]["name"], "echo")
            result = json.loads(
                EXTENSIONS.run(
                    {
                        "action": "call_mcp_tool",
                        "server": "fixture",
                        "name": "echo",
                        "arguments": {"value": 7},
                    },
                    context,
                )
            )
            self.assertIn('"value": 7', result["content"][0]["text"])

    def test_lsp_code_intelligence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            data_dir = Path(root) / "data"
            config_dir = workspace / ".codelite"
            config_dir.mkdir(parents=True)
            config_dir.joinpath("lsp.json").write_text(
                json.dumps(
                    {
                        "servers": {
                            "fixture": {
                                "command": sys.executable,
                                "args": [str(FIXTURES / "fake_lsp_server.py")],
                                "extensions": [".fixture"],
                                "languageId": "fixture",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            workspace.joinpath("sample.fixture").write_text("example\n", encoding="utf-8")
            context = self.context(workspace, data_dir)
            diagnostics = json.loads(
                CODE_INTELLIGENCE.run(
                    {"action": "diagnostics", "path": "sample.fixture"}, context
                )
            )
            self.assertEqual(diagnostics["results"][0]["message"], "Fixture warning")
            definition = json.loads(
                CODE_INTELLIGENCE.run(
                    {
                        "action": "definition",
                        "path": "sample.fixture",
                        "line": 1,
                        "character": 2,
                    },
                    context,
                )
            )
            self.assertEqual(definition["results"][0]["range"]["start"]["line"], 1)


if __name__ == "__main__":
    unittest.main()
