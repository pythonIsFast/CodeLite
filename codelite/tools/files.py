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

"""Filesystem tools: read, write, edit, and list.

Reads are never gated (they cannot damage anything); writes and edits go
through :meth:`~codelite.permission.manager.PermissionManager.require_write`
before touching disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext

MAX_READ_BYTES = 400_000
DEFAULT_READ_LIMIT = 2_000

#: Directories that are noise for a coding agent and would drown real results.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".cache",
    }
)


def _read_text(path: Path) -> str:
    if not path.exists():
        raise ToolError(f"{path} does not exist.")
    if path.is_dir():
        raise ToolError(f"{path} is a directory, not a file. Use list_dir instead.")
    if path.stat().st_size > MAX_READ_BYTES:
        raise ToolError(
            f"{path} is larger than {MAX_READ_BYTES} bytes. Read a slice with "
            "`offset`/`limit`, or narrow down with grep first."
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ToolError(f"{path} is not a UTF-8 text file.") from error


def _run_read_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve(args.get("path", ""))
    offset = max(int(args.get("offset") or 0), 0)
    limit = int(args.get("limit") or DEFAULT_READ_LIMIT)

    lines = _read_text(path).splitlines()
    total = len(lines)
    window = lines[offset : offset + limit]
    if not window:
        return f"{ctx.relative(path)} has {total} lines; offset {offset} is past the end."

    # Line numbers let the model refer to exact locations in later edits.
    body = "\n".join(f"{offset + i + 1}\t{line}" for i, line in enumerate(window))
    shown_to = offset + len(window)
    header = f"{ctx.relative(path)} (lines {offset + 1}-{shown_to} of {total})"
    footer = "" if shown_to >= total else f"\n... {total - shown_to} more lines"
    return f"{header}\n{body}{footer}"


def _run_write_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve(args.get("path", ""))
    content = args.get("content")
    if content is None:
        raise ToolError("`content` is required.")

    existed = path.exists()
    ctx.permissions.require_write(ctx.relative(path))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content), encoding="utf-8")
    verb = "Overwrote" if existed else "Created"
    line_count = str(content).count("\n") + 1
    return f"{verb} {ctx.relative(path)} ({line_count} lines)."


def _run_edit_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve(args.get("path", ""))
    old = args.get("old_string")
    new = args.get("new_string")
    replace_all = bool(args.get("replace_all"))

    if not old:
        raise ToolError("`old_string` is required and must not be empty.")
    if new is None:
        raise ToolError("`new_string` is required (use an empty string to delete).")
    if old == new:
        raise ToolError("`old_string` and `new_string` are identical.")

    original = _read_text(path)
    occurrences = original.count(old)
    if occurrences == 0:
        raise ToolError(
            f"`old_string` was not found in {ctx.relative(path)}. Read the file "
            "again -- it must match exactly, including whitespace."
        )
    if occurrences > 1 and not replace_all:
        raise ToolError(
            f"`old_string` appears {occurrences} times in {ctx.relative(path)}. "
            "Add more surrounding context to make it unique, or set "
            "`replace_all` to true."
        )

    ctx.permissions.require_write(ctx.relative(path))
    updated = original.replace(old, str(new)) if replace_all else original.replace(old, str(new), 1)
    path.write_text(updated, encoding="utf-8")
    changed = occurrences if replace_all else 1
    return f"Edited {ctx.relative(path)} ({changed} replacement{'s' if changed != 1 else ''})."


def _run_list_dir(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve(args.get("path") or ".")
    if not path.exists():
        raise ToolError(f"{path} does not exist.")
    if not path.is_dir():
        raise ToolError(f"{path} is not a directory.")

    entries: list[str] = []
    for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if entry.is_dir():
            if entry.name in IGNORED_DIRS:
                continue
            entries.append(f"{entry.name}/")
        else:
            entries.append(entry.name)

    if not entries:
        return f"{ctx.relative(path)} is empty."
    return f"{ctx.relative(path)}:\n" + "\n".join(entries)


READ_FILE = Tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file from the workspace. Returns the content with "
        "line numbers. Use `offset` and `limit` for large files."
    ),
    parameters=object_schema(
        {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "offset": {"type": "integer", "description": "First line to read (0-based)."},
            "limit": {"type": "integer", "description": "How many lines to read."},
        },
        required=["path"],
    ),
    run=_run_read_file,
)

WRITE_FILE = Tool(
    name="write_file",
    description=(
        "Write a file, creating it or replacing its entire content. Prefer "
        "edit_file for changes to existing files -- it is cheaper and safer."
    ),
    parameters=object_schema(
        {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "content": {"type": "string", "description": "The full file content."},
        },
        required=["path", "content"],
    ),
    run=_run_write_file,
)

EDIT_FILE = Tool(
    name="edit_file",
    description=(
        "Replace an exact string in an existing file. `old_string` must match "
        "the file byte-for-byte, including indentation, and must be unique "
        "unless `replace_all` is set."
    ),
    parameters=object_schema(
        {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence instead of requiring uniqueness.",
            },
        },
        required=["path", "old_string", "new_string"],
    ),
    run=_run_edit_file,
)

LIST_DIR = Tool(
    name="list_dir",
    description=(
        "List a directory's entries. Directories are suffixed with '/'. "
        "Build/cache directories are omitted."
    ),
    parameters=object_schema(
        {
            "path": {
                "type": "string",
                "description": "Workspace-relative directory path. Defaults to the workspace root.",
            }
        }
    ),
    run=_run_list_dir,
)
