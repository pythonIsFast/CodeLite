# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Small, portable project memory and lazily loaded skill discovery.

There is deliberately no embedding model or vector database here. Stable
facts are cheap to read from one bounded Markdown file, while skill bodies are
only inserted into a conversation after the model explicitly asks for one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = ".codelite"
MEMORY_PATH = Path(PROJECT_DIR) / "memory.md"
PROJECT_SKILLS_DIR = Path(PROJECT_DIR) / "skills"
MCP_CONFIG_PATH = Path(PROJECT_DIR) / "mcp.json"
LSP_CONFIG_PATH = Path(PROJECT_DIR) / "lsp.json"

MAX_MEMORY_CHARS = 2_500
MAX_SKILL_INDEX_CHARS = 1_500
MAX_SKILL_BODY_CHARS = 20_000
MAX_INSTRUCTION_FILES = 24
MAX_INSTRUCTION_FILE_CHARS = 4_000
MAX_INSTRUCTION_CHARS = 12_000

# Do not waste startup time walking generated dependencies or build outputs just
# to find a repository instruction file. This also keeps a checked-out
# dependency from unexpectedly contributing instructions to its parent project.
IGNORED_INSTRUCTION_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    scope: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
        }


def _skill_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    root = directory.resolve()
    files = list(directory.glob("*.md"))
    files.extend(directory.glob("*/SKILL.md"))
    resolved = {path.resolve() for path in files if path.is_file()}
    return sorted(path for path in resolved if root in path.parents)


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            values[key.strip()] = value.strip().strip('"\'')
    return values


def _describe_skill(path: Path, scope: str) -> SkillInfo | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    metadata = _frontmatter(text)
    fallback_name = path.parent.name if path.name == "SKILL.md" else path.stem
    name = metadata.get("name") or fallback_name
    description = metadata.get("description")
    if not description:
        body = re.sub(r"\A---\n.*?\n---\s*", "", text, flags=re.DOTALL)
        description = next(
            (line.lstrip("# ").strip() for line in body.splitlines() if line.strip()),
            "Project workflow instructions.",
        )
    return SkillInfo(name=name, description=description[:240], path=path, scope=scope)


def discover_skills(workspace: Path, data_dir: Path) -> list[SkillInfo]:
    """Find project and user skills, with project definitions taking priority."""
    discovered: dict[str, SkillInfo] = {}
    for directory, scope in (
        (Path(data_dir) / "skills", "user"),
        (Path(workspace) / PROJECT_SKILLS_DIR, "project"),
    ):
        allowed_root = Path(data_dir).resolve() if scope == "user" else Path(workspace).resolve()
        for path in _skill_files(directory):
            if allowed_root not in path.parents:
                continue
            info = _describe_skill(path, scope)
            if info is not None:
                discovered[info.name] = info
    return sorted(discovered.values(), key=lambda item: item.name.lower())


def read_skill(workspace: Path, data_dir: Path, name: str) -> str:
    for skill in discover_skills(workspace, data_dir):
        if skill.name == name:
            text = skill.path.read_text(encoding="utf-8")
            if len(text) > MAX_SKILL_BODY_CHARS:
                return text[:MAX_SKILL_BODY_CHARS] + "\n\n[Skill truncated]"
            return text
    raise KeyError(name)


def _configured_mcp_names(workspace: Path) -> list[str]:
    root = Path(workspace).resolve()
    path = (root / MCP_CONFIG_PATH).resolve()
    if root not in path.parents:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted(
        str(name)
        for name, value in servers.items()
        if isinstance(value, dict) and value.get("disabled") is not True
    )


def _repository_instruction_files(workspace: Path) -> list[Path]:
    """Find repository-local AGENTS.md and CLAUDE.md files cheaply and safely."""
    root = Path(workspace).resolve()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for directory, directories, names in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories if name not in IGNORED_INSTRUCTION_DIRS
        ]
        for name in names:
            if name not in {"AGENTS.md", "CLAUDE.md"}:
                continue
            path = (Path(directory) / name).resolve()
            if root not in path.parents or not path.is_file():
                continue
            files.append(path)
            if len(files) >= MAX_INSTRUCTION_FILES:
                break
        if len(files) >= MAX_INSTRUCTION_FILES:
            break
    return sorted(files, key=lambda path: (len(path.relative_to(root).parts), str(path)))


def _repository_instructions(workspace: Path) -> str:
    root = Path(workspace).resolve()
    remaining = MAX_INSTRUCTION_CHARS
    documents: list[str] = []
    for path in _repository_instruction_files(root):
        if remaining <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not text:
            continue
        clipped = text[: min(MAX_INSTRUCTION_FILE_CHARS, remaining)]
        if len(clipped) < len(text):
            clipped += "\n[Instruction file truncated.]"
        documents.append(f"--- {path.relative_to(root)} ---\n{clipped}")
        remaining -= len(clipped)
    if not documents:
        return ""
    return (
        "Repository instructions (loaded before this run; follow the applicable "
        "instructions before starting work):\n" + "\n\n".join(documents)
    )


def build_project_context(workspace: Path, data_dir: Path) -> str:
    """Build a tightly bounded prompt fragment for one workspace."""
    sections: list[str] = []
    root = Path(workspace).resolve()
    instructions = _repository_instructions(root)
    if instructions:
        sections.append(instructions)
    memory_path = (root / MEMORY_PATH).resolve()
    if root not in memory_path.parents:
        memory = ""
    else:
        try:
            memory = memory_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            memory = ""
    if memory:
        clipped = memory[:MAX_MEMORY_CHARS]
        if len(memory) > len(clipped):
            clipped += "\n[Project memory truncated; keep it more concise.]"
        sections.append("Project memory (stable facts, not current-task instructions):\n" + clipped)

    skills = discover_skills(workspace, data_dir)
    if skills:
        lines = [f"- {skill.name}: {skill.description}" for skill in skills]
        index = "\n".join(lines)[:MAX_SKILL_INDEX_CHARS]
        sections.append(
            "Available skills (load a relevant one with `extensions` before using it):\n"
            + index
        )

    servers = _configured_mcp_names(workspace)
    if servers:
        sections.append(
            "Configured MCP servers (discover tools lazily with `extensions`): "
            + ", ".join(servers)
        )
    return "\n\n".join(sections)
