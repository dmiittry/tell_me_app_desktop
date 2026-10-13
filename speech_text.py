"""Prepare pasted text for speech without reading web addresses aloud."""

from __future__ import annotations

import re


MARKDOWN_LINK = re.compile(
    r"(?P<image>!)?\[(?P<label>[^\]\n]+)\]\((?P<url>(?:https?://|www\.)[^\s)]+)\)",
    re.IGNORECASE,
)
BARE_LINK = re.compile(r"(?<![\w@])(?:https?://|www\.)[^\s<>\[\]{}\"']+", re.IGNORECASE)
FENCE = re.compile(r"(?ms)^\s*```.*?^\s*```\s*$")
TRAILING_PUNCTUATION = ".,;:!?)]}»"


def find_links(text: str) -> list[tuple[int, int, str]]:
    """Return non-overlapping link spans and normalized destinations for the editor."""
    links: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in MARKDOWN_LINK.finditer(text):
        start, end = match.span()
        links.append((start, end, _destination(match.group("url"))))
        occupied.append((start, end))
    for match in BARE_LINK.finditer(text):
        start, end = match.span()
        while end > start and text[end - 1] in TRAILING_PUNCTUATION:
            end -= 1
        if end == start or any(start < b and end > a for a, b in occupied):
            continue
        links.append((start, end, _destination(text[start:end])))
    return sorted(links)


def _destination(url: str) -> str:
    return url if url.lower().startswith(("http://", "https://")) else "https://" + url


def prepare_for_speech(text: str) -> str:
    """Replace URLs with a short announcement and lightly clean common Markdown."""
    text = FENCE.sub(" Пропускаю блок кода.\n", text)

    def markdown_replacement(match: re.Match[str]) -> str:
        label = match.group("label").strip()
        if match.group("image"):
            return f" Изображение: {label}. "
        return f" {label}. Это ссылка. "

    text = MARKDOWN_LINK.sub(markdown_replacement, text)

    def bare_replacement(match: re.Match[str]) -> str:
        value = match.group(0)
        trimmed = value.rstrip(TRAILING_PUNCTUATION)
        return " Это ссылка. " + value[len(trimmed):]

    text = BARE_LINK.sub(bare_replacement, text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    text = re.sub(r"(?<!\w)[*_`]+|[*_`]+(?!\w)", "", text)
    text = re.sub(r"\.\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
