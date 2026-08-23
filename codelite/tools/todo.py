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

"""The todo tool: the agent's own plan, visible to the user.

On a long task the model tends to lose the thread -- it finishes step three
and forgets steps four and five ever existed. Writing the plan down and
re-reading it each turn fixes that, and it doubles as the only honest
progress indicator the UI can show: not a spinner, but what the agent
actually thinks is left to do.

The list is replaced wholesale on every call rather than patched item by
item. That is deliberate: a diff-based API invites the model to send
malformed partial updates, and the whole list is small enough that resending
it costs nothing.
"""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext

STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 30
MAX_CONTENT_CHARS = 200

_GLYPH = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def _parse_items(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ToolError("`todos` must be a list of {content, status} objects.")
    if len(raw) > MAX_ITEMS:
        raise ToolError(f"Too many todos ({len(raw)}); keep the plan under {MAX_ITEMS} items.")

    items: list[dict[str, str]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ToolError(f"Todo #{index + 1} must be an object with `content` and `status`.")
        content = str(entry.get("content") or "").strip()
        if not content:
            raise ToolError(f"Todo #{index + 1} has no `content`.")
        status = str(entry.get("status") or "pending").strip()
        if status not in STATUSES:
            raise ToolError(
                f"Todo #{index + 1} has status '{status}'; use one of {', '.join(STATUSES)}."
            )
        items.append({"content": content[:MAX_CONTENT_CHARS], "status": status})

    active = [i for i in items if i["status"] == "in_progress"]
    if len(active) > 1:
        raise ToolError(
            "Only one todo may be `in_progress` at a time -- mark the others "
            "`pending` or `completed`."
        )
    return items


def _run_todo_write(args: dict[str, Any], ctx: ToolContext) -> str:
    items = _parse_items(args.get("todos"))
    ctx.todos = items
    ctx.emit("todos", {"todos": items})

    if not items:
        return "Todo list cleared."

    done = sum(1 for i in items if i["status"] == "completed")
    lines = [f"{_GLYPH[i['status']]} {i['content']}" for i in items]
    return f"Plan updated ({done}/{len(items)} done):\n" + "\n".join(lines)


TODO_WRITE = Tool(
    name="todo_write",
    description=(
        "Record or update your plan for the current task. Send the complete "
        "list every time -- it replaces the previous one. Use this for any "
        "task with more than about three steps: write the plan first, then "
        "mark exactly one item `in_progress` as you work and flip it to "
        "`completed` before starting the next. The user sees this list, so it "
        "is also how you show progress."
    ),
    parameters=object_schema(
        {
            "todos": {
                "type": "array",
                "description": "The full plan, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "What needs doing, as a short imperative phrase.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(STATUSES),
                            "description": "One item may be in_progress at a time.",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        required=["todos"],
    ),
    run=_run_todo_write,
)
