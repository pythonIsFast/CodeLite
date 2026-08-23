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

"""Tests for the app-level model and reasoning-effort policy."""

from __future__ import annotations

from codelite.config import REASONING_EFFORTS, chat_models, normalize_effort


def test_chat_models_excludes_non_chat_entries() -> None:
    catalog = [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "codex-auto-review",
        "gpt-image-2",
    ]
    assert chat_models(catalog, "gpt-image-2") == ["gpt-5.6-sol", "gpt-5.6-terra"]


def test_chat_models_keeps_order_and_survives_no_image_model() -> None:
    catalog = ["gpt-5.4", "gpt-5.6-sol"]
    assert chat_models(catalog) == catalog
    # An empty image model must not filter out the empty string or anything else.
    assert chat_models(catalog, "") == catalog


def test_normalize_effort_accepts_the_documented_levels() -> None:
    for level in REASONING_EFFORTS:
        assert normalize_effort(level) == level
    assert normalize_effort(" HIGH ") == "high"


def test_normalize_effort_falls_back_to_the_model_default() -> None:
    # Everything unusable becomes "", which means "send no reasoning field and
    # let the model's own catalog default apply" -- never a guessed level.
    for value in ["", "minimal", "fast", None, 3, True, ["high"]]:
        assert normalize_effort(value) == ""


def test_normalize_effort_respects_a_model_specific_list() -> None:
    # Luna stops at "max" while Sol reaches "ultra", so the same value has to
    # be accepted for one model and rejected for the other. Getting this wrong
    # is an HTTP 400 at the first turn, not a graceful downgrade.
    luna = ["low", "medium", "high", "xhigh", "max"]
    assert normalize_effort("max", luna) == "max"
    assert normalize_effort("ultra", luna) == ""
    assert normalize_effort("ultra") == "ultra"
