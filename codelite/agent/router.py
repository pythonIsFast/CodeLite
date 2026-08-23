# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""Cheap, explicit task routing for the Auto model picker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from ..config import normalize_effort
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

Also choose how hard the chosen model should think. Use "low" for lookups,
small edits, and anything mechanical; "medium" for ordinary multi-step work;
"high" only when the task needs sustained reasoning, such as a subtle bug, a
design decision with trade-offs, or a change whose failure is expensive.
Effort costs time and tokens on every turn of the run, so do not raise it
because the task sounds important -- raise it only when thinking harder is
what the task actually needs.

Return JSON only: {"model":"%(models)s", "effort":"low|medium|high", "reason":"one concise sentence for the user"}.\
"""


def _instructions(allowed: Sequence[str]) -> str:
    """Name only the models the router is actually allowed to return.

    Offering a model in the prompt that the caller then rejects wastes a whole
    routing round trip on a decision that can only fail validation.
    """
    text = ROUTER_INSTRUCTIONS % {"models": "|".join(allowed)}
    # The prose above still describes every model by name. When the set has
    # been narrowed, say so explicitly rather than relying on validation to
    # catch a choice the prompt itself invited.
    if set(allowed) != ROUTABLE_MODELS:
        text += f"\n\nOnly these models are available: {', '.join(allowed)}."
    return text


@dataclass(frozen=True)
class ModelDecision:
    model: str
    reason: str
    #: Empty means the model's own catalog default was kept.
    effort: str = ""
    fallback: bool = False

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "router": MODEL_PROFILES[ROUTER_MODEL]["name"],
            "model": self.model,
            "model_name": MODEL_PROFILES[self.model]["name"],
            "reason": self.reason,
            "effort": self.effort,
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


def select_model(
    session: Session,
    user_text: str,
    *,
    router_model: str = ROUTER_MODEL,
    fallback_model: str = FALLBACK_MODEL,
    routable: Sequence[str] = (),
) -> tuple[ModelDecision, dict[str, Any]]:
    """Route one message, falling back to `fallback_model` if that fails.

    The model set is passed in rather than read from the module constant so it
    can be narrowed in settings -- dropping the largest model is the simplest
    cap on what a single run can cost.
    """
    allowed = tuple(routable) or tuple(ROUTABLE_MODELS)
    body = {
        "model": router_model or ROUTER_MODEL,
        "reasoning": {"effort": "low"},
        "instructions": _instructions(allowed),
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
        if model not in allowed or not isinstance(reason, str) or not reason.strip():
            raise ValueError("Luna returned an invalid routing decision.")
        # An unusable effort is not worth failing the whole routing decision
        # over: normalize_effort returns "" and the model's catalog default
        # applies, which is exactly what no answer should mean.
        effort = normalize_effort(parsed.get("effort"))
        return ModelDecision(model=model, reason=reason.strip()[:400], effort=effort), response
    except Exception as error:  # noqa: BLE001 - routing must never prevent a task
        return (
            ModelDecision(
                model=fallback_model or FALLBACK_MODEL,
                reason=(
                    "Auto could not make a routing decision, so Code Lite chose "
                    "Terra as the balanced fallback."
                ),
                effort="medium",
                fallback=True,
            ),
            {"routing_error": str(error)},
        )
