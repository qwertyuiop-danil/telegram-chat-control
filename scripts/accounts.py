"""Small, owner-only registry for Telegram accounts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

import keychain


def _path(root: Path) -> Path:
    return root / "accounts.json"


def _write(root: Path, data: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = _path(root)
    fd, temporary = tempfile.mkstemp(prefix="accounts-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _copy_legacy_index(root: Path, account_id: str) -> None:
    source_path = root / "data" / "telegram.sqlite3"
    destination = root / "data" / "accounts" / account_id / "telegram.sqlite3"
    if not source_path.exists() or destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    os.chmod(destination, 0o600)


def load(root: Path) -> dict[str, Any]:
    path = _path(root)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    legacy_id = keychain.get(keychain.OWNER_ID)
    legacy_session = keychain.get(keychain.SESSION)
    if not legacy_id or not legacy_session:
        data: dict[str, Any] = {"active_id": None, "accounts": []}
        _write(root, data)
        return data
    account_id = str(int(legacy_id))
    keychain.put(keychain.session_service(account_id), legacy_session)
    _copy_legacy_index(root, account_id)
    data = {"active_id": account_id, "accounts": [{"id": account_id, "name": account_id, "comment": ""}]}
    _write(root, data)
    return data


def active(root: Path) -> dict[str, str]:
    data = load(root)
    account_id = data.get("active_id")
    for account in data["accounts"]:
        if account["id"] == account_id:
            return account
    raise RuntimeError("No active Telegram account. Run login-qr first.")


def list_accounts(root: Path) -> list[dict[str, Any]]:
    data = load(root)
    return [{**account, "active": account["id"] == data.get("active_id")} for account in data["accounts"]]


def add(root: Path, account_id: int, name: str, comment: str, *, activate: bool = True) -> dict[str, str]:
    data = load(root)
    item = {"id": str(account_id), "name": name or str(account_id), "comment": comment}
    data["accounts"] = [account for account in data["accounts"] if account["id"] != item["id"]]
    data["accounts"].append(item)
    if activate:
        data["active_id"] = item["id"]
    _write(root, data)
    return item


def use(root: Path, account_id: int) -> dict[str, str]:
    data = load(root)
    target = str(account_id)
    if not any(account["id"] == target for account in data["accounts"]):
        raise RuntimeError("Telegram account is not connected.")
    data["active_id"] = target
    _write(root, data)
    return active(root)


def comment(root: Path, account_id: int, text: str) -> dict[str, str]:
    data = load(root)
    target = str(account_id)
    for account in data["accounts"]:
        if account["id"] == target:
            account["comment"] = text
            _write(root, data)
            return account
    raise RuntimeError("Telegram account is not connected.")


def disconnect(root: Path, account_id: int) -> dict[str, Any]:
    data = load(root)
    target = str(account_id)
    removed = next((account for account in data["accounts"] if account["id"] == target), None)
    if not removed:
        raise RuntimeError("Telegram account is not connected.")
    data["accounts"] = [account for account in data["accounts"] if account["id"] != target]
    if data.get("active_id") == target:
        data["active_id"] = data["accounts"][0]["id"] if data["accounts"] else None
    _write(root, data)
    return {**removed, "active_id": data["active_id"]}
