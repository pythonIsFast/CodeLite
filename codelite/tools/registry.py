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

"""The set of tools the agent is given, and how to look one up.

Adding a capability means writing one module and adding it to :data:`TOOLS` --
nothing else in the loop needs to change.
"""

from __future__ import annotations

from typing import Any

from .base import Tool
from .files import EDIT_FILE, LIST_DIR, READ_FILE, WRITE_FILE
from .images import GENERATE_IMAGE
from .search import FIND_FILES, GREP
from .shell import SHELL
from .todo import TODO_WRITE

TOOLS: tuple[Tool, ...] = (
    READ_FILE,
    WRITE_FILE,
    EDIT_FILE,
    LIST_DIR,
    GREP,
    FIND_FILES,
    GENERATE_IMAGE,
    SHELL,
    TODO_WRITE,
)

_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def get(name: str) -> Tool | None:
    return _BY_NAME.get(name)


def names() -> list[str]:
    return [tool.name for tool in TOOLS]


def to_responses_tools() -> list[dict[str, Any]]:
    """Render every tool as a Responses-API tool definition."""
    return [tool.to_responses_tool() for tool in TOOLS]
