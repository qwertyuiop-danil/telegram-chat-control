# /// script
# requires-python = ">=3.10"
# dependencies = ["telethon>=1.42,<2", "qrcode[pil]>=8,<9"]
# ///
"""Local Telegram reader with owner-gated send permissions.

Run with: uv run scripts/telegram.py <command>
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

import accounts
import keychain
from store import Store

ROOT = Path(os.environ.get("TELEGRAM_CHAT_CONTROL_HOME", Path.home() / ".codex" / "telegram-chat-control"))


class RPCError(Exception):
    pass


def load_telethon() -> None:
    global TelegramClient, StringSession, RPCError, SessionPasswordNeededError, functions, types, utils
    if "TelegramClient" in globals():
        return
    from telethon import TelegramClient, functions, types, utils
    from telethon.errors import RPCError, SessionPasswordNeededError
    from telethon.sessions import StringSession


def stamp() -> str:
    return datetime.now(UTC).isoformat()


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


def print_json(value: Any) -> None:
    print(json.dumps(dump(value), ensure_ascii=False, indent=2), flush=True)


def active_account() -> dict[str, str]:
    return accounts.active(ROOT)


def store() -> Store:
    return Store(ROOT, active_account()["id"])


def account_directory() -> Path:
    directory = ROOT / "data" / "accounts" / active_account()["id"]
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def client() -> TelegramClient:
    load_telethon()
    api_id = int(keychain.require(keychain.API_ID, "Telegram API ID"))
    api_hash = keychain.require(keychain.API_HASH, "Telegram API hash")
    session = keychain.require(keychain.session_service(active_account()["id"]), "Telegram session; create it with login-qr")
    return TelegramClient(StringSession(session), api_id, api_hash)


async def assert_owner(current: TelegramClient) -> None:
    expected = int(active_account()["id"])
    me = await current.get_me()
    if me.id != expected:
        raise RuntimeError("This Telegram session is not the configured owner account; sending and permission changes are disabled.")


def classification(entity: Any) -> tuple[str, int, int]:
    if isinstance(entity, types.User):
        return ("bot" if entity.bot else "personal", 1, int(bool(entity.bot)))
    if isinstance(entity, types.Channel):
        if entity.broadcast:
            return "channel", int(not bool(entity.username or getattr(entity, "usernames", None))), 0
        return "group", 0, 0
    return "group", 0, 0


def peer_row(dialog: Any, peer_id: int) -> dict[str, Any]:
    entity = dialog.entity
    kind, private, bot = classification(entity)
    return {
        "peer_id": peer_id,
        "title": utils.get_display_name(entity) or str(peer_id),
        "kind": kind,
        "username": getattr(entity, "username", None),
        "is_private": private,
        "is_bot": bot,
        "data_json": json.dumps(dump(entity), ensure_ascii=False),
        "updated_at": stamp(),
    }


def media_kind(message: Any) -> str | None:
    if message.photo:
        return "photo"
    if message.document:
        for attribute in message.document.attributes:
            if isinstance(attribute, types.DocumentAttributeAudio) and attribute.voice:
                return "voice"
        return "document"
    return type(message.media).__name__ if message.media else None


def message_row(peer_id: int, message: Any) -> dict[str, Any]:
    reply = getattr(message, "reply_to", None)
    sender = getattr(message, "sender", None)
    return {
        "peer_id": peer_id,
        "message_id": message.id,
        "sent_at": message.date.isoformat() if message.date else None,
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": utils.get_display_name(sender) if sender else getattr(message, "post_author", None),
        "body": message.message or "",
        "reply_to_id": getattr(reply, "reply_to_msg_id", None),
        "edited_at": message.edit_date.isoformat() if message.edit_date else None,
        "media_kind": media_kind(message),
        "voice_transcript": getattr(message, "transcription", None),
    }


async def sync(full: bool = False) -> dict[str, int | str]:
    ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = ROOT / "sync.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "mode": "full" if full else "incremental"}
        return await _sync(full)


async def _sync(full: bool = False) -> dict[str, int | str]:
    current_account = active_account()
    local_store = store()
    chats = messages = history_requests = 0
    with local_store.connect() as db:
        if full:
            db.executescript("DELETE FROM messages_fts; DELETE FROM messages; DELETE FROM peers; DELETE FROM sync_state;")
        async with client() as current:
            async for dialog in current.iter_dialogs():
                peer_id = utils.get_peer_id(dialog.entity)
                Store.upsert_peer(db, peer_row(dialog, peer_id))
                cursor = 0 if full else Store.cursor(db, peer_id)
                newest = cursor
                if full or getattr(dialog.message, "id", 0) > cursor:
                    history_requests += 1
                    async for message in current.iter_messages(dialog.entity, min_id=cursor, reverse=True):
                        Store.upsert_message(db, message_row(peer_id, message))
                        newest = max(newest, message.id)
                        messages += 1
                        if messages % 100 == 0:
                            db.commit()
                Store.set_cursor(db, peer_id, newest, stamp())
                # Keep the first account import observable instead of committing
                # tens of thousands of messages only after every dialog finishes.
                db.commit()
                chats += 1
    return {"status": "ok", "account_id": current_account["id"], "mode": "full" if full else "incremental", "chats": chats, "history_requests": history_requests, "messages": messages}


def filters(value: str) -> tuple[str, tuple[Any, ...]]:
    if value == "all":
        return "1=1", ()
    if value == "personal":
        return "1=1", ()
    if value == "groups":
        return "kind='group'", ()
    if value == "channels":
        return "kind='channel'", ()
    if value == "private-channels":
        return "kind='channel' AND is_private=1", ()
    raise ValueError("Unknown filter.")


def list_peers(args: argparse.Namespace) -> list[dict[str, Any]]:
    condition, parameters = filters(args.filter)
    if args.filter == "personal":
        condition += " AND EXISTS (SELECT 1 FROM messages mine WHERE mine.peer_id=p.peer_id AND mine.sender_id=?)"
        parameters += (int(active_account()["id"]),)
    query = (args.query or "").strip()
    if query:
        condition += " AND (title LIKE ? OR username LIKE ? OR comment LIKE ?)"
        parameters += tuple(f"%{query}%" for _ in range(3))
    with store().connect(write=False) as db:
        rows = db.execute(
            f"""
            SELECT p.peer_id,p.title,p.kind,p.username,p.is_private,p.is_bot,p.comment,
                   COUNT(m.message_id) AS messages,MAX(m.sent_at) AS latest
            FROM peers p LEFT JOIN messages m ON p.peer_id=m.peer_id
            WHERE {condition} GROUP BY p.peer_id ORDER BY latest DESC NULLS LAST LIMIT ?
            """,
            (*parameters, args.limit),
        ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["display_title"] = f"{row['title']} — {row['comment']}" if row["comment"] else row["title"]
    return result


def read_messages(args: argparse.Namespace) -> list[dict[str, Any]]:
    where = ["m.peer_id=?"]
    parameters: list[Any] = [args.peer]
    if args.after:
        where.append("m.sent_at>=?")
        parameters.append(args.after)
    if args.before:
        where.append("m.sent_at<=?")
        parameters.append(args.before)
    if args.min_id is not None:
        where.append("m.message_id>=?")
        parameters.append(args.min_id)
    if args.max_id is not None:
        where.append("m.message_id<=?")
        parameters.append(args.max_id)
    with store().connect(write=False) as db:
        rows = db.execute(
            f"SELECT m.peer_id,m.message_id,m.sent_at,m.sender_id,COALESCE(m.sender_name,p.title,'ID ' || m.sender_id) AS sender_name,m.body,m.reply_to_id,m.edited_at,m.media_kind,m.voice_transcript FROM messages m LEFT JOIN peers p ON p.peer_id=m.sender_id WHERE {' AND '.join(where)} ORDER BY m.message_id DESC LIMIT ?",
            (*parameters, args.limit),
        ).fetchall()
    return [dict(row) for row in rows]


def print_chat(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        time = datetime.fromisoformat(message["sent_at"]).astimezone().strftime("%Y-%m-%d %H:%M") if message["sent_at"] else "—"
        sender = message["sender_name"] or "Неизвестный отправитель"
        text = (message["body"] or f"[{message['media_kind'] or 'сообщение без текста'}]").replace("\n", " ↵ ")
        print(f"{time} | {sender} | {text}")


def search_messages(args: argparse.Namespace) -> list[dict[str, Any]]:
    terms = re.findall(r"[\w-]+", args.query, flags=re.UNICODE)
    if not terms:
        return []
    expression = " AND ".join(f'"{term}"' for term in terms)
    with store().connect(write=False) as db:
        rows = db.execute(
            """
            SELECT f.peer_id,p.title,f.message_id,m.sent_at,m.sender_id,m.body,m.voice_transcript
            FROM messages_fts f JOIN messages m ON m.peer_id=f.peer_id AND m.message_id=f.message_id
            JOIN peers p ON p.peer_id=f.peer_id WHERE messages_fts MATCH ? AND (? IS NULL OR f.peer_id=?)
            ORDER BY m.sent_at DESC LIMIT ?
            """,
            (expression, args.peer, args.peer, args.limit),
        ).fetchall()
    return [dict(row) for row in rows]


async def resolve(current: TelegramClient, peer: str) -> Any:
    return await current.get_entity(int(peer) if peer.lstrip("-").isdigit() else peer)


def set_comment(args: argparse.Namespace) -> dict[str, Any]:
    with store().connect() as db:
        changed = db.execute("UPDATE peers SET comment=? WHERE peer_id=?", (args.text, args.peer)).rowcount
    if not changed:
        raise RuntimeError("Chat is not indexed; run sync first.")
    return {"peer_id": args.peer, "comment": args.text}


async def set_allowed(args: argparse.Namespace, remove: bool = False) -> dict[str, Any]:
    async with client() as current:
        await assert_owner(current)
        entity = await resolve(current, str(args.peer))
        peer_id = utils.get_peer_id(entity)
    with store().connect() as db:
        if remove:
            db.execute("DELETE FROM allowed_sends WHERE peer_id=?", (peer_id,))
        else:
            db.execute(
                "INSERT INTO allowed_sends(peer_id,comment,added_at) VALUES(?,?,?) ON CONFLICT(peer_id) DO UPDATE SET comment=excluded.comment",
                (peer_id, args.comment or "", stamp()),
            )
    return {"peer_id": peer_id, "allowed": not remove}


def allowed_list() -> list[dict[str, Any]]:
    with store().connect(write=False) as db:
        rows = db.execute(
            "SELECT a.peer_id,COALESCE(p.title,CAST(a.peer_id AS TEXT)) AS title,a.comment,a.added_at FROM allowed_sends a LEFT JOIN peers p ON p.peer_id=a.peer_id ORDER BY title"
        ).fetchall()
    return [dict(row) for row in rows]


async def send(args: argparse.Namespace) -> dict[str, Any]:
    with store().connect(write=False) as db:
        allowed = db.execute("SELECT 1 FROM allowed_sends WHERE peer_id=?", (args.peer,)).fetchone()
    if not allowed:
        raise RuntimeError("Recipient is not in the owner-managed allowlist; message was not sent.")
    async with client() as current:
        await assert_owner(current)
        entity = await resolve(current, str(args.peer))
        message = await current.send_message(entity, args.text, link_preview=False)
    return {"status": "sent", "peer_id": args.peer, "message_id": message.id}


async def transcribe(args: argparse.Namespace) -> dict[str, Any]:
    async with client() as current:
        entity = await resolve(current, str(args.peer))
        result = await current(functions.messages.TranscribeAudioRequest(entity, args.message))
    text = getattr(result, "text", None)
    pending = bool(getattr(result, "pending", False))
    with store().connect() as db:
        if text:
            db.execute("UPDATE messages SET voice_transcript=? WHERE peer_id=? AND message_id=?", (text, args.peer, args.message))
    return {"peer_id": args.peer, "message_id": args.message, "pending": pending, "text": text, "trial_remaining": getattr(result, "trial_remains_num", None)}


async def photo(args: argparse.Namespace) -> dict[str, Any]:
    output = account_directory() / "media"
    output.mkdir(parents=True, exist_ok=True)
    async with client() as current:
        entity = await resolve(current, str(args.peer))
        message = await current.get_messages(entity, ids=args.message)
        if not message or not message.photo:
            raise RuntimeError("This message does not contain a photo.")
        path = await current.download_media(message, file=output / f"{args.peer}-{args.message}")
    if args.open:
        subprocess.run(["open", str(path)], check=False)
    return {"path": str(path), "peer_id": args.peer, "message_id": args.message}


async def profile(args: argparse.Namespace) -> dict[str, Any]:
    key = str(args.peer)
    with store().connect(write=False) as db:
        cached = db.execute("SELECT data_json,updated_at FROM profiles WHERE peer_key=?", (key,)).fetchone()
    if cached and not args.refresh and datetime.fromisoformat(cached["updated_at"]) > datetime.now(UTC) - timedelta(hours=24):
        return {"source": "database", **json.loads(cached["data_json"])}
    async with client() as current:
        entity = await resolve(current, key)
        data: dict[str, Any] = {"id": utils.get_peer_id(entity), "entity": dump(entity)}
        try:
            if isinstance(entity, types.User):
                full = await current(functions.users.GetFullUserRequest(entity))
                data["full"] = dump(full)
                try:
                    gifts = await current(functions.payments.GetSavedStarGiftsRequest(entity, "", 100))
                    data["gifts"] = dump(gifts)
                except RPCError as error:
                    data["gifts_unavailable"] = type(error).__name__
            elif isinstance(entity, types.Channel):
                data["full"] = dump(await current(functions.channels.GetFullChannelRequest(entity)))
            if args.download_photo:
                folder = account_directory() / "profile-photos"
                folder.mkdir(parents=True, exist_ok=True)
                data["profile_photo_path"] = await current.download_profile_photo(entity, file=folder / str(utils.get_peer_id(entity)))
        except RPCError as error:
            data["details_unavailable"] = type(error).__name__
    with store().connect() as db:
        db.execute("INSERT INTO profiles(peer_key,data_json,updated_at) VALUES(?,?,?) ON CONFLICT(peer_key) DO UPDATE SET data_json=excluded.data_json,updated_at=excluded.updated_at", (key, json.dumps(data, ensure_ascii=False), stamp()))
    return {"source": "telegram_api", **data}


async def watch(args: argparse.Namespace) -> None:
    while True:
        print_json(await sync())
        await asyncio.sleep(args.interval)


def dialog(prompt: str, *, hidden: bool = False) -> str:
    command = "display dialog (item 1 of argv) default answer \"\""
    if hidden:
        command += " with hidden answer"
    command += " buttons {\"Cancel\", \"OK\"} default button \"OK\" cancel button \"Cancel\"\nreturn text returned of result"
    result = subprocess.run(["osascript", "-e", "on run argv\n" + command + "\nend run", prompt], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Input cancelled.")
    return result.stdout.rstrip("\n")


async def login_qr() -> dict[str, str]:
    """Log in without ever persisting the QR URL or password."""
    load_telethon()
    import qrcode

    api_id = int(keychain.require(keychain.API_ID, "Telegram API ID"))
    api_hash = keychain.require(keychain.API_HASH, "Telegram API hash")
    ROOT.mkdir(parents=True, exist_ok=True)
    qr_path = ROOT / "telegram-login-qr.png"
    try:
        current = TelegramClient(StringSession(), api_id, api_hash)
        await current.connect()
        try:
            qr_login = await current.qr_login()
            qrcode.make(qr_login.url).save(qr_path)
            os.chmod(qr_path, 0o600)
            subprocess.run(["open", str(qr_path)], check=True)
            try:
                await qr_login.wait()
            except SessionPasswordNeededError:
                password = dialog("Введите облачный пароль Telegram", hidden=True)
                await current.sign_in(password=password)
            me = await current.get_me()
            if not me:
                raise RuntimeError("QR login did not return a Telegram account.")
            account_id = int(me.id)
            name = utils.get_display_name(me) or str(account_id)
            comment = dialog(f"Комментарий для аккаунта {name}")
            keychain.put(keychain.session_service(account_id), current.session.save())
            account = accounts.add(ROOT, account_id, name, comment)
        finally:
            await current.disconnect()
        return {"status": "connected", "account_id": account["id"], "name": account["name"], "comment": account["comment"], "active": True}
    finally:
        qr_path.unlink(missing_ok=True)


def manage_account(args: argparse.Namespace) -> Any:
    if args.account_command == "list":
        return accounts.list_accounts(ROOT)
    if args.account_command == "use":
        return {**accounts.use(ROOT, args.id), "active": True}
    if args.account_command == "comment":
        return accounts.comment(ROOT, args.id, args.text)
    if args.account_command == "disconnect":
        keychain.remove(keychain.session_service(args.id))
        return {"status": "disconnected", **accounts.disconnect(ROOT, args.id)}
    raise ValueError("Unknown account command.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local Telegram reader and owner-gated sender")
    commands = result.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync")
    sync_parser.add_argument("--full", action="store_true")
    watch_parser = commands.add_parser("watch")
    watch_parser.add_argument("--interval", type=int, default=60)
    commands.add_parser("login-qr", help="Добавить аккаунт через QR-код")
    account_parser = commands.add_parser("account", help="Управление подключёнными аккаунтами")
    account_commands = account_parser.add_subparsers(dest="account_command", required=True)
    account_commands.add_parser("list")
    account_use = account_commands.add_parser("use")
    account_use.add_argument("--id", type=int, required=True)
    account_comment = account_commands.add_parser("comment")
    account_comment.add_argument("--id", type=int, required=True)
    account_comment.add_argument("--text", required=True)
    account_disconnect = account_commands.add_parser("disconnect")
    account_disconnect.add_argument("--id", type=int, required=True)
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--filter", choices=["all", "personal", "groups", "channels", "private-channels"], default="all")
    list_parser.add_argument("--query")
    list_parser.add_argument("--limit", type=int, default=100)
    comment_parser = commands.add_parser("comment")
    comment_parser.add_argument("--peer", type=int, required=True)
    comment_parser.add_argument("--text", required=True)
    read_parser = commands.add_parser("read")
    read_parser.add_argument("--peer", type=int, required=True)
    read_parser.add_argument("--limit", type=int, default=50)
    read_parser.add_argument("--after")
    read_parser.add_argument("--before")
    read_parser.add_argument("--min-id", type=int)
    read_parser.add_argument("--max-id", type=int)
    read_parser.add_argument("--json", action="store_true", help="Вывести исходные данные в JSON")
    search_parser = commands.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--peer", type=int)
    search_parser.add_argument("--limit", type=int, default=50)
    allow_parser = commands.add_parser("allow")
    allow_parser.add_argument("--peer", type=int, required=True)
    allow_parser.add_argument("--comment")
    disallow_parser = commands.add_parser("disallow")
    disallow_parser.add_argument("--peer", type=int, required=True)
    commands.add_parser("allowed")
    send_parser = commands.add_parser("send")
    send_parser.add_argument("--peer", type=int, required=True)
    send_parser.add_argument("--text", required=True)
    voice_parser = commands.add_parser("transcribe")
    voice_parser.add_argument("--peer", type=int, required=True)
    voice_parser.add_argument("--message", type=int, required=True)
    photo_parser = commands.add_parser("photo")
    photo_parser.add_argument("--peer", type=int, required=True)
    photo_parser.add_argument("--message", type=int, required=True)
    photo_parser.add_argument("--open", action="store_true")
    info_parser = commands.add_parser("info")
    info_parser.add_argument("--peer", required=True, help="Known numeric id or @username")
    info_parser.add_argument("--refresh", action="store_true")
    info_parser.add_argument("--download-photo", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "sync":
            output = asyncio.run(sync(args.full))
        elif args.command == "watch":
            asyncio.run(watch(args))
            return
        elif args.command == "login-qr":
            output = asyncio.run(login_qr())
        elif args.command == "account":
            output = manage_account(args)
        elif args.command == "list":
            output = list_peers(args)
        elif args.command == "comment":
            output = set_comment(args)
        elif args.command == "read":
            output = read_messages(args)
        elif args.command == "search":
            output = search_messages(args)
        elif args.command == "allow":
            output = asyncio.run(set_allowed(args))
        elif args.command == "disallow":
            output = asyncio.run(set_allowed(args, remove=True))
        elif args.command == "allowed":
            output = allowed_list()
        elif args.command == "send":
            output = asyncio.run(send(args))
        elif args.command == "transcribe":
            output = asyncio.run(transcribe(args))
        elif args.command == "photo":
            output = asyncio.run(photo(args))
        else:
            output = asyncio.run(profile(args))
        if args.command == "read" and not args.json:
            print_chat(output)
        else:
            print_json(output)
    except (RuntimeError, ValueError, keychain.KeychainError, RPCError) as error:
        raise SystemExit(f"telegram-chat-control: {type(error).__name__}: {error}") from error


if __name__ == "__main__":
    main()
