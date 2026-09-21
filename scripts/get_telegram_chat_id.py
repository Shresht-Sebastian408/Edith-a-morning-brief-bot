"""Print your Telegram chat id.

    1. Create a bot: message @BotFather, send /newbot, copy the token into .env
    2. Open a chat with your new bot and send it any message (this is required -
       bots cannot message you first).
    3. python scripts/get_telegram_chat_id.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from brief.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.telegram_bot_token:
        print("Set TELEGRAM_BOT_TOKEN in .env first.", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
    payload = httpx.get(url, timeout=30).json()

    if not payload.get("ok"):
        print(f"Telegram error: {payload.get('description')}", file=sys.stderr)
        return 1

    chats = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            name = chat.get("username") or chat.get("first_name") or chat.get("title")
            chats[chat["id"]] = f"{name} ({chat.get('type')})"

    if not chats:
        print(
            "No messages found. Send your bot a message, then run this again.\n"
            "(Telegram only retains recent updates, so send one now.)",
            file=sys.stderr,
        )
        return 1

    print("Add the id you want to .env as TELEGRAM_CHAT_ID:\n")
    for chat_id, label in chats.items():
        print(f"  TELEGRAM_CHAT_ID={chat_id}   # {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
