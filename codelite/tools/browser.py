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

"""A hidden, scriptable browser for pages `web_search`/`web_fetch` cannot
read -- ones that render their content with JavaScript. See
:mod:`codelite.browser` for how the window itself works.

Reading the page (navigating, taking a snapshot, a screenshot) is treated
like `web_fetch`: no prompt. Anything that acts on the page -- clicking,
typing, running arbitrary JavaScript -- goes through the same permission gate
as a file write, because it can submit a form, follow a link into a purchase
flow, or otherwise change state on a real site exactly as a write changes
state on disk.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from ..browser.client import BrowserError, get_client
from .base import Tool, ToolError, object_schema
from .context import ToolContext

_ACTIONS = ("navigate", "snapshot", "click", "fill", "evaluate", "screenshot", "reset")

#: Acting on a page is gated like a write; only reading is not.
_GATED_ACTIONS = frozenset({"click", "fill", "evaluate"})

DEFAULT_MAX_CHARS = 20_000


def _require_ref(args: dict[str, Any]) -> str:
    ref = args.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ToolError("`ref` is required. Call `snapshot` first to get element references.")
    return ref.strip()


def _permission_label(action: str, args: dict[str, Any]) -> str:
    if action == "click":
        return f"browser: click {args.get('ref', '?')}"
    if action == "fill":
        return f"browser: type into {args.get('ref', '?')}"
    return "browser: run JavaScript on the current page"


def _format_snapshot(result: dict[str, Any]) -> str:
    header = f"{result.get('title', '')} — {result.get('url', '')}".strip(" —")
    lines = result.get("elements") or []
    if not lines:
        return f"{header}\n(no interactive elements found)"
    return f"{header}\n" + "\n".join(lines)


def _save_screenshot(ctx: ToolContext, png_base64: str) -> str:
    directory = ctx.workspace / ".codelite" / "browser-shots"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"shot-{uuid.uuid4().hex[:10]}.png"
    path.write_bytes(base64.b64decode(png_base64))
    return ctx.relative(path) or str(path)


def _run_browser(args: dict[str, Any], ctx: ToolContext) -> str:
    action = args.get("action")
    if action not in _ACTIONS:
        raise ToolError(f"Unknown browser action `{action}`. Use one of: {', '.join(_ACTIONS)}.")

    if action in _GATED_ACTIONS:
        ctx.permissions.require_write(_permission_label(action, args))

    client = get_client()
    try:
        if action == "navigate":
            url = args.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ToolError("`url` is required for navigate.")
            result = client.call("navigate", url=url.strip())
            return f"Loaded {result.get('url')} — \"{result.get('title')}\""

        if action == "snapshot":
            max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)
            result = client.call("snapshot", max_chars=max_chars)
            return _format_snapshot(result)

        if action == "click":
            ref = _require_ref(args)
            client.call("click", ref=ref)
            return f"Clicked {ref}."

        if action == "fill":
            ref = _require_ref(args)
            value = args.get("value")
            if not isinstance(value, str):
                raise ToolError("`value` is required for fill.")
            client.call("fill", ref=ref, value=value)
            return f"Filled {ref}."

        if action == "evaluate":
            script = args.get("script")
            if not isinstance(script, str) or not script.strip():
                raise ToolError("`script` is required for evaluate.")
            result = client.call("evaluate", script=script)
            return json.dumps(result, ensure_ascii=False) if result is not None else "null"

        if action == "screenshot":
            result = client.call("screenshot")
            path = _save_screenshot(ctx, result["png_base64"])
            return f"Saved screenshot to {path}."

        # action == "reset"
        client.call("reset")
        return "Browser session reset."
    except BrowserError as error:
        raise ToolError(str(error)) from error


BROWSER = Tool(
    name="browser",
    description=(
        "Drive a hidden, scriptable browser window for pages that render their "
        "content with JavaScript -- the case web_fetch cannot read, since it "
        "only sees the initial HTML. Prefer web_search/web_fetch for anything "
        "they can already handle; reach for this only when a page needs "
        "actual script execution to show its content or to be interacted with.\n\n"
        "Actions: `navigate` (url, or \"back\"/\"forward\"), `snapshot` (a compact "
        "list of interactive elements, each with a stable `ref_N` to use with "
        "click/fill), `click` (ref), `fill` (ref, value), `evaluate` (script; "
        "runs arbitrary JavaScript and returns its value), `screenshot` (saves a "
        "PNG into the workspace and returns its path), `reset` (start over from "
        "a blank page).\n\n"
        "Call `snapshot` after `navigate` and again after anything that changes "
        "the page -- references are only valid for the elements currently on it."
    ),
    parameters=object_schema(
        {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "url": {
                "type": "string",
                "description": "For navigate: a URL, or \"back\"/\"forward\".",
            },
            "ref": {
                "type": "string",
                "description": "For click/fill: a reference from the last snapshot, e.g. \"ref_3\".",
            },
            "value": {"type": "string", "description": "For fill: the text to enter."},
            "script": {"type": "string", "description": "For evaluate: JavaScript to run."},
            "max_chars": {
                "type": "integer",
                "description": f"For snapshot: output budget (default {DEFAULT_MAX_CHARS}).",
            },
        },
        required=["action"],
    ),
    run=_run_browser,
)
