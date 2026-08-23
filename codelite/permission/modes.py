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

"""The four permission modes a conversation can run in."""

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    """How much the agent may do without asking.

    Reads are never gated in any mode -- they are non-destructive, and
    gating them would make the agent unusable. What differs between modes is
    how *writes* and *shell commands* are handled.
    """

    #: Every file write and every shell command needs manual confirmation.
    ASK = "ask"

    #: File writes run automatically; shell commands still need confirmation.
    PERMIT_WRITES = "permit_writes"

    #: File writes run automatically; shell commands are judged by a second
    #: model, which can escalate back to the user with its reasoning.
    AUTO = "auto"

    #: Everything runs automatically, no checks at all.
    BYPASS = "bypass"

    @property
    def writes_need_approval(self) -> bool:
        return self is Mode.ASK

    @property
    def shell_needs_approval(self) -> bool:
        """True when a shell command cannot run without *some* extra check.

        Note this is also True for :attr:`AUTO`, where the extra check is the
        judge model rather than the user -- see
        :meth:`~codelite.permission.manager.PermissionManager.require_shell`.
        """
        return self in (Mode.ASK, Mode.PERMIT_WRITES, Mode.AUTO)

    @property
    def label(self) -> str:
        return {
            Mode.ASK: "Ask",
            Mode.PERMIT_WRITES: "Permit writes",
            Mode.AUTO: "Auto",
            Mode.BYPASS: "Bypass permissions",
        }[self]

    @classmethod
    def parse(cls, value: object, fallback: "Mode | None" = None) -> "Mode":
        """Parse a mode name, falling back instead of raising on bad input.

        Values arrive from HTTP bodies and from the database, so a bad one is
        an expected condition, not a programming error -- defaulting to the
        most restrictive sensible mode beats crashing a request.
        """
        if isinstance(value, Mode):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return fallback if fallback is not None else cls.ASK
