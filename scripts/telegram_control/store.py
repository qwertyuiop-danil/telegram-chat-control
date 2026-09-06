"""SQLite index, migrations, and local read models for Telegram."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 2


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_value(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


class Store:
    """One account-scoped local index.

    Database rows keep the original message body forever. Deletion is a state
    transition, never a destructive operation.
    """

    def __init__(self, root: Path, account_id: str | int) -> None:
        self.root = root
        self.account_id = str(account_id)
        self.directory = root / "data" / "accounts" / self.account_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "telegram.sqlite3"

    @contextmanager
    def connect(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        created = not self.path.exists()
        if created:
            self.path.touch(mode=0o600, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            self._initialize(connection)
            connection.commit()
            yield connection
            if write:
                connection.commit()
            else:
                connection.rollback()
        finally:
            connection.close()

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _add_column(cls, connection: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in cls._columns(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    @classmethod
    def _initialize(cls, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS peers (
              peer_id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
              username TEXT, is_private INTEGER NOT NULL DEFAULT 0, is_bot INTEGER NOT NULL DEFAULT 0,
              comment TEXT NOT NULL DEFAULT '', data_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entities (
              entity_id INTEGER PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
              username TEXT, is_bot INTEGER NOT NULL DEFAULT 0, data_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS deleted_events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, peer_id INTEGER,
              received_at TEXT NOT NULL, resolved_at TEXT, note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS service_state (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
              USING fts5(peer_id UNINDEXED, message_id UNINDEXED, body);
            """
        )
        peer_columns = {
            "unread_count INTEGER NOT NULL DEFAULT 0",
            "archived INTEGER NOT NULL DEFAULT 0",
            "muted INTEGER NOT NULL DEFAULT 0",
            "pinned INTEGER NOT NULL DEFAULT 0",
            "has_topics INTEGER NOT NULL DEFAULT 0",
            "member_count INTEGER",
            "role TEXT",
            "last_message_at TEXT",
        }
        for definition in peer_columns:
            cls._add_column(connection, "peers", definition)
        message_columns = {
            "direction TEXT NOT NULL DEFAULT 'incoming'",
            "is_deleted INTEGER NOT NULL DEFAULT 0",
            "deleted_at TEXT",
            "deletion_source TEXT",
            "topic_id INTEGER",
            "reactions_json TEXT NOT NULL DEFAULT '{}'",
            "is_pinned INTEGER NOT NULL DEFAULT 0",
            "forward_json TEXT NOT NULL DEFAULT '{}'",
            "is_service INTEGER NOT NULL DEFAULT 0",
            "raw_json TEXT NOT NULL DEFAULT '{}'",
        }
        for definition in message_columns:
            cls._add_column(connection, "messages", definition)
        row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        version = int(row[0]) if row else 0
        if not row:
            connection.execute("INSERT INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,))
        elif version < SCHEMA_VERSION:
            connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
        if version < 2:
            cls.rebuild_fts(connection)

    @staticmethod
    def rebuild_fts(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM messages_fts")
        connection.execute(
            """INSERT INTO messages_fts(peer_id,message_id,body)
               SELECT peer_id,message_id,trim(body || ' ' || COALESCE(voice_transcript,''))
               FROM messages WHERE trim(body || ' ' || COALESCE(voice_transcript,'')) != ''"""
        )

    @staticmethod
    def upsert_peer(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
        values = {
            "peer_id": row["peer_id"], "title": row.get("title") or str(row["peer_id"]),
            "kind": row.get("kind") or "group", "username": row.get("username"),
            "is_private": int(bool(row.get("is_private"))), "is_bot": int(bool(row.get("is_bot"))),
            "data_json": json_value(row.get("data_json")), "updated_at": row.get("updated_at") or now(),
            "unread_count": int(row.get("unread_count") or 0), "archived": int(bool(row.get("archived"))),
            "muted": int(bool(row.get("muted"))), "pinned": int(bool(row.get("pinned"))),
            "has_topics": int(bool(row.get("has_topics"))), "member_count": row.get("member_count"),
            "role": row.get("role"), "last_message_at": row.get("last_message_at"),
        }
        connection.execute(
            """
            INSERT INTO peers(peer_id,title,kind,username,is_private,is_bot,comment,data_json,updated_at,
                              unread_count,archived,muted,pinned,has_topics,member_count,role,last_message_at)
            VALUES(:peer_id,:title,:kind,:username,:is_private,:is_bot,
                   COALESCE((SELECT comment FROM peers WHERE peer_id=:peer_id),''),:data_json,:updated_at,
                   :unread_count,:archived,:muted,:pinned,:has_topics,:member_count,:role,:last_message_at)
            ON CONFLICT(peer_id) DO UPDATE SET
              title=excluded.title,kind=excluded.kind,username=excluded.username,
              is_private=excluded.is_private,is_bot=excluded.is_bot,data_json=excluded.data_json,
              updated_at=excluded.updated_at,unread_count=excluded.unread_count,archived=excluded.archived,
              muted=excluded.muted,pinned=excluded.pinned,has_topics=excluded.has_topics,
              member_count=excluded.member_count,role=excluded.role,last_message_at=excluded.last_message_at
            """, values,
        )

    @staticmethod
    def upsert_entity(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
        if row.get("entity_id") is None:
            return
        connection.execute(
            """
            INSERT INTO entities(entity_id,title,kind,username,is_bot,data_json,updated_at)
            VALUES(:entity_id,:title,:kind,:username,:is_bot,:data_json,:updated_at)
            ON CONFLICT(entity_id) DO UPDATE SET title=excluded.title,kind=excluded.kind,
              username=excluded.username,is_bot=excluded.is_bot,data_json=excluded.data_json,updated_at=excluded.updated_at
            """,
            {
                "entity_id": row["entity_id"], "title": row.get("title") or str(row["entity_id"]),
                "kind": row.get("kind") or "unknown", "username": row.get("username"),
                "is_bot": int(bool(row.get("is_bot"))), "data_json": json_value(row.get("data_json")),
                "updated_at": row.get("updated_at") or now(),
            },
        )

    @classmethod
    def upsert_message(cls, connection: sqlite3.Connection, row: dict[str, Any]) -> None:
        values = {
            "peer_id": row["peer_id"], "message_id": row["message_id"], "sent_at": row.get("sent_at"),
            "sender_id": row.get("sender_id"), "sender_name": row.get("sender_name"), "body": row.get("body") or "",
            "reply_to_id": row.get("reply_to_id"), "edited_at": row.get("edited_at"), "media_kind": row.get("media_kind"),
            "voice_transcript": row.get("voice_transcript"), "direction": row.get("direction") or "incoming",
            "is_deleted": int(bool(row.get("is_deleted"))), "deleted_at": row.get("deleted_at"),
            "deletion_source": row.get("deletion_source"), "topic_id": row.get("topic_id"),
            "reactions_json": json_value(row.get("reactions_json")), "is_pinned": int(bool(row.get("is_pinned"))),
            "forward_json": json_value(row.get("forward_json")), "is_service": int(bool(row.get("is_service"))),
            "raw_json": json_value(row.get("raw_json")),
        }
        connection.execute("DELETE FROM messages_fts WHERE peer_id=? AND message_id=?", (values["peer_id"], values["message_id"]))
        connection.execute(
            """
            INSERT INTO messages(peer_id,message_id,sent_at,sender_id,sender_name,body,reply_to_id,edited_at,media_kind,
              voice_transcript,direction,is_deleted,deleted_at,deletion_source,topic_id,reactions_json,is_pinned,
              forward_json,is_service,raw_json)
            VALUES(:peer_id,:message_id,:sent_at,:sender_id,:sender_name,:body,:reply_to_id,:edited_at,:media_kind,
              :voice_transcript,:direction,:is_deleted,:deleted_at,:deletion_source,:topic_id,:reactions_json,:is_pinned,
              :forward_json,:is_service,:raw_json)
            ON CONFLICT(peer_id,message_id) DO UPDATE SET
              sent_at=excluded.sent_at,sender_id=COALESCE(excluded.sender_id,messages.sender_id),
              sender_name=COALESCE(excluded.sender_name,messages.sender_name),body=excluded.body,
              reply_to_id=excluded.reply_to_id,edited_at=excluded.edited_at,media_kind=excluded.media_kind,
              voice_transcript=COALESCE(excluded.voice_transcript,messages.voice_transcript),
              direction=excluded.direction,topic_id=excluded.topic_id,reactions_json=excluded.reactions_json,
              is_pinned=excluded.is_pinned,forward_json=excluded.forward_json,is_service=excluded.is_service,
              raw_json=excluded.raw_json
            """, values,
        )
        searchable = " ".join(part for part in (values["body"], values["voice_transcript"] or "") if part).strip()
        if searchable:
            connection.execute("INSERT INTO messages_fts(peer_id,message_id,body) VALUES(?,?,?)", (values["peer_id"], values["message_id"], searchable))

    @staticmethod
    def mark_deleted(connection: sqlite3.Connection, peer_id: int, message_id: int, source: str, when: str | None = None) -> bool:
        changed = connection.execute(
            """UPDATE messages SET is_deleted=1,deleted_at=COALESCE(deleted_at,?),
               deletion_source=COALESCE(deletion_source,?) WHERE peer_id=? AND message_id=?""",
            (when or now(), source, peer_id, message_id),
        ).rowcount
        return bool(changed)

    @classmethod
    def mark_deleted_unknown_peer(cls, connection: sqlite3.Connection, message_id: int, source: str) -> int:
        peers = connection.execute("SELECT peer_id FROM messages WHERE message_id=? AND is_deleted=0", (message_id,)).fetchall()
        if len(peers) == 1:
            cls.mark_deleted(connection, int(peers[0]["peer_id"]), message_id, source)
            return 1
        connection.execute("INSERT INTO deleted_events(message_id,peer_id,received_at,note) VALUES(?,?,?,?)", (message_id, None, now(), "ambiguous" if peers else "not-indexed"))
        return 0

    @staticmethod
    def cursor(connection: sqlite3.Connection, peer_id: int) -> int:
        row = connection.execute("SELECT max_message_id FROM sync_state WHERE peer_id=?", (peer_id,)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def set_cursor(connection: sqlite3.Connection, peer_id: int, message_id: int, synced_at: str | None = None) -> None:
        connection.execute(
            """INSERT INTO sync_state(peer_id,max_message_id,synced_at) VALUES(?,?,?)
               ON CONFLICT(peer_id) DO UPDATE SET max_message_id=MAX(max_message_id,excluded.max_message_id),synced_at=excluded.synced_at""",
            (peer_id, message_id, synced_at or now()),
        )

    @staticmethod
    def set_state(connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            """INSERT INTO service_state(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, json_value(value), now()),
        )

    @staticmethod
    def state(connection: sqlite3.Connection) -> dict[str, Any]:
        return {row["key"]: {"value": json.loads(row["value"]), "updated_at": row["updated_at"]} for row in connection.execute("SELECT key,value,updated_at FROM service_state")}


def encode_cursor(sent_at: str | None, peer_id: int, message_id: int) -> str:
    payload = json.dumps([sent_at or "", peer_id, message_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(value: str | None) -> tuple[str, int, int] | None:
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        sent_at, peer_id, message_id = json.loads(decoded)
        return str(sent_at), int(peer_id), int(message_id)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid cursor.") from error


def row_dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
