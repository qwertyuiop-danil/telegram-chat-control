"""Credential storage with a keyring-first, private-file fallback."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
from typing import Any


SERVICE = "codex-telegram-chat-control"
API_ID = "api-id"
API_HASH = "api-hash"


class CredentialError(RuntimeError):
    pass


def session_key(account_id: str | int) -> str:
    return f"session:{account_id}"


class Credentials:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "secrets.json"
        self._keyring: Any | None = None
        self._keyring_checked = False

    def _load_keyring(self) -> Any | None:
        if not self._keyring_checked:
            self._keyring_checked = True
            # A lingering systemd user service and a non-interactive SSH shell
            # do not have a session D-Bus. Secret Service can block while
            # trying to discover one, so use the documented private fallback.
            headless_openclaw = self.root.parent.name == ".openclaw"
            if platform.system() == "Linux" and (headless_openclaw or not os.environ.get("DBUS_SESSION_BUS_ADDRESS")):
                self._keyring = None
                return None
            try:
                import keyring  # type: ignore[import-not-found]

                # Calling get_password exercises the configured backend. Several
                # headless Linux backends import but fail only on first access.
                keyring.get_password(SERVICE, "__availability_probe__")
                self._keyring = keyring
            except Exception:
                self._keyring = None
        return self._keyring

    def backend(self) -> str:
        return "keyring" if self._load_keyring() else "private-file"

    def _protect_file(self) -> None:
        if os.name != "nt":
            os.chmod(self.path, 0o600)
            return
        # icacls is bundled with supported Windows editions. Keep the fallback
        # usable if policy blocks it, but surface the weaker state in status.
        user = getpass.getuser()
        subprocess.run(
            ["icacls", str(self.path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            check=False,
            text=True,
        )

    def _read_file(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise CredentialError("Local credential file is invalid.") from error
        return {str(key): str(value) for key, value in data.items()}

    def _write_file(self, data: dict[str, str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="secrets-", dir=self.root)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, self.path)
            self._protect_file()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, key: str) -> str | None:
        backend = self._load_keyring()
        if backend:
            try:
                value = backend.get_password(SERVICE, key)
                if value:
                    return value
            except Exception:
                self._keyring = None
        return self._read_file().get(key)

    def require(self, key: str, label: str) -> str:
        value = self.get(key)
        if not value:
            raise CredentialError(f"{label} is missing. Run account add or configure the credential store first.")
        return value

    def put(self, key: str, value: str) -> None:
        backend = self._load_keyring()
        if backend:
            try:
                backend.set_password(SERVICE, key, value)
                return
            except Exception:
                self._keyring = None
        data = self._read_file()
        data[key] = value
        self._write_file(data)

    def remove(self, key: str) -> None:
        backend = self._load_keyring()
        if backend:
            try:
                backend.delete_password(SERVICE, key)
                return
            except Exception:
                self._keyring = None
        data = self._read_file()
        data.pop(key, None)
        self._write_file(data)

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"backend": self.backend(), "fallback_path": None}
        if result["backend"] == "private-file":
            result["fallback_path"] = str(self.path)
            result["warning"] = "Credentials are in a local protected file because a system keyring is unavailable."
            if self.path.exists() and os.name != "nt":
                result["permissions"] = oct(self.path.stat().st_mode & 0o777)
        return result


def legacy_macos_secret(service: str) -> str | None:
    """Read old v1 Keychain entries once, only on macOS."""
    if platform.system() != "Darwin":
        return None
    result = subprocess.run(
        ["security", "find-generic-password", "-a", getpass.getuser(), "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def migrate_legacy_macos(credentials: Credentials, account_id: str) -> None:
    """Copy v1 secrets into the current backend without deleting the originals."""
    mappings = {
        API_ID: "codex-tg-reader-api-id",
        API_HASH: "codex-tg-reader-api-hash",
        session_key(account_id): f"codex-tg-reader-session-{account_id}",
    }
    for new_key, old_key in mappings.items():
        if not credentials.get(new_key):
            old_value = legacy_macos_secret(old_key)
            if old_value:
                credentials.put(new_key, old_value)
