"""A small human-in-the-loop question channel for agent tools."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

Publisher = Callable[[str, dict[str, Any]], None]


@dataclass
class PendingQuestion:
    id: str
    question: str
    header: str
    options: list[dict[str, str]]
    allow_freeform: bool
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _answer: str | None = field(default=None, repr=False)

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "header": self.header,
            "options": self.options,
            "allow_freeform": self.allow_freeform,
        }


class QuestionManager:
    """Keeps one or more agent questions blocked until the user answers."""

    def __init__(self, publish: Publisher) -> None:
        self._publish = publish
        self._lock = threading.Lock()
        self._pending: dict[str, PendingQuestion] = {}

    def ask(
        self, question: str, options: list[dict[str, str]], header: str = "", allow_freeform: bool = True
    ) -> str:
        pending = PendingQuestion(
            id=uuid.uuid4().hex,
            question=question,
            header=header,
            options=options,
            allow_freeform=allow_freeform,
        )
        with self._lock:
            self._pending[pending.id] = pending
        self._publish("question_request", pending.payload())
        pending._event.wait()
        with self._lock:
            self._pending.pop(pending.id, None)
        return pending._answer or "The user cancelled the question."

    def reply(self, question_id: str, answer: str) -> bool:
        answer = answer.strip()
        with self._lock:
            pending = self._pending.get(question_id)
            if pending is None or not answer:
                return False
            labels = {option["label"] for option in pending.options}
            cancelled = answer == "The user cancelled the question."
            if not pending.allow_freeform and answer not in labels and not cancelled:
                return False
            pending._answer = answer
        pending._event.set()
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [pending.payload() for pending in self._pending.values()]

    def cancel_pending(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
        for item in pending:
            item._answer = "The user cancelled the question."
            item._event.set()
