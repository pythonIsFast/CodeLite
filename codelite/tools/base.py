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
# The tool shape (id + description + parameter schema + an execute function
# that receives a context carrying the permission callback) is inspired by
# sst/opencode's packages/opencode/src/tool/tool.ts, reimplemented here as a
# plain dataclass.

"""The tool contract every agent tool implements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .context import ToolContext


class ToolError(Exception):
    """A tool failed in a way the model should see and can recover from.

    Distinct from an unexpected crash: the loop turns this into the tool's
    output text so the model can correct course, instead of aborting the run.
    """


@dataclass(frozen=True)
class Tool:
    """A single capability the agent can invoke."""

    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any], ToolContext], str]

    def to_responses_tool(self) -> dict[str, Any]:
        """Render as a Responses-API tool definition."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    """Small helper so each tool's schema stays a one-liner."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
