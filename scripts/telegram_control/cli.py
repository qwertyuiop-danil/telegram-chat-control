"""Structured command line interface for the local Telegram index."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date as calendar_date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable

from . import accounts
from .client import CredentialError, RPCError, allowed_list, download_photo, login_qr, profile, reconcile_deletions, send, set_allowed, store, sync_index, transcribe
from .services import install as install_service
from .services import run_daemon, start as start_service, status as service_status, stop as stop_service, uninstall as uninstall_service
from .store import Store, decode_cursor, encode_cursor, now, row_dicts


def default_root() -> Path:
    """Prefer the shared OpenClaw runtime when this skill is installed there."""
    openclaw = Path.home() / ".openclaw" / "telegram-chat-control"
    return openclaw if openclaw.exists() else Path.home() / ".codex" / "telegram-chat-control"


ROOT = Path(os.environ.get("TELEGRAM_CHAT_CONTROL_HOME", default_root()))
DEFAULT_LIMIT = 20
MAX_LIMIT = 200


def output(value: dict[str, Any], *, text_format: bool = False) -> None:
    if text_format and value.get("items") and value.get("meta", {}).get("kind") == "messages":
        print_messages(value["items"])
        return
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def envelope(kind: str, items: list[Any], *, limit: int | None = None, has_more: bool = False, next_cursor: str | None = None, fields: list[str] | None = None, warnings: list[str] | None = None, period: dict[str, Any] | None = None) -> dict[str, Any]:
    account_id: str | None
    try:
        account_id = accounts.active(ROOT)["id"]
    except RuntimeError:
        account_id = None
    sync_age: float | None = None
    if account_id:
        try:
            with store(ROOT, account_id).connect(write=False) as db:
                state = Store.state(db).get("last_sync")
            if state:
                at = state["value"].get("at")
                sync_age = round((datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds(), 3) if at else None
        except (KeyError, TypeError, ValueError):
            pass
    return {
        "meta": {
            "kind": kind,
            "account_id": account_id,
            "generated_at": now(),
            "sync_age_seconds": sync_age,
            "limit": limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "fields": fields or [],
            "warnings": warnings or [],
            "period": period,
        },
        "items": items,
    }


def limit(value: int) -> int:
    if value < 1 or value > MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}.")
    return value


def requested_fields(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    fields = [field.strip() for field in raw.split(",") if field.strip()]
    if not fields:
        raise ValueError("--fields must contain at least one field.")
    return fields


def select_fields(value: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    if not fields:
        return value
    missing = [field for field in fields if field not in value]
    if missing:
        raise ValueError(f"Unknown field(s): {', '.join(missing)}")
    return {field: value[field] for field in fields}


def resolve_period(args: argparse.Namespace) -> dict[str, Any] | None:
    """Return a half-open UTC interval from the shared time flags."""
    requested_minutes = getattr(args, "last_m", None)
    requested_hours = getattr(args, "last_h", None)
    if (requested_minutes is not None and requested_minutes <= 0) or (requested_hours is not None and requested_hours <= 0):
        raise ValueError("--last_m and --last_h must be positive integers.")
    minutes = requested_minutes or 0
    hours = requested_hours or 0
    requested_date = getattr(args, "date", None)
    after = getattr(args, "after", None)
    before = getattr(args, "before", None)
    if requested_date and (minutes or hours or requested_minutes is not None or requested_hours is not None):
        raise ValueError("--date cannot be combined with --last_m or --last_h.")
    if (requested_date or minutes or hours) and (after or before):
        raise ValueError("--date, --last_m, and --last_h cannot be combined with --after or --before.")
    if minutes or hours:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours, minutes=minutes)
        return {"source": "relative", "start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC"}
    if not requested_date:
        return None
    try:
        selected = calendar_date.fromisoformat(requested_date)
    except ValueError as error:
        raise ValueError("--date must use YYYY-MM-DD.") from error
    start_local = datetime(selected.year, selected.month, selected.day).astimezone()
    next_day = selected + timedelta(days=1)
    end_local = datetime(next_day.year, next_day.month, next_day.day).astimezone()
    return {
        "source": "date",
        "start": start_local.astimezone(timezone.utc).isoformat(),
        "end": end_local.astimezone(timezone.utc).isoformat(),
        "timezone": str(start_local.tzinfo),
    }


def json_or_empty(value: str | None) -> Any:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def format_chat(row: dict[str, Any], *, full: bool = False, fields: list[str] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "chat_id": row["peer_id"], "title": row["title"], "type": row["kind"], "username": row.get("username"),
        "visibility": "private" if row.get("is_private") else "public", "bot": bool(row.get("is_bot")),
        "unread_count": row.get("unread_count", 0), "archived": bool(row.get("archived")),
        "muted": bool(row.get("muted")), "pinned": bool(row.get("pinned")),
        "has_topics": bool(row.get("has_topics")), "member_count": row.get("member_count"), "role": row.get("role"),
        "last_message_at": row.get("last_message_at"), "comment": row.get("comment") or None,
    }
    if full:
        item["details"] = json_or_empty(row.get("data_json"))
    if row.get("activity_message_count") is not None:
        item["activity"] = {
            "message_count": row["activity_message_count"],
            "incoming_count": row["activity_incoming_count"],
            "outgoing_count": row["activity_outgoing_count"],
            "last_message_at": row["activity_last_message_at"],
        }
    return select_fields(item, fields)


def format_message(row: dict[str, Any], args: argparse.Namespace, fields: list[str] | None = None) -> dict[str, Any]:
    maximum = args.max_text_chars
    if maximum < 1:
        raise ValueError("--max-text-chars must be positive.")
    body = row.get("body") or ""
    truncated = not args.full and len(body) > maximum
    text = body if args.full else body[:maximum] + ("…" if truncated else "")
    sender_name = row.get("entity_title") or row.get("sender_name") or ("Unknown sender" if row.get("sender_id") else "Telegram")
    item: dict[str, Any] = {
        "chat": {"chat_id": row["peer_id"], "title": row["chat_title"], "type": row["chat_kind"], "username": row.get("chat_username")},
        "message_id": row["message_id"], "sent_at": row.get("sent_at"), "direction": row.get("direction"),
        "sender": {"id": row.get("sender_id"), "name": sender_name, "username": row.get("entity_username"), "is_owner": row.get("direction") == "outgoing"},
        "text": text, "text_truncated": truncated,
        "media": {"kind": row["media_kind"]} if row.get("media_kind") else None,
        "reply_to": row.get("reply_to_id"), "topic": row.get("topic_id"), "edited": bool(row.get("edited_at")),
        "deleted": {"value": bool(row.get("is_deleted")), "at": row.get("deleted_at"), "source": row.get("deletion_source")},
        "reactions": json_or_empty(row.get("reactions_json")), "pinned": bool(row.get("is_pinned")),
        "service": bool(row.get("is_service")),
    }
    if args.full:
        item["voice_transcript"] = row.get("voice_transcript")
        item["forward"] = json_or_empty(row.get("forward_json"))
        item["details"] = json_or_empty(row.get("raw_json"))
    return select_fields(item, fields)


def print_messages(items: list[dict[str, Any]]) -> None:
    for item in items:
        chat = item["chat"]
        direction = {"incoming": "←", "outgoing": "→", "channel_post": "•", "service": "⚙"}.get(item["direction"], "?")
        tags = []
        if item["deleted"]["value"]:
            tags.append("[УДАЛЕНО]")
        if item["edited"]:
            tags.append("[ИЗМЕНЕНО]")
        if item["media"]:
            tags.append(f"[МЕДИА: {item['media']['kind']}]")
        stamp = item["sent_at"] or "—"
        print(f"{chat['title']} ({chat['chat_id']})")
        print(f"{stamp} {direction} {item['sender']['name']} #{item['message_id']} {' '.join(tags)}".rstrip())
        print(item["text"] or "[сообщение без текста]")


def _chat_conditions(args: argparse.Namespace) -> tuple[list[str], list[Any]]:
    where = ["1=1"]
    values: list[Any] = []
    if getattr(args, "type", None):
        where.append("p.kind=?")
        values.append(args.type)
    if getattr(args, "visibility", None):
        where.append("p.is_private=?")
        values.append(1 if args.visibility == "private" else 0)
    if getattr(args, "name", None):
        where.append("p.title LIKE ?")
        values.append(f"%{args.name}%")
    if getattr(args, "username", None):
        where.append("p.username LIKE ?")
        values.append(f"%{args.username.lstrip('@')}%")
    if getattr(args, "query", None):
        where.append("(p.title LIKE ? OR p.username LIKE ? OR p.comment LIKE ?)")
        values.extend([f"%{args.query}%"] * 3)
    for flag, column in (("unread", "p.unread_count > 0"), ("archived", "p.archived=1"), ("muted", "p.muted=1"), ("pinned", "p.pinned=1"), ("has_topics", "p.has_topics=1")):
        if getattr(args, flag, False):
            where.append(column)
    if getattr(args, "role", None):
        where.append("p.role=?")
        values.append(args.role)
    if getattr(args, "member_min", None) is not None:
        where.append("p.member_count>=?")
        values.append(args.member_min)
    if getattr(args, "member_max", None) is not None:
        where.append("p.member_count<=?")
        values.append(args.member_max)
    return where, values


def chat_list(args: argparse.Namespace) -> dict[str, Any]:
    fields = requested_fields(args.fields)
    where, values = _chat_conditions(args)
    requested = limit(args.limit)
    period = resolve_period(args)
    direction = getattr(args, "direction", None)
    if direction and not period:
        raise ValueError("--direction for chats requires --last_m, --last_h, or --date.")
    with store(ROOT).connect(write=False) as db:
        if period:
            selected_direction = direction or "incoming"
            active_where = {"incoming": "a.activity_incoming_count > 0", "outgoing": "a.activity_outgoing_count > 0", "any": "a.activity_message_count > 0"}[selected_direction]
            rows = row_dicts(db.execute(
                f"""
                WITH activity AS (
                  SELECT peer_id,COUNT(*) AS activity_message_count,
                    SUM(CASE WHEN direction='incoming' THEN 1 ELSE 0 END) AS activity_incoming_count,
                    SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) AS activity_outgoing_count,
                    MAX(sent_at) AS activity_last_message_at
                  FROM messages WHERE sent_at >= ? AND sent_at < ? GROUP BY peer_id
                )
                SELECT p.*,a.activity_message_count,a.activity_incoming_count,a.activity_outgoing_count,a.activity_last_message_at
                FROM peers p JOIN activity a ON a.peer_id=p.peer_id
                WHERE {' AND '.join(where)} AND {active_where}
                ORDER BY a.activity_last_message_at DESC,p.peer_id DESC LIMIT ?
                """,
                (period["start"], period["end"], *values, requested + 1),
            ).fetchall())
        else:
            rows = row_dicts(db.execute(
                f"SELECT p.* FROM peers p WHERE {' AND '.join(where)} ORDER BY p.last_message_at DESC NULLS LAST,p.peer_id DESC LIMIT ?",
                (*values, requested + 1),
            ).fetchall())
    has_more = len(rows) > requested
    items = [format_chat(row, full=args.full, fields=fields) for row in rows[:requested]]
    return envelope("chats", items, limit=requested, has_more=has_more, fields=fields, period=period)


def chat_show(args: argparse.Namespace) -> dict[str, Any]:
    fields = requested_fields(args.fields)
    with store(ROOT).connect(write=False) as db:
        row = db.execute("SELECT * FROM peers WHERE peer_id=?", (args.chat,)).fetchone()
    if not row:
        raise RuntimeError("Chat is not indexed. Run `sync run` first.")
    return envelope("chat", [format_chat(dict(row), full=args.full, fields=fields)], fields=fields)


def _message_conditions(args: argparse.Namespace, period: dict[str, Any] | None) -> tuple[list[str], list[Any]]:
    where = ["1=1"]
    values: list[Any] = []
    chats = getattr(args, "chat", None) or []
    if isinstance(chats, int):
        chats = [chats]
    selected_chats: list[str] = []
    if chats:
        selected_chats.append("m.peer_id IN (" + ",".join("?" for _ in chats) + ")")
        values.extend(chats)
    if getattr(args, "personal", False):
        selected_chats.append("p.kind IN ('personal','bot')")
    if selected_chats:
        where.append("(" + " OR ".join(selected_chats) + ")")
    if getattr(args, "sender", None) is not None:
        where.append("m.sender_id=?")
        values.append(args.sender)
    if getattr(args, "direction", None):
        where.append("m.direction=?")
        values.append(args.direction)
    if getattr(args, "after", None):
        where.append("m.sent_at>=?")
        values.append(args.after)
    if getattr(args, "before", None):
        where.append("m.sent_at<=?")
        values.append(args.before)
    if period:
        where.extend(["m.sent_at>=?", "m.sent_at<?"])
        values.extend([period["start"], period["end"]])
    deleted = getattr(args, "deleted", "any")
    if deleted == "only":
        where.append("m.is_deleted=1")
    elif deleted == "exclude":
        where.append("m.is_deleted=0")
    if getattr(args, "edited", False):
        where.append("m.edited_at IS NOT NULL")
    if getattr(args, "media", None):
        where.append("m.media_kind=?")
        values.append(args.media)
    if getattr(args, "topic", None) is not None:
        where.append("m.topic_id=?")
        values.append(args.topic)
    if getattr(args, "reply_to", None) is not None:
        where.append("m.reply_to_id=?")
        values.append(args.reply_to)
    if getattr(args, "reactions", False):
        where.append("m.reactions_json != '{}'")
    if getattr(args, "pinned", False):
        where.append("m.is_pinned=1")
    cursor = decode_cursor(getattr(args, "cursor", None))
    if cursor:
        sent_at, peer_id, message_id = cursor
        comparison = ">" if getattr(args, "order", "desc") == "asc" else "<"
        where.append(f"(COALESCE(m.sent_at,'') {comparison} ? OR (COALESCE(m.sent_at,'')=? AND (m.peer_id {comparison} ? OR (m.peer_id=? AND m.message_id {comparison} ?))))")
        values.extend([sent_at, sent_at, peer_id, peer_id, message_id])
    return where, values


MESSAGE_SELECT = """
SELECT m.*,p.title AS chat_title,p.kind AS chat_kind,p.username AS chat_username,
       e.title AS entity_title,e.username AS entity_username
