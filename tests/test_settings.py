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

"""Coercion rules for the behaviour settings.

Every one of these values is a protective limit, so what matters is not that a
number round-trips but that an unusable one cannot reach the agent.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from codelite import settings


@contextmanager
def raises(expected: type[BaseException]) -> Iterator[None]:
    """A local stand-in for pytest.raises.

    The repository has no test dependency and pytest is not installed, so the
    tests stay runnable with a plain interpreter rather than adding one for a
    single helper.
    """
    try:
        yield
    except expected:
        return
    raise AssertionError(f"expected {expected.__name__}")


def test_defaults_cover_every_declared_setting() -> None:
    values = settings.defaults()
    assert set(values) == {setting.key for setting in settings.SETTINGS}
    # Round-tripping the defaults must be a no-op, or the defaults are already
    # outside their own bounds.
    assert settings.coerce(values) == values


def test_numbers_are_clamped_not_refused() -> None:
    # A form that keeps the rest of the user's edits and corrects one field is
    # friendlier than one that throws the whole payload away.
    values = settings.coerce({"shell_timeout_seconds": 999_999})
    assert values["shell_timeout_seconds"] == settings.BY_KEY["shell_timeout_seconds"].maximum
    values = settings.coerce({"max_tool_output_chars": 1})
    assert values["max_tool_output_chars"] == settings.BY_KEY["max_tool_output_chars"].minimum


def test_wrong_types_are_refused() -> None:
    for payload in [
        {"shell_timeout_seconds": "soon"},
        {"shell_timeout_seconds": True},
        {"lsp_timeout_seconds": None},
        {"agent_model": ""},
        {"auto_models": []},
    ]:
        with raises(ValueError):
            settings.coerce(payload)


def test_unknown_keys_are_ignored() -> None:
    # A database written by another build must not make the app unstartable.
    values = settings.coerce({"from_the_future": 1, "shell_timeout_seconds": 60})
    assert "from_the_future" not in values
    assert values["shell_timeout_seconds"] == 60


def test_compaction_cannot_cross_the_stop_threshold() -> None:
    # Compacting at or past the hard stop means the run aborts before
    # compaction ever runs, so the pair gets pulled back apart.
    values = settings.coerce(
        {"context_compact_fraction": 0.95, "context_stop_fraction": 0.90}
    )
    assert values["context_compact_fraction"] <= values["context_stop_fraction"] - 0.02


def test_a_valid_pair_is_left_alone() -> None:
    values = settings.coerce(
        {"context_compact_fraction": 0.60, "context_stop_fraction": 0.90}
    )
    assert values["context_compact_fraction"] == 0.60
    assert values["context_stop_fraction"] == 0.90


def test_unknown_models_are_dropped_but_never_all_of_them() -> None:
    known = ["gpt-5.6-luna", "gpt-5.6-terra"]
    values = settings.coerce({"auto_models": ["gpt-5.6-luna", "made-up"]}, known)
    assert values["auto_models"] == ["gpt-5.6-luna"]

    # If nothing matches, the catalog is more likely wrong than the user, so
    # this raises rather than silently emptying the list.
    with raises(ValueError):
        settings.coerce({"auto_models": ["made-up"]}, known)


def test_an_unknown_single_model_is_kept() -> None:
    # The catalog is a network call. A lookup failure must not wipe a choice
    # the user made while it was reachable.
    values = settings.coerce({"agent_model": "gpt-9"}, ["gpt-5.6-luna"])
    assert values["agent_model"] == "gpt-9"


def test_active_limits_track_the_last_apply() -> None:
    original = settings.active("mcp_timeout_seconds")
    try:
        settings.apply(settings.coerce({"mcp_timeout_seconds": 42}))
        assert settings.active("mcp_timeout_seconds") == 42
    finally:
        settings.apply({"mcp_timeout_seconds": original})


def test_schema_describes_every_setting_and_group() -> None:
    described = settings.schema(["gpt-5.6-luna"])
    assert described["models"] == ["gpt-5.6-luna"]
    assert {group["key"] for group in described["groups"]} >= {
        setting.group for setting in settings.SETTINGS
    }
    for entry in described["settings"]:
        assert entry["label"] and entry["help"], entry["key"]
