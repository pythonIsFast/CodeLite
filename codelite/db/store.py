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

CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: Columns added after the first release. Applied with ALTER TABLE on open, so
#: an existing database keeps working instead of needing to be thrown away.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("conversations", "context_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "total_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "meta", "TEXT NOT NULL DEFAULT '{}'"),
    ("conversations", "compaction_summary", "TEXT NOT NULL DEFAULT ''"),
    ("conversations", "compacted_item_count", "INTEGER NOT NULL DEFAULT 0"),
    # Which external session a conversation was imported from, empty for one
    # started here. It is what makes re-running an import a no-op instead of a
    # second copy of every chat.
    ("conversations", "source_id", "TEXT NOT NULL DEFAULT ''"),
    # Empty means the model's own default reasoning level from the catalog.
    ("conversations", "reasoning_effort", "TEXT NOT NULL DEFAULT ''"),
)


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
    #: Tokens the last turn actually sent as context -- what the UI's ring shows.
    context_tokens: int = 0
    #: Cumulative tokens spent across every turn in this conversation.
    total_tokens: int = 0
    #: A private working summary used by the agent; the full transcript stays in items.
    compaction_summary: str = ""
    compacted_item_count: int = 0
    #: Requested reasoning level, or empty for the model's catalog default.
    reasoning_effort: str = ""
    #: The external session this was imported from, empty when started here.
    #: `from_row` splats every column, so a new column has to land here too.
    source_id: str = ""

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
            "reasoning_effort": self.reasoning_effort,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "context_tokens": self.context_tokens,
            "total_tokens": self.total_tokens,
            "compacted_item_count": self.compacted_item_count,
        }


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add any columns a database created by an older version is missing."""
    for table, column, definition in MIGRATIONS:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


class Store:
    """Thin, parameterized-SQL wrapper. No ORM, no migrations framework."""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            _apply_migrations(conn)

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
        self,
        workspace: str,
        model: str,
        permission_mode: str,
        title: str = "",
        reasoning_effort: str = "",
    ) -> Conversation:
        conversation = Conversation(
            id=uuid.uuid4().hex,
            title=title,
            workspace=workspace,
            model=model,
            permission_mode=permission_mode,
            created_at=_now(),
            updated_at=_now(),
            reasoning_effort=reasoning_effort,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, workspace, model, "
                "permission_mode, created_at, updated_at, reasoning_effort) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation.id,
                    conversation.title,
                    conversation.workspace,
                    conversation.model,
                    conversation.permission_mode,
                    conversation.created_at,
                    conversation.updated_at,
                    conversation.reasoning_effort,
                ),
            )
        return conversation

    def imported_source_ids(self) -> set[str]:
        """Every external session already in the database."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_id FROM conversations WHERE source_id != ''"
            ).fetchall()
        return {row["source_id"] for row in rows}

    def import_conversation(
        self,
        *,
        source_id: str,
        title: str,
        workspace: str,
        model: str,
        permission_mode: str,
        created_at: str,
        items: list[dict[str, Any]],
        timestamps: list[str] | None = None,
        context_tokens: int = 0,
        total_tokens: int = 0,
    ) -> str:
        """Insert a whole conversation with its original timestamps.

        Separate from :meth:`create_conversation` plus :meth:`append_items` for
        two reasons: an import must keep the times the exchange actually
        happened rather than stamping everything "now", and it has to be one
        transaction, so a failure halfway cannot leave a conversation whose
        history stops mid-sentence.
        """
        conversation_id = uuid.uuid4().hex
        stamps = list(timestamps or [])
        # A short list would silently shift every later item's time, so pad
        # rather than zip: the count comes from a file we did not write.
        if len(stamps) < len(items):
            stamps += [created_at] * (len(items) - len(stamps))
        updated_at = stamps[-1] if stamps else created_at

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, workspace, model, "
                "permission_mode, created_at, updated_at, context_tokens, "
                "total_tokens, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    title,
                    workspace,
                    model,
                    permission_mode,
                    created_at,
                    updated_at,
                    context_tokens,
                    total_tokens,
                    source_id,
                ),
            )
            conn.executemany(
                "INSERT INTO items (conversation_id, payload, created_at, meta) "
                "VALUES (?, ?, ?, ?)",
                [
                    (conversation_id, json.dumps(item), stamp, "{}")
                    for item, stamp in zip(items, stamps)
                ],
            )
        return conversation_id

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
        allowed = {"title", "model", "permission_mode", "workspace", "reasoning_effort"}
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

    def record_usage(
        self, conversation_id: str, context_tokens: int, spent_tokens: int
    ) -> None:
        """Store the latest context size and add to the running total.

        ``context_tokens`` is replaced (it describes the current history size,
        which does not accumulate) while ``spent_tokens`` is added to the
        total, so the conversation keeps a truthful lifetime cost.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET context_tokens = ?, "
                "total_tokens = total_tokens + ? WHERE id = ?",
                (int(context_tokens), int(spent_tokens), conversation_id),
            )

    def save_compaction(
        self, conversation_id: str, summary: str, compacted_item_count: int
    ) -> None:
        """Persist a private summary while retaining the full UI transcript."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET compaction_summary = ?, "
                "compacted_item_count = ?, context_tokens = 0, updated_at = ? "
                "WHERE id = ?",
                (summary, int(compacted_item_count), _now(), conversation_id),
            )

    # -- app state -----------------------------------------------------------

    def set_state(self, key: str, value: Any) -> None:
        """Persist a small JSON-serialisable value under ``key``."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), _now()),
            )

    def get_state(self, key: str) -> Any | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    # -- items ---------------------------------------------------------------

    def append_items(
        self,
        conversation_id: str,
        items: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not items:
            return
        timestamp = _now()
        encoded_meta = json.dumps(meta or {})
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO items (conversation_id, payload, created_at, meta) "
                "VALUES (?, ?, ?, ?)",
                [
                    (conversation_id, json.dumps(item), timestamp, encoded_meta)
                    for item in items
                ],
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (timestamp, conversation_id),
            )

    def load_items(self, conversation_id: str) -> list[dict[str, Any]]:
        """Just the payloads -- what the model gets sent as ``input``."""
        return [entry["payload"] for entry in self.load_entries(conversation_id)]

    def load_entries(self, conversation_id: str) -> list[dict[str, Any]]:
        """Payloads plus their stored timestamp and metadata, for the UI."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload, created_at, meta FROM items "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                continue  # Skip a corrupt row rather than breaking the whole load.
            try:
                meta = json.loads(row["meta"]) if row["meta"] else {}
            except ValueError:
                meta = {}
            entries.append(
                {
                    "payload": payload,
                    "created_at": row["created_at"],
                    "meta": meta if isinstance(meta, dict) else {},
                }
            )
        return entries

    def count_items(self, conversation_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row["n"]) if row else 0
