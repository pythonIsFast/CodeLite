# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Lightweight project-level context shared by every conversation."""

from .context import build_project_context, discover_skills, read_skill

__all__ = ["build_project_context", "discover_skills", "read_skill"]
