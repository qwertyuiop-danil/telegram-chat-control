"""Keep Telegram credentials in the macOS Keychain."""

from __future__ import annotations

import getpass
import subprocess

ACCOUNT = getpass.getuser()
API_ID = "codex-tg-reader-api-id"
API_HASH = "codex-tg-reader-api-hash"
SESSION = "codex-tg-reader-session"
OWNER_ID = "codex-telegram-owner-id"


class KeychainError(RuntimeError):
    pass


def get(service: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def require(service: str, label: str) -> str:
    value = get(service)
    if not value:
        raise KeychainError(f"{label} is missing from Keychain.")
    return value


def put(service: str, value: str) -> None:
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", ACCOUNT, "-s", service, "-w", value],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise KeychainError("Could not write to Keychain.")


def remove(service: str) -> None:
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", ACCOUNT, "-s", service],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 44):  # 44 = item is already absent
        raise KeychainError("Could not remove Keychain item.")


def session_service(account_id: int | str) -> str:
    return f"codex-tg-reader-session-{account_id}"
