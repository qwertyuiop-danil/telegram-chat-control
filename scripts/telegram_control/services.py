"""User-scoped background service adapters for macOS, Linux, and Windows."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any

from . import accounts
from .client import handle_deleted, ingest_event, load_telethon, reconcile_deletions, store, sync_index, telegram_client
from .credentials import Credentials
from .store import Store, now


LABEL = "com.openai.telegram-chat-control"
LINUX_UNIT = "telegram-chat-control.service"
WINDOWS_TASK = "Codex Telegram Chat Control"


def entry_command() -> list[str]:
    """Use the current interpreter, which `uv run` has already prepared."""
    entrypoint = Path(__file__).resolve().parents[1] / "telegram.py"
    return [sys.executable, str(entrypoint), "service", "run"]


def _quoted_command() -> str:
    return subprocess.list2cmdline(entry_command())


def launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def systemd_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "systemd" / "user" / LINUX_UNIT


def launchd_plist() -> str:
    arguments = "".join(f"<string>{part}</string>" for part in entry_command())
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{arguments}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
"""


def systemd_unit() -> str:
    return f"""[Unit]
Description=Codex Telegram Chat Control
After=network-online.target

[Service]
Type=simple
ExecStart={_quoted_command()}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"Service command failed: {' '.join(command)}")


def install() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        path = launchd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(), encoding="utf-8")
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
    elif system == "Linux":
        if not shutil.which("systemctl"):
            raise RuntimeError("systemd user services are unavailable; run `service run` under your supervisor.")
        path = systemd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", LINUX_UNIT])
    elif system == "Windows":
        _run(["schtasks", "/Create", "/TN", WINDOWS_TASK, "/SC", "ONLOGON", "/TR", _quoted_command(), "/F"])
        _run(["schtasks", "/Run", "/TN", WINDOWS_TASK])
        path = None
    else:
        raise RuntimeError(f"Unsupported platform: {system}")
    return {"status": "installed", "platform": system, "definition_path": str(path) if path else None}


def start() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"])
    elif system == "Linux":
        _run(["systemctl", "--user", "start", LINUX_UNIT])
    elif system == "Windows":
        _run(["schtasks", "/Run", "/TN", WINDOWS_TASK])
    else:
        raise RuntimeError(f"Unsupported platform: {system}")
    return {"status": "started"}


def stop() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        _run(["launchctl", "kill", "SIGTERM", f"gui/{os.getuid()}/{LABEL}"])
    elif system == "Linux":
        _run(["systemctl", "--user", "stop", LINUX_UNIT])
    elif system == "Windows":
        _run(["schtasks", "/End", "/TN", WINDOWS_TASK])
    else:
        raise RuntimeError(f"Unsupported platform: {system}")
    return {"status": "stopped"}


def uninstall() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        path = launchd_path()
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True, check=False)
        path.unlink(missing_ok=True)
    elif system == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", LINUX_UNIT], capture_output=True, check=False)
        path = systemd_path()
        path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    elif system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"], capture_output=True, check=False)
        path = None
    else:
        raise RuntimeError(f"Unsupported platform: {system}")
    return {"status": "uninstalled", "definition_path": str(path) if path else None}


def status(root: Path) -> dict[str, Any]:
    state: dict[str, Any] = {}
    try:
        account = accounts.active(root)
        with store(root, account["id"]).connect(write=False) as db:
            state = Store.state(db)
    except RuntimeError:
        account = None
    heartbeat = state.get("heartbeat")
    age: float | None = None
    if heartbeat:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat["value"]["at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            pass
    return {
        "platform": platform.system(), "active_account": account["id"] if account else None,
        "heartbeat_age_seconds": age, "service_state": state, "credentials": Credentials(root).status(),
    }


async def run_daemon(root: Path) -> None:
    """Run until interrupted; restart the Telethon connection when active account changes."""
    load_telethon()
    from telethon import events

    while True:
        account = accounts.active(root)
        account_id = account["id"]
        current = telegram_client(root, account_id)
        index_lock = asyncio.Lock()

        async def on_message(event: Any) -> None:
            async with index_lock:
                await ingest_event(root, event)

        async def on_deleted(event: Any) -> None:
            async with index_lock:
                await handle_deleted(root, event)

        current.add_event_handler(on_message, events.NewMessage)
        current.add_event_handler(on_message, events.MessageEdited)
        current.add_event_handler(on_deleted, events.MessageDeleted)
        try:
            await current.connect()
            await current.catch_up()
            with store(root, account_id).connect() as db:
                Store.set_state(db, "heartbeat", {"at": now(), "account_id": account_id, "pid": os.getpid(), "phase": "connected"})
            last_audit = asyncio.get_running_loop().time()
            while accounts.active(root)["id"] == account_id:
                current_time = asyncio.get_running_loop().time()
                async with index_lock:
                    await sync_index(root, current=current, dialog_limit=100)
                    with store(root, account_id).connect() as db:
                        Store.set_state(db, "heartbeat", {"at": now(), "account_id": account_id, "pid": os.getpid(), "phase": "synced"})
                    if current_time - last_audit >= 300:
                        await reconcile_deletions(root, current=current)
                        last_audit = current_time
                await asyncio.sleep(5)
        finally:
            await current.disconnect()
