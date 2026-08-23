"""Structured questions an agent can ask while it is working."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext


def _run_ask_user(args: dict[str, Any], ctx: ToolContext) -> str:
    question = args.get("question")
    options = args.get("options", [])
    header = args.get("header", "")
    allow_freeform = bool(args.get("allow_freeform", True))
    if not isinstance(question, str) or not question.strip():
        raise ToolError("`question` must be a non-empty string.")
    if not isinstance(header, str):
        raise ToolError("`header` must be a string.")
    if not isinstance(options, list) or len(options) > 5:
        raise ToolError("`options` must contain at most five choices.")
    normalized: list[dict[str, str]] = []
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("label"), str):
            raise ToolError("Every option needs a string `label`.")
        label = option["label"].strip()
        description = option.get("description", "")
        if not label or not isinstance(description, str):
            raise ToolError("Option labels must be non-empty and descriptions must be strings.")
        normalized.append({"label": label, "description": description.strip()})
    if len({option["label"] for option in normalized}) != len(normalized):
        raise ToolError("Question option labels must be unique.")
    if not normalized and not allow_freeform:
        raise ToolError("Provide choices or allow free-form answers.")
    if ctx.questions is None:
        raise ToolError("Interactive questions are not available in this run.")
    answer = ctx.questions.ask(question.strip(), normalized, header.strip(), allow_freeform)
    return f"User answer: {answer}"


ASK_USER = Tool(
    name="ask_user",
    description=(
        "Ask the user a focused question while working. Offer up to five concise choices "
        "when they would make the decision easier; free-form answers can also be allowed."
    ),
    parameters=object_schema(
        {
            "header": {"type": "string", "description": "Short label for the question."},
            "question": {"type": "string", "description": "The decision or clarification needed."},
            "options": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["label", "description"],
                },
            },
            "allow_freeform": {"type": "boolean", "description": "Allow a typed answer (default true)."},
        },
        required=["question"],
    ),
    run=_run_ask_user,
)
