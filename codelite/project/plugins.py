# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Lazy, permission-gated project-local tools and tool execution hooks."""

from __future__ import annotations

import ast
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from ..tools.base import Tool, ToolError
from ..tools.context import ToolContext

LOCAL_TOOLS_DIR = Path(".codelite/tools")
LOCAL_PLUGINS_DIR = Path(".codelite/plugins")
MAX_EXTENSIONS = 32
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HOOK_NAMES = {"before_tool", "after_tool"}


@dataclass(frozen=True)
class _Definition:
    path: Path
    tool: dict[str, Any] | None
    hooks: tuple[str, ...]


def _literal_assignment(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
    return None


def _definition(path: Path) -> _Definition | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    tool = _literal_assignment(tree, "TOOL")
    if not isinstance(tool, dict):
        tool = None
    hooks_value = _literal_assignment(tree, "HOOKS")
    hooks = tuple(
        value for value in hooks_value or [] if isinstance(value, str) and value in _HOOK_NAMES
    ) if isinstance(hooks_value, (list, tuple)) else ()
    if tool is None and not hooks:
        return None
    return _Definition(path=path, tool=tool, hooks=hooks)


def _discover(workspace: Path) -> list[_Definition]:
    root = workspace.resolve()
    paths: list[Path] = []
    for relative in (LOCAL_TOOLS_DIR, LOCAL_PLUGINS_DIR):
        directory = (root / relative).resolve()
        if root not in directory.parents or not directory.is_dir():
            continue
        paths.extend(sorted(directory.glob("*.py")))
    definitions: list[_Definition] = []
    for path in paths[:MAX_EXTENSIONS]:
        resolved = path.resolve()
        if root not in resolved.parents or not resolved.is_file():
            continue
        item = _definition(resolved)
        if item is not None:
            definitions.append(item)
    return definitions


class LocalExtensionHost:
    """Discovers metadata safely and imports extension code only when invoked."""

    def __init__(self, workspace: Path, reserved_names: set[str]) -> None:
        self.workspace = workspace.resolve()
        self._definitions = _discover(self.workspace)
        self._modules: dict[Path, ModuleType] = {}
        self._tools: dict[str, tuple[Tool, _Definition]] = {}
        self._hooks = [item for item in self._definitions if item.hooks]
        for item in self._definitions:
            metadata = item.tool
            if not metadata:
                continue
            name = metadata.get("name")
            description = metadata.get("description")
            parameters = metadata.get("parameters")
            if (
                not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or name in reserved_names
                or not isinstance(description, str)
                or not isinstance(parameters, dict)
            ):
                continue
            tool = Tool(
                name=name,
                description=description[:1_000],
                parameters=parameters,
                run=lambda arguments, context, definition=item: self._run_local_tool(
                    definition, arguments, context
                ),
            )
            self._tools[name] = (tool, item)

    def tools(self) -> list[Tool]:
        return [item[0] for item in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        entry = self._tools.get(name)
        return entry[0] if entry else None

    def names(self) -> list[str]:
        return list(self._tools)

    def _authorize(self, definition: _Definition, context: ToolContext) -> None:
        relative = definition.path.relative_to(self.workspace).as_posix()
        context.permissions.require_shell(
            f"load Code Lite extension {relative}",
            context.task_prompt,
            context.relative(context.cwd or self.workspace) or ".",
        )

    def _load(self, definition: _Definition, context: ToolContext) -> ModuleType:
        cached = self._modules.get(definition.path)
        if cached is not None:
            return cached
        self._authorize(definition, context)
        module_name = "codelite_local_" + re.sub(r"\W+", "_", str(definition.path))
        spec = importlib.util.spec_from_file_location(module_name, definition.path)
        if spec is None or spec.loader is None:
            raise ToolError(f"Could not load local extension `{definition.path.name}`.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._modules[definition.path] = module
        return module

    def _run_local_tool(
        self, definition: _Definition, arguments: dict[str, Any], context: ToolContext
    ) -> str:
        function = getattr(self._load(definition, context), "run", None)
        if not callable(function):
            raise ToolError(f"Local tool `{definition.path.name}` has no callable `run` function.")
        result = function(arguments, context)
        return result if isinstance(result, str) else str(result)

    def before_tool(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        current = arguments
        for definition in self._hooks:
            if "before_tool" not in definition.hooks:
                continue
            function = getattr(self._load(definition, context), "before_tool", None)
            if not callable(function):
                raise ToolError(f"Plugin `{definition.path.name}` declares a missing `before_tool` hook.")
            changed = function(name, current, context)
            if changed is not None:
                if not isinstance(changed, dict):
                    raise ToolError("A `before_tool` hook must return a dictionary or None.")
                current = changed
        return current

    def after_tool(
        self, name: str, arguments: dict[str, Any], output: str, context: ToolContext
    ) -> str:
        current = output
        for definition in reversed(self._hooks):
            if "after_tool" not in definition.hooks:
                continue
            function = getattr(self._load(definition, context), "after_tool", None)
            if not callable(function):
                raise ToolError(f"Plugin `{definition.path.name}` declares a missing `after_tool` hook.")
            changed = function(name, arguments, current, context)
            if changed is not None:
                current = changed if isinstance(changed, str) else str(changed)
        return current
