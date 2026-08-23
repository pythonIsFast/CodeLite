# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Cheap, explicit task routing for the Auto model picker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..provider.chat import parse_responses_output
from ..provider.session import Session

AUTO_MODEL = "auto"
ROUTER_MODEL = "gpt-5.6-luna"
FALLBACK_MODEL = "gpt-5.6-terra"
ROUTABLE_MODELS = {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}

MODEL_PROFILES = {
    "gpt-5.6-luna": {
        "name": "Luna",
        "fit": "Best for focused questions, routine edits, and quick checks.",
        "limit": "Not the first choice for risky, cross-cutting, or difficult debugging.",
    },
    "gpt-5.6-terra": {
        "name": "Terra",
        "fit": "Best for normal multi-file work, refactors, and debugging.",
        "limit": "Usually unnecessary for one small, well-bounded task.",
    },
    "gpt-5.6-sol": {
        "name": "Sol",
        "fit": "Best for complex architecture, high-risk changes, and hard investigation.",
        "limit": "Avoid for routine work when Luna or Terra is sufficient.",
    },
}

ROUTER_INSTRUCTIONS = """\
You are the lightweight model router for a local coding agent. Choose the
cheapest GPT-5.6 model that is capable of completing the user's task.

Choose gpt-5.6-luna for focused questions, routine edits, small lookups, and
simple bounded tasks. Choose gpt-5.6-terra for typical multi-file changes,
ordinary debugging, refactors, or work that needs stronger coordination.
Choose gpt-5.6-sol only for genuinely complex architecture, high-risk/security
or data-sensitive changes, large cross-cutting refactors, or hard investigation
where failure would be costly. Do not choose Sol merely because the task is a
coding task. Prefer Luna whenever its limits clearly do not apply.

Return JSON only: {"model":"gpt-5.6-luna|gpt-5.6-terra|gpt-5.6-sol", "reason":"one concise sentence for the user"}.\
"""


@dataclass(frozen=True)
class ModelDecision:
    model: str
    reason: str
    fallback: bool = False

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "router": MODEL_PROFILES[ROUTER_MODEL]["name"],
            "model": self.model,
            "model_name": MODEL_PROFILES[self.model]["name"],
            "reason": self.reason,
            "fallback": self.fallback,
            "profiles": MODEL_PROFILES,
        }


def _parse_json(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        candidate = candidate.rsplit("```", 1)[0]
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def select_model(session: Session, user_text: str) -> tuple[ModelDecision, dict[str, Any]]:
    """Use Luna with low reasoning and fall back to Terra if routing fails."""
    body = {
        "model": ROUTER_MODEL,
        "reasoning": {"effort": "low"},
        "instructions": ROUTER_INSTRUCTIONS,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
    }
    try:
        response = session.send_responses(body, stream=False)
        if not isinstance(response, dict):
            raise ValueError("Luna returned an unreadable response.")
        text = response.get("output_text")
        if not isinstance(text, str):
            text = parse_responses_output(response).text
        parsed = _parse_json(text)
        model = parsed.get("model") if parsed else None
        reason = parsed.get("reason") if parsed else None
        if model not in ROUTABLE_MODELS or not isinstance(reason, str) or not reason.strip():
            raise ValueError("Luna returned an invalid routing decision.")
        return ModelDecision(model=model, reason=reason.strip()[:400]), response
    except Exception as error:  # noqa: BLE001 - routing must never prevent a task
        return (
            ModelDecision(
                model=FALLBACK_MODEL,
                reason=(
                    "Luna could not make a routing decision, so Code Lite chose "
                    "Terra as the balanced fallback."
                ),
                fallback=True,
            ),
            {"routing_error": str(error)},
        )
