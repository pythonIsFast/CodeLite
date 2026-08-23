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

"""Your ChatGPT plan's usage allowance, as reported by the Codex backend.

Codex does not expose a "how much of my plan have I used" endpoint. Instead
every ``/responses`` call comes back with a set of ``x-codex-*`` response
headers describing the current state of the account's rate-limit windows.
This module turns those headers into something the UI can show.

Observed on a Plus account (2026-08):

    x-codex-plan-type:                    plus
    x-codex-active-limit:                 premium
    x-codex-primary-used-percent:         12
    x-codex-primary-window-minutes:       10080      (7 days -> the weekly cap)
    x-codex-primary-reset-after-seconds:  334917
    x-codex-primary-reset-at:             1787818072 (unix seconds)
    x-codex-secondary-*:                  0 / empty  (the old 5-hour window)
    x-codex-credits-balance:              0

Nothing here is documented or guaranteed, so every field is optional and a
missing or unparseable value yields ``None`` rather than a guess. The window
length is what identifies a limit -- do not assume "primary" means weekly;
check ``window_minutes``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: A window at least this long is treated as the weekly allowance. Codex
#: reports exactly 10080 minutes (7 days) today, but the comparison is a range
#: so a slightly different figure still reads as "weekly" rather than unknown.
WEEKLY_WINDOW_MIN_MINUTES = 6 * 24 * 60


def _percent(raw: Any) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, value))


def _integer(raw: Any) -> int | None:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _flag(raw: Any) -> bool | None:
    value = _text(raw)
    if value is None:
        return None
    return value.lower() in ("true", "1", "yes")


@dataclass(frozen=True)
class Window:
    """One rate-limit window (Codex reports a "primary" and a "secondary")."""

    used_percent: float | None = None
    window_minutes: int | None = None
    reset_after_seconds: int | None = None
    reset_at: int | None = None

    @property
    def known(self) -> bool:
        """True when this window is actually in use.

        A disabled window comes back as zeroes -- which is how the old
        five-hour limit now reports -- and must not be shown as "0% used".
        """
        return self.used_percent is not None and bool(self.window_minutes)

    @property
    def is_weekly(self) -> bool:
        return bool(self.window_minutes) and self.window_minutes >= WEEKLY_WINDOW_MIN_MINUTES

    def as_dict(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "window_minutes": self.window_minutes,
            "reset_after_seconds": self.reset_after_seconds,
            "reset_at": self.reset_at,
            "is_weekly": self.is_weekly,
        }


@dataclass(frozen=True)
class RateLimits:
    """A snapshot of the account's plan usage."""

    plan_type: str | None = None
    active_limit: str | None = None
    primary: Window = field(default_factory=Window)
    secondary: Window = field(default_factory=Window)
    credits_balance: int | None = None
    credits_unlimited: bool | None = None
    has_credits: bool | None = None

    @property
    def weekly(self) -> Window | None:
        """The weekly window, whichever slot reports it."""
        for window in (self.primary, self.secondary):
            if window.known and window.is_weekly:
                return window
        return None

    def as_dict(self) -> dict[str, Any]:
        weekly = self.weekly
        return {
            "plan_type": self.plan_type,
            "active_limit": self.active_limit,
            "primary": self.primary.as_dict(),
            "secondary": self.secondary.as_dict(),
            "weekly": weekly.as_dict() if weekly else None,
            "credits_balance": self.credits_balance,
            "credits_unlimited": self.credits_unlimited,
            "has_credits": self.has_credits,
        }


def _window(headers: Mapping[str, str], prefix: str) -> Window:
    return Window(
        used_percent=_percent(headers.get(f"x-codex-{prefix}-used-percent")),
        window_minutes=_integer(headers.get(f"x-codex-{prefix}-window-minutes")),
        reset_after_seconds=_integer(headers.get(f"x-codex-{prefix}-reset-after-seconds")),
        reset_at=_integer(headers.get(f"x-codex-{prefix}-reset-at")),
    )


def parse_rate_limits(headers: Mapping[str, str]) -> RateLimits | None:
    """Read the ``x-codex-*`` headers off a response. ``None`` if absent.

    Header names are matched case-insensitively: they arrive lowercase today,
    but HTTP header casing is not something to rely on.
    """
    lowered = {str(key).lower(): value for key, value in headers.items()}
    if not any(key.startswith("x-codex-") for key in lowered):
        return None

    return RateLimits(
        plan_type=_text(lowered.get("x-codex-plan-type")),
        active_limit=_text(lowered.get("x-codex-active-limit")),
        primary=_window(lowered, "primary"),
        secondary=_window(lowered, "secondary"),
        credits_balance=_integer(lowered.get("x-codex-credits-balance")),
        credits_unlimited=_flag(lowered.get("x-codex-credits-unlimited")),
        has_credits=_flag(lowered.get("x-codex-credits-has-credits")),
    )