FROM messages m JOIN peers p ON p.peer_id=m.peer_id
LEFT JOIN entities e ON e.entity_id=m.sender_id
"""


def _fts_expression(query: str, mode: str) -> str:
    terms = re.findall(r"[\w-]+", query, flags=re.UNICODE)
    if not terms:
        raise ValueError("Search query has no searchable terms.")
    if mode == "phrase":
        return '"' + " ".join(terms).replace('"', '""') + '"'
    joiner = " OR " if mode == "any" else " AND "
    return joiner.join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def message_list(args: argparse.Namespace, *, search_query: str | None = None) -> dict[str, Any]:
    fields = requested_fields(args.fields)
    period = resolve_period(args)
    where, values = _message_conditions(args, period)
    requested = limit(args.limit)
    prefix = MESSAGE_SELECT
    if search_query is not None:
        prefix += "JOIN messages_fts f ON f.peer_id=m.peer_id AND f.message_id=m.message_id\n"
        where.insert(0, "messages_fts MATCH ?")
        values.insert(0, _fts_expression(search_query, args.match))
    with store(ROOT).connect(write=False) as db:
        direction = "ASC" if getattr(args, "order", "desc") == "asc" else "DESC"
        rows = row_dicts(db.execute(
            prefix + f"WHERE {' AND '.join(where)} ORDER BY COALESCE(m.sent_at,'') {direction},m.peer_id {direction},m.message_id {direction} LIMIT ?",
            (*values, requested + 1),
        ).fetchall())
    has_more = len(rows) > requested
    shown = rows[:requested]
    items = [format_message(row, args, fields) for row in shown]
    next_cursor = None
    if has_more and shown:
        last = shown[-1]
        next_cursor = encode_cursor(last.get("sent_at"), int(last["peer_id"]), int(last["message_id"]))
    return envelope("messages", items, limit=requested, has_more=has_more, next_cursor=next_cursor, fields=fields, period=period)


def message_show(args: argparse.Namespace) -> dict[str, Any]:
    fields = requested_fields(args.fields)
    with store(ROOT).connect(write=False) as db:
        row = db.execute(MESSAGE_SELECT + "WHERE m.peer_id=? AND m.message_id=?", (args.chat, args.message)).fetchone()
    if not row:
        raise RuntimeError("Message is not indexed.")
    return envelope("message", [format_message(dict(row), args, fields)], fields=fields)


def set_comment(args: argparse.Namespace) -> dict[str, Any]:
    with store(ROOT).connect() as db:
        changed = db.execute("UPDATE peers SET comment=? WHERE peer_id=?", (args.text, args.chat)).rowcount
    if not changed:
        raise RuntimeError("Chat is not indexed. Run `sync run` first.")
    return envelope("chat", [{"chat_id": args.chat, "comment": args.text}])


def sync_status() -> dict[str, Any]:
    return envelope("sync", [service_status(ROOT)])


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Local Telegram reader with a compact, agent-safe JSON interface")
    commands = root.add_subparsers(dest="command", required=True)

    account = commands.add_parser("account", help="Manage Telegram accounts")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    add = account_commands.add_parser("add", help="Connect an account with a QR code")
    add.add_argument("--api-id")
    add.add_argument("--api-hash")
    add.add_argument("--comment", default="")
    account_commands.add_parser("list")
    use = account_commands.add_parser("use")
    use.add_argument("--id", type=int, required=True)
    disconnect = account_commands.add_parser("disconnect")
    disconnect.add_argument("--id", type=int, required=True)

    chat = commands.add_parser("chat", help="Find and inspect indexed chats")
    chat_commands = chat.add_subparsers(dest="chat_command", required=True)
    for name in ("list", "search"):
        item = chat_commands.add_parser(name)
        if name == "search":
            item.add_argument("query")
        _chat_filters(item)
        item.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        item.add_argument("--full", action="store_true")
        item.add_argument("--fields")
    show = chat_commands.add_parser("show")
    show.add_argument("--chat", type=int, required=True)
    show.add_argument("--full", action="store_true")
    show.add_argument("--fields")
    comment = chat_commands.add_parser("comment")
    comment.add_argument("--chat", type=int, required=True)
    comment.add_argument("--text", required=True)

    message = commands.add_parser("message", help="Read and search indexed messages")
    message_commands = message.add_subparsers(dest="message_command", required=True)
    for name in ("list", "search"):
        item = message_commands.add_parser(name)
        if name == "search":
            item.add_argument("query")
            item.add_argument("--match", choices=["all", "any", "phrase"], default="all")
        _message_filters(item)
        item.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        item.add_argument("--cursor")
        item.add_argument("--full", action="store_true")
        item.add_argument("--fields")
        item.add_argument("--max-text-chars", type=int, default=500)
        item.add_argument("--format", choices=["json", "text"], default="json")
    show_message = message_commands.add_parser("show")
    show_message.add_argument("--chat", type=int, required=True)
    show_message.add_argument("--message", type=int, required=True)
    show_message.add_argument("--full", action="store_true")
    show_message.add_argument("--fields")
    show_message.add_argument("--max-text-chars", type=int, default=500)
    show_message.add_argument("--format", choices=["json", "text"], default="json")
    voice = message_commands.add_parser("transcribe")
    voice.add_argument("--chat", type=int, required=True)
    voice.add_argument("--message", type=int, required=True)
    photo = message_commands.add_parser("photo")
    photo.add_argument("--chat", type=int, required=True)
    photo.add_argument("--message", type=int, required=True)

    profile_parser = commands.add_parser("profile", help="Get a normalized Telegram profile")
    profile_commands = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_show = profile_commands.add_parser("show")
    profile_show.add_argument("--peer", required=True)
    profile_show.add_argument("--refresh", action="store_true")
    profile_show.add_argument("--full", action="store_true")
    profile_show.add_argument("--section", choices=["admins", "gifts", "permissions", "raw"])
    profile_show.add_argument("--photo", action="store_true")
    profile_show.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    profile_show.add_argument("--fields")

    sync = commands.add_parser("sync", help="Synchronize and audit the local index")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    run = sync_commands.add_parser("run")
    run.add_argument("--full", action="store_true")
    reconcile = sync_commands.add_parser("reconcile")
    reconcile.add_argument("--deep", action="store_true")
    sync_commands.add_parser("status")

    service = commands.add_parser("service", help="Manage the user background service")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    for name in ("install", "start", "stop", "status", "uninstall"):
        service_commands.add_parser(name)
    service_commands.add_parser("run", help=argparse.SUPPRESS)

    allowlist = commands.add_parser("allowlist", help="Manage send permissions")
    allow_commands = allowlist.add_subparsers(dest="allow_command", required=True)
    allow_commands.add_parser("list")
    allow_add = allow_commands.add_parser("add")
    allow_add.add_argument("--chat", type=int, required=True)
    allow_add.add_argument("--comment", default="")
    allow_remove = allow_commands.add_parser("remove")
    allow_remove.add_argument("--chat", type=int, required=True)
    send_parser = commands.add_parser("send", help="Send only to an allowlisted chat")
    send_parser.add_argument("--chat", type=int, required=True)
    send_parser.add_argument("--text", required=True)
    return root


def _chat_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", choices=["personal", "bot", "group", "channel"])
    parser.add_argument("--visibility", choices=["public", "private"])
    parser.add_argument("--name")
    parser.add_argument("--username")
    parser.add_argument("--unread", action="store_true")
    parser.add_argument("--archived", action="store_true")
    parser.add_argument("--muted", action="store_true")
    parser.add_argument("--pinned", action="store_true")
    parser.add_argument("--has-topics", action="store_true")
    parser.add_argument("--role", choices=["owner", "admin", "member"])
    parser.add_argument("--member-min", type=int)
    parser.add_argument("--member-max", type=int)
    parser.add_argument("--last_m", type=int)
    parser.add_argument("--last_h", type=int)
    parser.add_argument("--date")
    parser.add_argument("--direction", choices=["any", "incoming", "outgoing"])


def _message_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chat", type=int, action="append")
    parser.add_argument("--personal", action="store_true")
    parser.add_argument("--sender", type=int)
    parser.add_argument("--direction", choices=["incoming", "outgoing", "channel_post", "service"])
    parser.add_argument("--after")
    parser.add_argument("--before")
    parser.add_argument("--last_m", type=int)
    parser.add_argument("--last_h", type=int)
    parser.add_argument("--date")
    parser.add_argument("--order", choices=["asc", "desc"], default="desc")
    parser.add_argument("--deleted", choices=["any", "only", "exclude"], default="any")
    parser.add_argument("--edited", action="store_true")
    parser.add_argument("--media")
    parser.add_argument("--topic", type=int)
    parser.add_argument("--reply-to", type=int)
    parser.add_argument("--reactions", action="store_true")
    parser.add_argument("--pinned", action="store_true")


async def dispatch(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.command == "account":
        if args.account_command == "add":
            return envelope("account", [await login_qr(ROOT, api_id=args.api_id, api_hash=args.api_hash, comment=args.comment)])
        if args.account_command == "list":
            return envelope("accounts", accounts.list_accounts(ROOT))
        if args.account_command == "use":
            return envelope("account", [accounts.use(ROOT, args.id)])
        return envelope("account", [accounts.disconnect(ROOT, args.id)])
    if args.command == "chat":
        if args.chat_command in {"list", "search"}:
            return chat_list(args)
        if args.chat_command == "show":
            return chat_show(args)
        return set_comment(args)
    if args.command == "message":
        if args.message_command == "list":
            return message_list(args)
        if args.message_command == "search":
            return message_list(args, search_query=args.query)
        if args.message_command == "show":
            return message_show(args)
        if args.message_command == "transcribe":
            return envelope("transcription", [await transcribe(ROOT, args.chat, args.message)])
        return envelope("photo", [await download_photo(ROOT, args.chat, args.message)])
    if args.command == "profile":
        fields = requested_fields(args.fields)
        data = await profile(ROOT, args.peer, refresh=args.refresh, full=args.full, section=args.section, photo=args.photo, limit=limit(args.limit))
        return envelope("profile", [select_fields(data, fields)], limit=args.limit, fields=fields)
    if args.command == "sync":
        if args.sync_command == "run":
            return envelope("sync", [await sync_index(ROOT, full=args.full)])
        if args.sync_command == "reconcile":
            return envelope("reconcile", [await reconcile_deletions(ROOT, deep=args.deep)])
        return sync_status()
    if args.command == "service":
        if args.service_command == "run":
            await run_daemon(ROOT)
            return None
        functions: dict[str, Callable[[], dict[str, Any]]] = {
            "install": install_service, "start": start_service, "stop": stop_service,
            "status": lambda: service_status(ROOT), "uninstall": uninstall_service,
        }
        return envelope("service", [functions[args.service_command]()])
    if args.command == "allowlist":
        if args.allow_command == "list":
            return envelope("allowlist", allowed_list(ROOT))
        return envelope("allowlist", [await set_allowed(ROOT, args.chat, getattr(args, "comment", ""), remove=args.allow_command == "remove")])
    return envelope("send", [await send(ROOT, args.chat, args.text)])


def main() -> None:
    args = parser().parse_args()
    try:
        result = asyncio.run(dispatch(args))
        if result is not None:
            output(result, text_format=getattr(args, "format", "json") == "text")
    except (RuntimeError, ValueError, CredentialError, RPCError) as error:
        raise SystemExit(f"telegram-chat-control: {type(error).__name__}: {error}") from error


if __name__ == "__main__":
    main()
