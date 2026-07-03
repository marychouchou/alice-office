from __future__ import annotations

import re

# LINE Messaging API hard limits. A text message object is capped at 5000
# characters; Reply/Push calls accept at most 5 message objects. We chunk
# below LINE's hard limit to leave room for the ellipsis truncation marker.
_LINE_SAFE_BUBBLE_CHARS = 4500
_LINE_MAX_MESSAGES_PER_CALL = 5

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_CODE_INLINE_RE = re.compile(r"`([^`]+)`")
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)


def strip_markdown_preserving_urls(text: str) -> str:
    """Strip Markdown that LINE's text bubbles can't render, keeping URLs usable.

    LINE has no Markdown support — bold, italics, code fences, headings, and
    bullet markers all render as literal characters. Bare URLs are auto-linked
    by the client, so `[label](url)` is rewritten to `label (url)` before the
    rest of the syntax is stripped, keeping the link tappable.

    Args:
        text: Raw text that may contain Markdown.

    Returns:
        Text with Markdown syntax removed and URLs preserved.
    """
    if not text:
        return text

    def _unfence(match: re.Match[str]) -> str:
        return match.group(1).rstrip("\n")

    text = _MD_CODE_BLOCK_RE.sub(_unfence, text)
    text = _MD_CODE_INLINE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITAL_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("• ", text)
    return text


def _find_break_point(remaining: str, max_chars: int) -> int:
    """Find the best cut index within `max_chars`, preferring paragraph/line/word breaks.

    Args:
        remaining: Text still to be chunked.
        max_chars: Maximum chunk size to cut within.

    Returns:
        Index to cut at (always greater than 0).
    """
    cut = remaining.rfind("\n\n", 0, max_chars)
    if cut < int(max_chars * 0.5):
        cut = remaining.rfind("\n", 0, max_chars)
    if cut < int(max_chars * 0.5):
        cut = remaining.rfind(" ", 0, max_chars)
    return cut if cut > 0 else max_chars


def _append_truncated(chunks: list[str], remaining: str, max_chars: int) -> list[str]:
    """Append leftover text as a truncated, ellipsis-terminated final chunk.

    Called once the 5-message budget is exhausted but text remains — the
    caller can't add another bubble, so the last chunk absorbs the overflow.

    Args:
        chunks: Chunks accumulated so far.
        remaining: Leftover text that didn't fit in a chunk of its own.
        max_chars: Per-chunk character budget.

    Returns:
        Updated chunks list with a truncated tail.
    """
    if chunks:
        tail = chunks[-1]
        if len(tail) > max_chars - 1:
            tail = tail[: max_chars - 1]
        chunks[-1] = tail.rstrip() + "…"
    else:
        chunks.append(remaining[: max_chars - 1] + "…")
    return chunks


def split_for_line(text: str, max_chars: int = _LINE_SAFE_BUBBLE_CHARS) -> list[str]:
    """Split text into LINE-sized bubbles, preferring paragraph/line breaks.

    Returns at most 5 chunks (LINE's per-call message limit); text that still
    doesn't fit is truncated with an ellipsis on the final chunk so the
    response stays deliverable in a single Reply/Push call.

    Args:
        text: Text to split.
        max_chars: Soft per-chunk character budget.

    Returns:
        List of text chunks, or an empty list if `text` is empty.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining and len(chunks) < _LINE_MAX_MESSAGES_PER_CALL:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            remaining = ""
            break
        cut = _find_break_point(remaining, max_chars)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks = _append_truncated(chunks, remaining, max_chars)
    return chunks


def format_for_line(text: str) -> list[str]:
    """Prepare agent reply text for delivery to LINE: strip Markdown, then chunk.

    Args:
        text: Raw agent reply text.

    Returns:
        List of LINE-ready text chunks; empty if `text` is blank.
    """
    return split_for_line(strip_markdown_preserving_urls(text))
