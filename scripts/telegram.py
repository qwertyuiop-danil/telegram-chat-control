# /// script
# requires-python = ">=3.10"
# dependencies = ["telethon>=1.42,<2", "qrcode[pil]>=8,<9", "filelock>=3.16,<4", "keyring>=25,<26"]
# ///
"""Entry point for the cross-platform Telegram chat-control skill.

Run with: uv run scripts/telegram.py <command>
"""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_control.cli import main


if __name__ == "__main__":
    main()
