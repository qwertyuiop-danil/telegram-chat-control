"""Telethon integration and normalized Telegram data ingestion."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, AsyncIterator
import webbrowser

from filelock import FileLock, Timeout

from . import accounts
from .credentials import API_HASH, API_ID, Credentials, CredentialError, migrate_legacy_macos, session_key
from .store import Store, now


class RPCError(Exception):
    """Fallback type used before Telethon is imported."""


def load_telethon() -> None:
    global TelegramClient, StringSession, RPCError, SessionPasswordNeededError, functions, types, utils, events
    if "TelegramClient" in globals():
        return
    from telethon import TelegramClient, events, functions, types, utils
    from telethon.errors import RPCError, SessionPasswordNeededError
    from telethon.sessions import StringSession


def dump(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return dump(value.to_dict())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"binary_bytes": len(value)}
    if isinstance(value, dict):
        return {str(key): dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [dump(item) for item in value]
    return value


def active_account(root: Path) -> dict[str, str]:
    return accounts.active(root)


def store(root: Path, account_id: str | None = None) -> Store:
    return Store(root, account_id or active_account(root)["id"])


def telegram_client(root: Path, account_id: str | None = None) -> Any:
    load_telethon()
    account_id = account_id or active_account(root)["id"]
    credentials = Credentials(root)
    migrate_legacy_macos(credentials, account_id)
    api_id = int(credentials.require(API_ID, "Telegram API ID"))
    api_hash = credentials.require(API_HASH, "Telegram API hash")
    session = credentials.require(session_key(account_id), "Telegram session")
    return TelegramClient(StringSession(session), api_id, api_hash, sequential_updates=True, catch_up=True)


async def assert_owner(root: Path, current: Any) -> None:
    expected = int(active_account(root)["id"])
    me = await current.get_me()
    if not me or int(me.id) != expected:
        raise RuntimeError("The connected Telegram session is not the configured owner account.")


def classification(entity: Any) -> tuple[str, bool, bool]:
    if isinstance(entity, types.User):
        return ("bot" if entity.bot else "personal", True, bool(entity.bot))
    if isinstance(entity, types.Channel):
        return ("channel" if entity.broadcast else "group", bool(entity.broadcast and not getattr(entity, "username", None)), False)
    return ("group", False, False)


def entity_row(entity: Any) -> dict[str, Any]:
    kind, _private, bot = classification(entity)
    return {
        "entity_id": utils.get_peer_id(entity),
        "title": utils.get_display_name(entity) or str(utils.get_peer_id(entity)),
        "kind": kind,
        "username": getattr(entity, "username", None),
        "is_bot": bot,
        "data_json": dump(entity),
        "updated_at": now(),
    }


def peer_row(dialog: Any, peer_id: int) -> dict[str, Any]:
    entity = dialog.entity
    kind, private, bot = classification(entity)
    last = getattr(dialog, "message", None)
    role = "owner" if getattr(entity, "creator", False) else "admin" if getattr(entity, "admin_rights", None) else "member"
    return {
        "peer_id": peer_id,
        "title": utils.get_display_name(entity) or str(peer_id),
        "kind": kind,
        "username": getattr(entity, "username", None),
        "is_private": private,
        "is_bot": bot,
        "data_json": dump(entity),
        "updated_at": now(),
        "unread_count": getattr(dialog, "unread_count", 0),
        "archived": bool(getattr(dialog, "archived", False) or getattr(dialog, "folder_id", None) == 1),
        "muted": bool(getattr(dialog, "mute_until", None)),
        "pinned": bool(getattr(dialog, "pinned", False)),
        "has_topics": bool(getattr(entity, "forum", False)),
        "member_count": getattr(entity, "participants_count", None),
        "role": role,
        "last_message_at": last.date.isoformat() if last and getattr(last, "date", None) else None,
    }


def media_kind(message: Any) -> str | None:
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "document", None):
        for attribute in message.document.attributes:
            if isinstance(attribute, types.DocumentAttributeAudio) and attribute.voice:
                return "voice"
            if isinstance(attribute, types.DocumentAttributeVideo):
                return "video"
        return "document"
    return type(message.media).__name__ if getattr(message, "media", None) else None


def message_row(peer_id: int, message: Any, owner_id: int, sender: Any | None = None) -> dict[str, Any]:
    reply = getattr(message, "reply_to", None)
    sender = sender or getattr(message, "sender", None)
    if getattr(message, "action", None):
        direction = "service"
    elif getattr(message, "post", False):
        direction = "channel_post"
    elif getattr(message, "out", False) or getattr(message, "sender_id", None) == owner_id:
        direction = "outgoing"
    else:
        direction = "incoming"
    return {
        "peer_id": peer_id,
        "message_id": message.id,
        "sent_at": message.date.isoformat() if getattr(message, "date", None) else None,
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": utils.get_display_name(sender) if sender else getattr(message, "post_author", None),
        "body": getattr(message, "message", None) or "",
        "reply_to_id": getattr(reply, "reply_to_msg_id", None),
        "edited_at": message.edit_date.isoformat() if getattr(message, "edit_date", None) else None,
        "media_kind": media_kind(message),
        "voice_transcript": getattr(message, "transcription", None),
        "direction": direction,
        "topic_id": getattr(reply, "reply_to_top_id", None),
        "reactions_json": dump(getattr(message, "reactions", None)),
        "is_pinned": bool(getattr(message, "pinned", False)),
        "forward_json": dump(getattr(message, "fwd_from", None)),
        "is_service": bool(getattr(message, "action", None)),
        "raw_json": dump(message),
    }


async def resolve(current: Any, peer: str | int) -> Any:
    value = int(peer) if str(peer).lstrip("-").isdigit() else peer
    return await current.get_entity(value)


async def _ingest_message(root: Path, db: Any, peer_id: int, message: Any, owner_id: int, sender: Any | None = None) -> None:
    sender = sender or getattr(message, "sender", None)
    if sender:
        Store.upsert_entity(db, entity_row(sender))
    Store.upsert_message(db, message_row(peer_id, message, owner_id, sender))


async def sync_index(root: Path, *, full: bool = False, current: Any | None = None, dialog_limit: int | None = None) -> dict[str, Any]:
    """Index current dialog state. A daemon passes its live client to avoid reconnects."""
    root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(root / "sync.lock"))
    try:
        with lock.acquire(timeout=0):
            account = active_account(root)
            own_client = current is None
            if own_client:
                current = telegram_client(root, account["id"])
                await current.connect()
            try:
                result = await _sync_index(root, current, int(account["id"]), full=full, dialog_limit=dialog_limit)
            finally:
                if own_client:
                    await current.disconnect()
            return result
    except Timeout:
        return {"status": "already_running", "mode": "full" if full else "incremental"}


async def _sync_index(root: Path, current: Any, owner_id: int, *, full: bool, dialog_limit: int | None) -> dict[str, Any]:
    local_store = store(root)
    chats = messages = history_requests = 0
    with local_store.connect() as db:
        if full:
            db.executescript("DELETE FROM messages_fts; DELETE FROM messages; DELETE FROM entities; DELETE FROM peers; DELETE FROM sync_state;")
        async for dialog in current.iter_dialogs(limit=dialog_limit):
            peer_id = utils.get_peer_id(dialog.entity)
            Store.upsert_peer(db, peer_row(dialog, peer_id))
            Store.upsert_entity(db, entity_row(dialog.entity))
            cursor = 0 if full else Store.cursor(db, peer_id)
            newest = cursor
            if full or getattr(dialog.message, "id", 0) > cursor:
                history_requests += 1
                async for message in current.iter_messages(dialog.entity, min_id=cursor, reverse=True):
                    await _ingest_message(root, db, peer_id, message, owner_id)
                    newest = max(newest, message.id)
                    messages += 1
                    if messages % 100 == 0:
                        db.commit()
            Store.set_cursor(db, peer_id, newest)
            db.commit()
            chats += 1
        Store.set_state(db, "last_sync", {"at": now(), "mode": "full" if full else "incremental", "chats": chats, "messages": messages})
    return {"status": "ok", "mode": "full" if full else "incremental", "chats": chats, "history_requests": history_requests, "messages": messages}


async def ingest_event(root: Path, event: Any) -> None:
    """Persist new and edited events while the daemon holds a live connection."""
    chat = await event.get_chat()
    if not chat:
        return
    message = event.message
    sender = await event.get_sender()
    peer_id = utils.get_peer_id(chat)
    owner_id = int(active_account(root)["id"])
    with store(root).connect() as db:
        Store.upsert_entity(db, entity_row(chat))
        # Events do not contain full dialog settings, so preserve existing peer metadata.
        existing = db.execute("SELECT * FROM peers WHERE peer_id=?", (peer_id,)).fetchone()
        if not existing:
            kind, private, bot = classification(chat)
            Store.upsert_peer(db, {"peer_id": peer_id, "title": utils.get_display_name(chat) or str(peer_id), "kind": kind,
                                   "username": getattr(chat, "username", None), "is_private": private, "is_bot": bot,
                                   "data_json": dump(chat), "updated_at": now()})
        await _ingest_message(root, db, peer_id, message, owner_id, sender)
        Store.set_cursor(db, peer_id, message.id)


async def handle_deleted(root: Path, event: Any) -> None:
    peer_id = getattr(event, "chat_id", None)
    with store(root).connect() as db:
        for message_id in event.deleted_ids:
            if peer_id is not None:
                Store.mark_deleted(db, int(peer_id), int(message_id), "event")
            else:
                Store.mark_deleted_unknown_peer(db, int(message_id), "event")


async def reconcile_deletions(root: Path, *, deep: bool = False, current: Any | None = None) -> dict[str, int]:
    """Verify saved message IDs without ever dropping their locally saved body."""
    account = active_account(root)
    own_client = current is None
    if own_client:
        current = telegram_client(root, account["id"])
        await current.connect()
    checked = deleted = chats = 0
    try:
        with store(root).connect(write=False) as db:
            rows = db.execute(
                """SELECT peer_id,kind,member_count FROM peers
                   WHERE kind='personal' OR (kind='group' AND member_count IS NOT NULL AND member_count <= 40)"""
            ).fetchall()
        for peer in rows:
            with store(root).connect(write=False) as db:
                limit = -1 if deep else 200
                ids = [int(row[0]) for row in db.execute(
                    "SELECT message_id FROM messages WHERE peer_id=? AND is_deleted=0 ORDER BY message_id DESC LIMIT ?",
                    (peer["peer_id"], limit),
                )]
            if not ids:
                continue
            try:
                entity = await resolve(current, int(peer["peer_id"]))
                fetched = await current.get_messages(entity, ids=ids)
            except RPCError:
                continue
            messages = fetched if isinstance(fetched, list) else [fetched]
            present = {int(message.id) for message in messages if message and getattr(message, "id", None)}
            with store(root).connect() as db:
                for message_id in ids:
                    checked += 1
                    if message_id not in present and Store.mark_deleted(db, int(peer["peer_id"]), message_id, "audit"):
                        deleted += 1
            chats += 1
        return {"chats": chats, "checked": checked, "deleted": deleted}
    finally:
        if own_client:
            await current.disconnect()


async def set_allowed(root: Path, peer: str | int, comment: str = "", *, remove: bool = False) -> dict[str, Any]:
    async with telegram_client(root) as current:
        await assert_owner(root, current)
        entity = await resolve(current, peer)
        peer_id = utils.get_peer_id(entity)
    with store(root).connect() as db:
        if remove:
            db.execute("DELETE FROM allowed_sends WHERE peer_id=?", (peer_id,))
        else:
            db.execute("INSERT INTO allowed_sends(peer_id,comment,added_at) VALUES(?,?,?) ON CONFLICT(peer_id) DO UPDATE SET comment=excluded.comment", (peer_id, comment, now()))
    return {"chat_id": peer_id, "allowed": not remove}


def allowed_list(root: Path) -> list[dict[str, Any]]:
    with store(root).connect(write=False) as db:
        return [dict(row) for row in db.execute(
            "SELECT a.peer_id AS chat_id,COALESCE(p.title,CAST(a.peer_id AS TEXT)) AS title,a.comment,a.added_at FROM allowed_sends a LEFT JOIN peers p ON p.peer_id=a.peer_id ORDER BY title"
        )]


async def send(root: Path, peer: str | int, text: str) -> dict[str, Any]:
    with store(root).connect(write=False) as db:
        allowed = db.execute("SELECT 1 FROM allowed_sends WHERE peer_id=?", (peer,)).fetchone()
    if not allowed:
        raise RuntimeError("Recipient is not in the owner-managed allowlist; no message was sent.")
    async with telegram_client(root) as current:
        await assert_owner(root, current)
        entity = await resolve(current, peer)
        message = await current.send_message(entity, text, link_preview=False)
    return {"status": "sent", "chat_id": int(peer), "message_id": message.id}


async def transcribe(root: Path, peer: int, message_id: int) -> dict[str, Any]:
    async with telegram_client(root) as current:
        entity = await resolve(current, peer)
        result = await current(functions.messages.TranscribeAudioRequest(entity, message_id))
    text = getattr(result, "text", None)
    if text:
        with store(root).connect() as db:
            db.execute("UPDATE messages SET voice_transcript=? WHERE peer_id=? AND message_id=?", (text, peer, message_id))
            Store.rebuild_fts(db)
    return {"chat_id": peer, "message_id": message_id, "pending": bool(getattr(result, "pending", False)), "text": text}


async def download_photo(root: Path, peer: int, message_id: int) -> dict[str, Any]:
    folder = root / "data" / "accounts" / active_account(root)["id"] / "media"
    folder.mkdir(parents=True, exist_ok=True)
    async with telegram_client(root) as current:
        entity = await resolve(current, peer)
        message = await current.get_messages(entity, ids=message_id)
        if not message or not message.photo:
            raise RuntimeError("This message does not contain a photo.")
        path = await current.download_media(message, file=folder / f"{peer}-{message_id}")
    return {"path": str(path), "chat_id": peer, "message_id": message_id}


def _profile_summary(entity: Any) -> dict[str, Any]:
    kind, _private, bot = classification(entity)
    return {
        "id": utils.get_peer_id(entity), "kind": kind, "title": utils.get_display_name(entity) or str(utils.get_peer_id(entity)),
        "username": getattr(entity, "username", None), "bot": bot, "verified": bool(getattr(entity, "verified", False)),
        "premium": bool(getattr(entity, "premium", False)), "scam": bool(getattr(entity, "scam", False)),
        "fake": bool(getattr(entity, "fake", False)), "member_count": getattr(entity, "participants_count", None),
    }


async def profile(root: Path, peer: str | int, *, refresh: bool = False, full: bool = False, section: str | None = None, photo: bool = False, limit: int = 20) -> dict[str, Any]:
    key = str(peer)
    with store(root).connect(write=False) as db:
        cached = db.execute("SELECT data_json,updated_at FROM profiles WHERE peer_key=?", (key,)).fetchone()
    if cached and not refresh and not full and not section and not photo and datetime.fromisoformat(cached["updated_at"]) > datetime.now(timezone.utc) - timedelta(hours=24):
        data = json.loads(cached["data_json"])
        return {"source": "cache", **data}
    async with telegram_client(root) as current:
        entity = await resolve(current, peer)
        data: dict[str, Any] = {"profile": _profile_summary(entity)}
        detailed: Any | None = None
        try:
            if isinstance(entity, types.User):
                detailed = await current(functions.users.GetFullUserRequest(entity))
                full_user = getattr(detailed, "full_user", None)
                data["profile"]["about"] = getattr(full_user, "about", None)
                data["profile"]["phone"] = getattr(entity, "phone", None)
                if section == "gifts":
                    data["gifts"] = dump(await current(functions.payments.GetSavedStarGiftsRequest(entity, "", limit)))
            elif isinstance(entity, types.Channel):
                detailed = await current(functions.channels.GetFullChannelRequest(entity))
                full_chat = getattr(detailed, "full_chat", None)
                data["profile"]["about"] = getattr(full_chat, "about", None)
                data["profile"]["member_count"] = getattr(full_chat, "participants_count", data["profile"]["member_count"])
                if section == "admins":
                    admins = []
                    async for participant in current.iter_participants(entity, filter=types.ChannelParticipantsAdmins, limit=limit):
                        admins.append(_profile_summary(participant))
                    data["admins"] = admins
            if photo:
                folder = root / "data" / "accounts" / active_account(root)["id"] / "profile-photos"
                folder.mkdir(parents=True, exist_ok=True)
                data["profile"]["photo_path"] = await current.download_profile_photo(entity, file=folder / str(data["profile"]["id"]))
            if full or section in {"permissions", "raw"}:
                data["details"] = dump(detailed) if detailed else dump(entity)
            if section == "raw":
                data["raw"] = dump(entity)
        except RPCError as error:
            data["details_unavailable"] = type(error).__name__
    with store(root).connect() as db:
        db.execute("INSERT INTO profiles(peer_key,data_json,updated_at) VALUES(?,?,?) ON CONFLICT(peer_key) DO UPDATE SET data_json=excluded.data_json,updated_at=excluded.updated_at", (key, json.dumps(data, ensure_ascii=False), now()))
    return {"source": "telegram_api", **data}


async def login_qr(root: Path, *, api_id: str | None = None, api_hash: str | None = None, comment: str = "") -> dict[str, Any]:
    load_telethon()
    import qrcode

    credentials = Credentials(root)
    if not api_id and not credentials.get(API_ID):
        api_id = input("Telegram API ID: ").strip()
    if not api_hash and not credentials.get(API_HASH):
        import getpass

        api_hash = getpass.getpass("Telegram API hash: ")
    if api_id:
        credentials.put(API_ID, api_id)
    if api_hash:
        credentials.put(API_HASH, api_hash)
    numeric_api_id = int(credentials.require(API_ID, "Telegram API ID"))
    hash_value = credentials.require(API_HASH, "Telegram API hash")
    root.mkdir(parents=True, exist_ok=True)
    qr_path = root / "telegram-login-qr.png"
    current = TelegramClient(StringSession(), numeric_api_id, hash_value)
    try:
        await current.connect()
        qr_login = await current.qr_login()
        qrcode.make(qr_login.url).save(qr_path)
        if qr_path.exists() and __import__("os").name != "nt":
            __import__("os").chmod(qr_path, 0o600)
        webbrowser.open(qr_path.resolve().as_uri())
        try:
            await qr_login.wait()
        except SessionPasswordNeededError:
            import getpass

            await current.sign_in(password=getpass.getpass("Telegram cloud password: "))
        me = await current.get_me()
        if not me:
            raise RuntimeError("QR login did not return an account.")
        account_id = int(me.id)
        credentials.put(session_key(account_id), current.session.save())
        account = accounts.add(root, account_id, utils.get_display_name(me) or str(account_id), comment)
        return {
            "status": "connected",
            "account": account,
            "credentials": credentials.status(),
            "next_step": "Run `service install` to enable five-second background synchronization.",
        }
    finally:
        await current.disconnect()
        qr_path.unlink(missing_ok=True)
