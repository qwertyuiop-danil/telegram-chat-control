"""Local SQLite store."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


class Store:
    def __init__(self, root: Path, account_id: str | None = None) -> None:
        self.directory = root / "data" / "accounts" / str(account_id) if account_id else root / "data"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "telegram.sqlite3"

    @contextmanager
    def connect(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        created = not self.path.exists()
        if created:
            self.path.touch(mode=0o600)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            if write or created:
                self._initialize(connection)
            yield connection
            if write:
                connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS peers (
              peer_id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
              username TEXT, is_private INTEGER NOT NULL, is_bot INTEGER NOT NULL,
              comment TEXT NOT NULL DEFAULT '', data_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sent_at TEXT,
              sender_id INTEGER, sender_name TEXT, body TEXT NOT NULL DEFAULT '', reply_to_id INTEGER,
              edited_at TEXT, media_kind TEXT, voice_transcript TEXT,
              PRIMARY KEY(peer_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS sync_state (
              peer_id INTEGER PRIMARY KEY, max_message_id INTEGER NOT NULL DEFAULT 0, synced_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS allowed_sends (
              peer_id INTEGER PRIMARY KEY, comment TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
              peer_key TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
              USING fts5(peer_id UNINDEXED, message_id UNINDEXED, body);
            """
        )
        if "sender_name" not in {row[1] for row in connection.execute("PRAGMA table_info(messages)")}:
            connection.execute("ALTER TABLE messages ADD COLUMN sender_name TEXT")

    @staticmethod
    def upsert_peer(connection: sqlite3.Connection, row: dict) -> None:
        connection.execute(
            """
            INSERT INTO peers(peer_id,title,kind,username,is_private,is_bot,comment,data_json,updated_at)
            VALUES(:peer_id,:title,:kind,:username,:is_private,:is_bot,
                   COALESCE((SELECT comment FROM peers WHERE peer_id=:peer_id),''),:data_json,:updated_at)
            ON CONFLICT(peer_id) DO UPDATE SET
              title=excluded.title,kind=excluded.kind,username=excluded.username,
              is_private=excluded.is_private,is_bot=excluded.is_bot,data_json=excluded.data_json,updated_at=excluded.updated_at
            """,
            row,
        )

    @staticmethod
    def upsert_message(connection: sqlite3.Connection, row: dict) -> None:
        connection.execute("DELETE FROM messages_fts WHERE peer_id=? AND message_id=?", (row["peer_id"], row["message_id"]))
        connection.execute(
            """
            INSERT INTO messages(peer_id,message_id,sent_at,sender_id,sender_name,body,reply_to_id,edited_at,media_kind,voice_transcript)
            VALUES(:peer_id,:message_id,:sent_at,:sender_id,:sender_name,:body,:reply_to_id,:edited_at,:media_kind,:voice_transcript)
            ON CONFLICT(peer_id,message_id) DO UPDATE SET
              sent_at=excluded.sent_at,sender_id=excluded.sender_id,sender_name=COALESCE(excluded.sender_name,messages.sender_name),body=excluded.body,
              reply_to_id=excluded.reply_to_id,edited_at=excluded.edited_at,media_kind=excluded.media_kind,
              voice_transcript=COALESCE(messages.voice_transcript, excluded.voice_transcript)
            """,
            row,
        )
        if row["body"]:
            connection.execute("INSERT INTO messages_fts(peer_id,message_id,body) VALUES(?,?,?)", (row["peer_id"], row["message_id"], row["body"]))

    @staticmethod
    def cursor(connection: sqlite3.Connection, peer_id: int) -> int:
        row = connection.execute("SELECT max_message_id FROM sync_state WHERE peer_id=?", (peer_id,)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def set_cursor(connection: sqlite3.Connection, peer_id: int, message_id: int, synced_at: str) -> None:
        connection.execute(
            """
            INSERT INTO sync_state(peer_id,max_message_id,synced_at) VALUES(?,?,?)
            ON CONFLICT(peer_id) DO UPDATE SET max_message_id=MAX(max_message_id,excluded.max_message_id),synced_at=excluded.synced_at
            """,
            (peer_id, message_id, synced_at),
        )
