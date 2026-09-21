"""Telegram delivery.

HTML parse mode, not MarkdownV2. MarkdownV2 requires escaping sixteen different
characters anywhere they appear, and a single unescaped '.' or '-' in a subject
line makes the whole send fail with a 400. HTML needs three escapes and is far
harder to break with arbitrary email content.
"""

from __future__ import annotations

import html
import logging

import httpx

from brief.config import Settings
from brief.contracts import Brief
from brief.delivery.tts import Audio

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4096  # Telegram's hard limit; longer sends are rejected outright.


class TelegramDelivery:
    name = "telegram"

    def __init__(self, settings: Settings) -> None:
        settings.require("telegram_bot_token", "telegram_chat_id")
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id

    async def send(self, brief: Brief, audio: Audio | None = None) -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            for chunk in _chunk(render_html(brief)):
                await self._call(
                    client,
                    "sendMessage",
                    data={
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "true",
                    },
                )

            if audio is not None:
                method = "sendVoice" if audio.is_opus else "sendAudio"
                field = "voice" if audio.is_opus else "audio"
                filename = "brief.ogg" if audio.is_opus else "brief.mp3"
                mime = "audio/ogg" if audio.is_opus else "audio/mpeg"
                await self._call(
                    client,
                    method,
                    data={"chat_id": self.chat_id},
                    files={field: (filename, audio.data, mime)},
                )

    async def _call(self, client: httpx.AsyncClient, method: str, **kwargs) -> dict:
        response = await client.post(API.format(token=self.token, method=method), **kwargs)
        payload = response.json()
        if not payload.get("ok"):
            # Telegram puts the useful reason in the body, not the status line.
            raise RuntimeError(
                f"Telegram {method} failed: {payload.get('description', response.text)}"
            )
        log.info("telegram %s ok", method)
        return payload


def render_html(brief: Brief) -> str:
    e = html.escape
    parts = [f"<b>{e(brief.greeting)}</b>"]

    # Only claim a quiet morning when there is genuinely nothing to show. If a
    # section exists (including a failure notice), it speaks for itself.
    if brief.is_quiet_day() and not brief.text_sections:
        parts.append("\nNothing needs you this morning.")

    for section in brief.text_sections:
        parts.append(f"\n<b>{e(section.title)}</b>")
        parts.extend(f"• {e(bullet)}" for bullet in section.bullets)

    return "\n".join(parts).strip()


def _chunk(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line boundaries so a bullet is never cut mid-word."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            # A single line longer than the limit is hard-split as a last resort.
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


__all__ = ["TelegramDelivery", "render_html"]
