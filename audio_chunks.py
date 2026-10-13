"""Text boundaries and a single, seekable PCM recording."""

import re
import wave
from pathlib import Path

SAMPLE_RATE = 24000


def split_text(text: str, max_words: int = 28, max_chars: int = 240) -> list[str]:
    """Keep every word, prefer sentence boundaries and bound long sentences."""
    if max_words < 1 or max_chars < 1:
        raise ValueError("Chunk limits must be positive")
    sentences = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    chunks, current = [], []
    for sentence in sentences:
        words = sentence.split()
        if current and (len(current) + len(words) > max_words or
                        len(" ".join(current + words)) > max_chars):
            chunks.append(" ".join(current))
            current = []
        for word in words:
            if current and (len(current) >= max_words or
                            len(" ".join(current)) + len(word) + 1 > max_chars):
                chunks.append(" ".join(current))
                current = []
            current.append(word)
    if current:
        chunks.append(" ".join(current))
    return chunks


def wave_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        if not frames:
            raise ValueError("Empty recording")
        if path.stat().st_size < frames * audio.getnchannels() * audio.getsampwidth() + 44:
            raise ValueError("Incomplete recording")
        return frames / audio.getframerate()


def open_recording(path: Path) -> wave.Wave_write:
    audio = wave.open(str(path), "wb")
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(SAMPLE_RATE)
    return audio


def append_chunk(output: wave.Wave_write, path: Path) -> None:
    with wave.open(str(path), "rb") as chunk:
        if (chunk.getnchannels(), chunk.getsampwidth(), chunk.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("Unexpected audio format")
        while frames := chunk.readframes(65536):
            output.writeframesraw(frames)
