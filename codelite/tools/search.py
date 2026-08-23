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

"""Search tools: content grep and filename glob.

Implemented in pure Python rather than shelling out to ``grep``/``rg`` so
that searching never needs a permission prompt (it touches nothing) and
behaves identically regardless of what is installed on the host.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Iterator

from .base import Tool, ToolError, object_schema
from .context import ToolContext
from .files import IGNORED_DIRS

MAX_MATCHES = 200
MAX_FILE_BYTES = 2_000_000
MAX_LINE_CHARS = 400


def _walk(root: Path) -> Iterator[Path]:
    """Yield files under ``root``, pruning noise directories as we descend."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in IGNORED_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def _search_file(path: Path, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # Binary or unreadable: silently skip, same as ripgrep does.

    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            trimmed = line.strip()
            if len(trimmed) > MAX_LINE_CHARS:
                trimmed = trimmed[:MAX_LINE_CHARS] + "..."
            hits.append((number, trimmed))
    return hits


def _run_grep(args: dict[str, Any], ctx: ToolContext) -> str:
    raw_pattern = args.get("pattern")
    if not raw_pattern:
        raise ToolError("`pattern` is required.")

    flags = 0 if args.get("case_sensitive") else re.IGNORECASE
    try:
        pattern = re.compile(str(raw_pattern), flags)
    except re.error as error:
        raise ToolError(f"`pattern` is not a valid regular expression: {error}") from error

    root = ctx.resolve(args.get("path") or ".")
    if not root.exists():
        raise ToolError(f"{root} does not exist.")

    glob = args.get("glob")
    targets = [root] if root.is_file() else _walk(root)

    results: list[str] = []
    files_with_matches = 0
    truncated = False
    for file_path in targets:
        if glob and not fnmatch.fnmatch(file_path.name, str(glob)):
            continue
        hits = _search_file(file_path, pattern)
        if not hits:
            continue
        files_with_matches += 1
        for number, line in hits:
            if len(results) >= MAX_MATCHES:
                truncated = True
                break
            results.append(f"{ctx.relative(file_path)}:{number}: {line}")
        if truncated:
            break

    if not results:
        return f"No matches for /{raw_pattern}/ under {ctx.relative(root)}."

    header = f"{len(results)} match(es) in {files_with_matches} file(s):"
    footer = f"\n... result limit of {MAX_MATCHES} reached; narrow the pattern." if truncated else ""
    return f"{header}\n" + "\n".join(results) + footer


def _run_find_files(args: dict[str, Any], ctx: ToolContext) -> str:
    glob = args.get("glob")
    if not glob:
        raise ToolError("`glob` is required, e.g. '*.py' or 'test_*'.")

    root = ctx.resolve(args.get("path") or ".")
    if not root.is_dir():
        raise ToolError(f"{root} is not a directory.")

    matches = [
        ctx.relative(p)
        for p in _walk(root)
        if fnmatch.fnmatch(p.name, str(glob)) or fnmatch.fnmatch(str(p), str(glob))
    ]
    if not matches:
        return f"No files matching '{glob}' under {ctx.relative(root)}."

    matches.sort()
    truncated = len(matches) > MAX_MATCHES
    shown = matches[:MAX_MATCHES]
    footer = f"\n... and {len(matches) - MAX_MATCHES} more" if truncated else ""
    return f"{len(matches)} file(s) matching '{glob}':\n" + "\n".join(shown) + footer


GREP = Tool(
    name="grep",
    description=(
        "Search file contents by regular expression. Returns 'path:line: text' "
        "hits. Case-insensitive unless `case_sensitive` is set."
    ),
    parameters=object_schema(
        {
            "pattern": {"type": "string", "description": "Python regular expression."},
            "path": {
                "type": "string",
                "description": "Workspace-relative file or directory to search. Defaults to the root.",
            },
            "glob": {
                "type": "string",
                "description": "Only search files whose name matches this glob, e.g. '*.py'.",
            },
            "case_sensitive": {"type": "boolean", "description": "Match case exactly."},
        },
        required=["pattern"],
    ),
    run=_run_grep,
)

FIND_FILES = Tool(
    name="find_files",
    description="Find files by name pattern (glob), e.g. '*.py' or 'Dockerfile*'.",
    parameters=object_schema(
        {
            "glob": {"type": "string", "description": "Filename glob to match."},
            "path": {
                "type": "string",
                "description": "Workspace-relative directory to search. Defaults to the root.",
            },
        },
        required=["glob"],
    ),
    run=_run_find_files,
)
