"""Account registry and migration from the v1 on-disk layout."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from .credentials import Credentials, legacy_macos_secret, migrate_legacy_macos, session_key


def _path(root: Path) -> Path:
    return root / "accounts.json"


def _write(root: Path, data: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="accounts-", dir=root)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, _path(root))
        if os.name != "nt":
            os.chmod(_path(root), 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _copy_legacy_index(root: Path, account_id: str) -> None:
    source = root / "data" / "telegram.sqlite3"
    destination = root / "data" / "accounts" / account_id / "telegram.sqlite3"
    if not source.exists() or destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    old_db, new_db = sqlite3.connect(source), sqlite3.connect(destination)
    try:
        old_db.backup(new_db)
    finally:
        new_db.close()
        old_db.close()
    if os.name != "nt":
        os.chmod(destination, 0o600)


def load(root: Path) -> dict[str, Any]:
    path = _path(root)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    legacy_id = legacy_macos_secret("codex-telegram-owner-id")
    legacy_session = legacy_macos_secret("codex-tg-reader-session")
    if not legacy_id or not legacy_session:
        data: dict[str, Any] = {"active_id": None, "accounts": []}
        _write(root, data)
        return data
    account_id = str(int(legacy_id))
    credentials = Credentials(root)
    credentials.put(session_key(account_id), legacy_session)
    migrate_legacy_macos(credentials, account_id)
    _copy_legacy_index(root, account_id)
    data = {"active_id": account_id, "accounts": [{"id": account_id, "name": account_id, "comment": ""}]}
    _write(root, data)
    return data


def active(root: Path) -> dict[str, str]:
    data = load(root)
    for account in data["accounts"]:
        if account["id"] == data.get("active_id"):
            return account
    raise RuntimeError("No active Telegram account. Run `account add` first.")


def list_accounts(root: Path) -> list[dict[str, Any]]:
    data = load(root)
    return [{**account, "active": account["id"] == data.get("active_id")} for account in data["accounts"]]


def add(root: Path, account_id: int, name: str, comment: str, *, activate: bool = True) -> dict[str, str]:
    data = load(root)
    account = {"id": str(account_id), "name": name or str(account_id), "comment": comment}
    data["accounts"] = [item for item in data["accounts"] if item["id"] != account["id"]]
    data["accounts"].append(account)
    if activate:
        data["active_id"] = account["id"]
    _write(root, data)
    return account


def use(root: Path, account_id: int) -> dict[str, str]:
    data = load(root)
    target = str(account_id)
    if not any(item["id"] == target for item in data["accounts"]):
        raise RuntimeError("Telegram account is not connected.")
    data["active_id"] = target
    _write(root, data)
    return active(root)


def disconnect(root: Path, account_id: int) -> dict[str, Any]:
    data = load(root)
    target = str(account_id)
    account = next((item for item in data["accounts"] if item["id"] == target), None)
    if not account:
        raise RuntimeError("Telegram account is not connected.")
    data["accounts"] = [item for item in data["accounts"] if item["id"] != target]
    if data.get("active_id") == target:
        data["active_id"] = data["accounts"][0]["id"] if data["accounts"] else None
    _write(root, data)
    Credentials(root).remove(session_key(target))
    return {**account, "active_id": data["active_id"]}
