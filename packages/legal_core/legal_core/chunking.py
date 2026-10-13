"""Article-preserving chunking: an article is one chunk unless it is too long,
in which case it is split only on paragraph/sentence boundaries. Retrieval always
maps chunks back to their parent article."""

import re

_BOUNDARY = re.compile(r"(?<=[\n.؛؟!])\s+")


def chunk_article(text: str, max_chars: int) -> list[tuple[int, str]]:
    """Return (source_offset, chunk_text) pairs covering `text`."""
    if len(text) <= max_chars:
        return [(0, text)]
    pieces: list[tuple[int, str]] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        pieces.append((start, text[start : m.start()]))
        start = m.end()
    pieces.append((start, text[start:]))

    chunks: list[tuple[int, str]] = []
    offset, buf = 0, ""
    for piece_offset, piece in pieces:
        if not piece:
            continue
        if buf and len(buf) + 1 + len(piece) > max_chars:
            chunks.append((offset, buf))
            buf = ""
        if not buf:
            offset = piece_offset
            buf = piece
        else:
            buf = text[offset : piece_offset + len(piece)]
    if buf:
        chunks.append((offset, buf))
    return chunks
