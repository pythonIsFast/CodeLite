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

"""Conversation storage on SQLite.

History is stored as Responses-API *input items* -- exactly the shape the
provider layer already expects for its ``input`` field. That means loading a
conversation and sending it to the model needs no translation step, and the
UI can render from the same rows.

A fresh connection is opened per operation rather than shared: the agent loop
runs in background threads while Flask serves requests, and SQLite
connections are not safely shareable across threads. For a single-user local
app the cost is irrelevant, and WAL mode keeps concurrent readers happy.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    workspace       TEXT NOT NULL,
    model           TEXT NOT NULL,
    permission_mode TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_conversation ON items(conversation_id, id);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class Conversation:
    id: str
    title: str
    workspace: str
    model: str
    permission_mode: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Conversation":
        return cls(**{key: row[key] for key in row.keys()})

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "permission_mode": self.permission_mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Store:
    """Thin, parameterized-SQL wrapper. No ORM, no migrations framework."""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- conversations -------------------------------------------------------

    def create_conversation(
        self, workspace: str, model: str, permission_mode: str, title: str = ""
    ) -> Conversation:
        conversation = Conversation(
            id=uuid.uuid4().hex,
            title=title,
            workspace=workspace,
            model=model,
            permission_mode=permission_mode,
            created_at=_now(),
            updated_at=_now(),
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, workspace, model, "
                "permission_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation.id,
                    conversation.title,
                    conversation.workspace,
                    conversation.model,
                    conversation.permission_mode,
                    conversation.created_at,
                    conversation.updated_at,
                ),
            )
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return Conversation.from_row(row) if row else None

    def list_conversations(self, limit: int = 100) -> list[Conversation]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Conversation.from_row(row) for row in rows]

    def update_conversation(self, conversation_id: str, **fields: str) -> None:
        allowed = {"title", "model", "permission_mode", "workspace"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE conversations SET {assignments}, updated_at = ? WHERE id = ?",
                (*updates.values(), _now(), conversation_id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    # -- items ---------------------------------------------------------------

    def append_items(self, conversation_id: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        timestamp = _now()
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO items (conversation_id, payload, created_at) VALUES (?, ?, ?)",
                [(conversation_id, json.dumps(item), timestamp) for item in items],
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (timestamp, conversation_id),
            )

    def load_items(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM items WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                items.append(json.loads(row["payload"]))
            except ValueError:
                continue  # Skip a corrupt row rather than breaking the whole load.
        return items

    def count_items(self, conversation_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["n"]) if row else 0
