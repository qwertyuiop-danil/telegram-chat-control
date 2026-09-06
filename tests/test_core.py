from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from telegram_control import accounts, cli, client
from telegram_control import credentials as credentials_module
from telegram_control.credentials import Credentials
from telegram_control.services import launchd_plist, systemd_unit
from telegram_control.store import Store, decode_cursor


class TelegramStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        accounts.add(self.root, 100, "Owner", "main")
        self.store = Store(self.root, "100")
        with self.store.connect() as db:
            Store.upsert_peer(db, {"peer_id": 10, "title": "Project Group", "kind": "group", "username": "project", "member_count": 12, "role": "admin", "pinned": True, "last_message_at": "2026-01-02T03:04:05+00:00"})
            Store.upsert_peer(db, {"peer_id": 11, "title": "Bob", "kind": "personal", "last_message_at": "2026-01-02T06:00:00+00:00"})
            Store.upsert_peer(db, {"peer_id": 12, "title": "Helper Bot", "kind": "bot", "is_bot": True, "last_message_at": "2026-01-02T07:00:00+00:00"})
            Store.upsert_peer(db, {"peer_id": 13, "title": "Other Group", "kind": "group", "last_message_at": "2026-01-02T08:00:00+00:00"})
            Store.upsert_entity(db, {"entity_id": 200, "title": "Alice", "kind": "personal", "username": "alice"})
            Store.upsert_message(db, {"peer_id": 10, "message_id": 1, "sent_at": "2026-01-02T03:04:05+00:00", "sender_id": 200, "sender_name": "Wrong fallback", "body": "hello world", "direction": "incoming", "reactions_json": {"results": []}})
            Store.upsert_message(db, {"peer_id": 10, "message_id": 2, "sent_at": "2026-01-02T03:05:05+00:00", "sender_id": 100, "sender_name": "Owner", "body": "a very long outgoing message", "direction": "outgoing", "edited_at": "2026-01-02T03:06:05+00:00"})
            Store.upsert_message(db, {"peer_id": 11, "message_id": 1, "sent_at": "2026-01-02T06:00:00+00:00", "sender_id": 201, "sender_name": "Bob", "body": "private", "direction": "incoming"})
            Store.upsert_message(db, {"peer_id": 12, "message_id": 1, "sent_at": "2026-01-02T07:00:00+00:00", "sender_id": 202, "sender_name": "Helper Bot", "body": "bot", "direction": "outgoing"})
            Store.upsert_message(db, {"peer_id": 13, "message_id": 1, "sent_at": "2026-01-02T08:00:00+00:00", "sender_id": 203, "sender_name": "Other", "body": "other group", "direction": "incoming"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def message_args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "chat": None, "personal": False, "sender": None, "direction": None, "after": None, "before": None,
            "last_m": None, "last_h": None, "date": None, "order": "desc",
            "deleted": "any", "edited": False, "media": None, "topic": None, "reply_to": None,
            "reactions": False, "pinned": False, "cursor": None, "limit": 20, "full": False,
            "fields": None, "max_text_chars": 10, "format": "json",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def chat_args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "type": None, "visibility": None, "name": None, "username": None, "query": None,
            "unread": False, "archived": False, "muted": False, "pinned": False, "has_topics": False,
            "role": None, "member_min": None, "member_max": None, "last_m": None, "last_h": None,
            "date": None, "direction": None, "limit": 20, "full": False, "fields": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_message_contract_has_unambiguous_sender_and_direction(self) -> None:
        with patch.object(cli, "ROOT", self.root):
            result = cli.message_list(self.message_args(chat=10))
        first = result["items"][0]
        second = result["items"][1]
        self.assertEqual(first["direction"], "outgoing")
        self.assertTrue(first["sender"]["is_owner"])
        self.assertEqual(second["sender"]["name"], "Alice")
        self.assertEqual(second["chat"]["title"], "Project Group")
        self.assertTrue(first["text_truncated"])

    def test_deleted_message_keeps_body_and_search_index(self) -> None:
        with self.store.connect() as db:
            self.assertTrue(Store.mark_deleted(db, 10, 1, "audit"))
        with self.store.connect(write=False) as db:
            row = db.execute("SELECT body,is_deleted,deletion_source FROM messages WHERE peer_id=10 AND message_id=1").fetchone()
            match = db.execute("SELECT body FROM messages_fts WHERE messages_fts MATCH 'hello'").fetchone()
        self.assertEqual(row["body"], "hello world")
        self.assertEqual(row["is_deleted"], 1)
        self.assertEqual(row["deletion_source"], "audit")
        self.assertEqual(match["body"], "hello world")

    def test_unknown_deletion_marks_unique_message_and_records_ambiguous_event(self) -> None:
        with self.store.connect() as db:
            self.assertEqual(Store.mark_deleted_unknown_peer(db, 2, "event"), 1)
            Store.upsert_message(db, {"peer_id": 10, "message_id": 99, "body": "one"})
            Store.upsert_message(db, {"peer_id": 11, "message_id": 99, "body": "two"})
            self.assertEqual(Store.mark_deleted_unknown_peer(db, 99, "event"), 0)
            recorded = db.execute("SELECT note FROM deleted_events WHERE message_id=99").fetchone()
        self.assertEqual(recorded["note"], "ambiguous")

    def test_filters_cursor_and_field_projection(self) -> None:
        with patch.object(cli, "ROOT", self.root):
            result = cli.message_list(self.message_args(chat=10, direction="outgoing", fields="message_id,direction", limit=1))
            chats = cli.chat_list(argparse.Namespace(type="group", visibility=None, name=None, username=None, query=None, unread=False, archived=False, muted=False, pinned=True, has_topics=False, role="admin", member_min=10, member_max=20, limit=20, full=False, fields=None))
        self.assertEqual(result["items"], [{"message_id": 2, "direction": "outgoing"}])
        self.assertEqual(chats["items"][0]["chat_id"], 10)
        self.assertIsNone(result["meta"]["next_cursor"])
        self.assertIsNone(decode_cursor(None))

    def test_full_text_search_returns_deleted_messages_by_default(self) -> None:
        with self.store.connect() as db:
            Store.mark_deleted(db, 10, 1, "event")
        args = self.message_args(match="all", query="hello")
        with patch.object(cli, "ROOT", self.root):
            result = cli.message_list(args, search_query=args.query)
        self.assertEqual(result["items"][0]["message_id"], 1)
        self.assertTrue(result["items"][0]["deleted"]["value"])

    def test_relative_and_date_periods_validate_and_report_bounds(self) -> None:
        relative = cli.resolve_period(self.message_args(last_h=1, last_m=30))
        self.assertEqual(relative["source"], "relative")
        start = cli.datetime.fromisoformat(relative["start"])
        end = cli.datetime.fromisoformat(relative["end"])
        self.assertEqual((end - start).total_seconds(), 5400)
        calendar = cli.resolve_period(self.message_args(date="2026-01-02"))
        self.assertEqual(cli.datetime.fromisoformat(calendar["start"]).astimezone().date().isoformat(), "2026-01-02")
        with self.assertRaises(ValueError):
            cli.resolve_period(self.message_args(last_m=10, after="2026-01-01"))
        with self.assertRaises(ValueError):
            cli.resolve_period(self.message_args(last_h=0))

    def test_multi_chat_personal_selection_and_ascending_cursor(self) -> None:
        with self.store.connect() as db:
            Store.upsert_message(db, {"peer_id": 10, "message_id": 3, "sent_at": "2026-01-02T03:06:05+00:00", "body": "third", "direction": "incoming"})
        with patch.object(cli, "ROOT", self.root):
            selected = cli.message_list(self.message_args(chat=[10], personal=True, date="2026-01-02", order="asc"))
            first_page = cli.message_list(self.message_args(chat=[10], order="asc", limit=2))
            second_page = cli.message_list(self.message_args(chat=[10], order="asc", limit=2, cursor=first_page["meta"]["next_cursor"]))
        self.assertEqual({item["chat"]["chat_id"] for item in selected["items"]}, {10, 11, 12})
        self.assertEqual([item["message_id"] for item in first_page["items"]], [1, 2])
        self.assertEqual([item["message_id"] for item in second_page["items"]], [3])

    def test_period_chat_activity_is_direction_aware(self) -> None:
        with patch.object(cli, "ROOT", self.root):
            incoming = cli.chat_list(self.chat_args(date="2026-01-02", direction="incoming"))
            outgoing = cli.chat_list(self.chat_args(date="2026-01-02", direction="outgoing"))
            any_activity = cli.chat_list(self.chat_args(date="2026-01-02", direction="any"))
        self.assertEqual({item["chat_id"] for item in incoming["items"]}, {10, 11, 13})
        self.assertEqual({item["chat_id"] for item in outgoing["items"]}, {10, 12})
        self.assertEqual({item["chat_id"] for item in any_activity["items"]}, {10, 11, 12, 13})
        group = next(item for item in any_activity["items"] if item["chat_id"] == 10)
        self.assertEqual(group["activity"]["message_count"], 2)

    def test_migrates_v1_database_without_losing_rows(self) -> None:
        legacy_root = Path(tempfile.mkdtemp())
        try:
            destination = legacy_root / "data" / "accounts" / "7"
            destination.mkdir(parents=True)
            path = destination / "telegram.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE peers(peer_id INTEGER PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,username TEXT,is_private INTEGER NOT NULL,is_bot INTEGER NOT NULL,comment TEXT NOT NULL DEFAULT '',data_json TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE messages(peer_id INTEGER NOT NULL,message_id INTEGER NOT NULL,sent_at TEXT,sender_id INTEGER,sender_name TEXT,body TEXT NOT NULL DEFAULT '',reply_to_id INTEGER,edited_at TEXT,media_kind TEXT,voice_transcript TEXT,PRIMARY KEY(peer_id,message_id));
                CREATE VIRTUAL TABLE messages_fts USING fts5(peer_id UNINDEXED,message_id UNINDEXED,body);
                INSERT INTO peers VALUES(1,'Legacy','personal',NULL,1,0,'note','{}','now');
                INSERT INTO messages VALUES(1,4,'now',2,'Alice','saved text',NULL,NULL,NULL,NULL);
                INSERT INTO messages_fts VALUES(1,4,'saved text');
                """
            )
            connection.commit()
            connection.close()
            with Store(legacy_root, "7").connect(write=False) as db:
                row = db.execute("SELECT body,direction,is_deleted FROM messages WHERE peer_id=1 AND message_id=4").fetchone()
                comment = db.execute("SELECT comment FROM peers WHERE peer_id=1").fetchone()[0]
                found = db.execute("SELECT body FROM messages_fts WHERE messages_fts MATCH 'saved'").fetchone()[0]
            self.assertEqual((row["body"], row["direction"], row["is_deleted"]), ("saved text", "incoming", 0))
            self.assertEqual(comment, "note")
            self.assertEqual(found, "saved text")
        finally:
            import shutil
            shutil.rmtree(legacy_root)


class PlatformTests(unittest.TestCase):
    def test_telethon_proxy_comes_from_explicit_environment(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_CHAT_CONTROL_PROXY": "http://127.0.0.1:10811"}, clear=True):
            self.assertEqual(client.telegram_proxy(), {"proxy_type": "http", "addr": "127.0.0.1", "port": 10811, "rdns": True})

    def test_headless_linux_uses_private_credential_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(credentials_module.platform, "system", return_value="Linux"), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Credentials(Path(directory)).backend(), "private-file")

    def test_service_definitions_are_user_scoped_and_restartable(self) -> None:
        self.assertIn("LaunchAgents", str(Path.home() / "Library" / "LaunchAgents"))
        self.assertIn("KeepAlive", launchd_plist())
        unit = systemd_unit()
        self.assertIn("Restart=always", unit)
        self.assertIn("service run", unit)

    def test_private_file_credential_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credentials = Credentials(Path(directory))
            credentials._keyring_checked = True
            credentials._keyring = None
            credentials.put("test", "secret")
            self.assertEqual(credentials.get("test"), "secret")
            if os.name != "nt":
                self.assertEqual(oct(credentials.path.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
