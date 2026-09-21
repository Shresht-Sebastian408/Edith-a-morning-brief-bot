"""Text to speech for the Telegram voice note.

Telegram renders an audio file as a playable voice note (with a waveform, speed
control and auto-transcription) only when it is OGG/OPUS. Anything else arrives
as a file attachment you have to download, which defeats the point. So the
pipeline is: synthesise to MP3, transcode to OPUS with ffmpeg.

Primary engine is edge-tts: free, no API key, no account, and natural Indian
English voices. gTTS is the fallback - more robotic, but it has different
failure modes, which is the whole reason it is here.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_VOICE = "en-IN-PrabhatNeural"  # Indian English, male. Neerja is the female voice.


@dataclass(slots=True)
class Audio:
    data: bytes
    is_opus: bool  # False means MP3, which must be sent via sendAudio, not sendVoice


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def synthesize(text: str, voice: str = DEFAULT_VOICE, rate: str = "+8%") -> Audio:
    """Render `text` to audio, preferring OGG/OPUS.

    Slightly faster than default rate: a morning brief read at normal pace drags.
    """
    mp3 = await _edge(text, voice, rate)
    if mp3 is None:
        mp3 = await asyncio.to_thread(_gtts, text)
    if mp3 is None:
        raise RuntimeError("both edge-tts and gTTS failed to produce audio")

    if not ffmpeg_available():
        log.warning("ffmpeg not found; sending MP3 as an audio file, not a voice note")
        return Audio(data=mp3, is_opus=False)

    opus = await asyncio.to_thread(_to_opus, mp3)
    return Audio(data=opus, is_opus=True) if opus else Audio(data=mp3, is_opus=False)


async def _edge(text: str, voice: str, rate: str) -> bytes | None:
    try:
        import edge_tts
    except ImportError:
        log.warning("edge-tts not installed")
        return None
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        chunks = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.extend(chunk["data"])
        if chunks:
            return bytes(chunks)
        log.warning("edge-tts returned no audio")
    except Exception as exc:  # noqa: BLE001 - unofficial endpoint, can break anytime
        log.warning("edge-tts failed (%s); falling back to gTTS", exc)
    return None


def _gtts(text: str) -> bytes | None:
    try:
        from gtts import gTTS
    except ImportError:
        log.warning("gTTS not installed")
        return None
    try:
        import io

        buf = io.BytesIO()
        gTTS(text=text, lang="en", tld="co.in").write_to_fp(buf)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("gTTS failed: %s", exc)
        return None


def _to_opus(mp3: bytes) -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.mp3"
        dst = Path(tmp) / "out.ogg"
        src.write_bytes(mp3)
        try:
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(src),
                    "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
                    str(dst),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            log.warning("ffmpeg transcode failed: %s %s", exc, stderr[:200])
            return None
        return dst.read_bytes() if dst.exists() else None
