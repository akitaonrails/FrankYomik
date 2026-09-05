"""Reading a rasterised prose page: OCR per column, then furigana.

manga-ocr was trained on manga text, which means roughly one balloon at a
time. A whole column of a novel is far outside that and comes back as
gibberish, so each column is read in short vertical slices instead. The slices
also anchor the result: a chunk's height divided by the characters read from it
gives the pitch actually used, so a dropped character misplaces furigana only
within that chunk rather than sliding the rest of the column.

Readings are produced against the column's full text, not the slice, because
MeCab needs the sentence to choose between readings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from PIL import Image

from .book_layout import PageLayout, TextColumn, column_chunks
from .config import BOOK_GLYPHS_PER_CHUNK

# Glyph edges sit right at the column bounds; a couple of pixels avoids
# clipping them without reaching the ruby in the gutter.
CROP_PAD_PX = 2

OcrFn = Callable[[Image.Image], str]
AnnotateFn = Callable[[str], list[dict]]
ProgressFn = Callable[[int, int], None]


@dataclass(frozen=True)
class Chunk:
    """One OCR slice of a column."""

    y0: int
    y1: int
    text: str


@dataclass(frozen=True)
class Ruby:
    """A reading, and the vertical span of the characters it belongs to."""

    reading: str
    y0: int
    y1: int


@dataclass(frozen=True)
class ColumnReading:
    """Everything read from one column of the page."""

    index: int
    column: TextColumn
    chunks: tuple[Chunk, ...]
    text: str
    ruby: tuple[Ruby, ...]


def character_spans(chunks: Sequence[Chunk]) -> list[tuple[int, int]]:
    """Vertical span of every character, in reading order down the column."""
    spans: list[tuple[int, int]] = []
    for chunk in chunks:
        count = len(chunk.text)
        if count == 0:
            continue
        step = (chunk.y1 - chunk.y0) / count
        for i in range(count):
            spans.append((int(chunk.y0 + i * step), int(chunk.y0 + (i + 1) * step)))
    return spans


def _ruby_for(text: str, spans: Sequence[tuple[int, int]],
              segments: Sequence[dict]) -> list[Ruby]:
    """Attach each reading to the span of the characters it annotates.

    Segments are located by search rather than by accumulating lengths:
    annotate drops whitespace-only morphemes, and a running offset would
    silently slide every reading after one.
    """
    ruby: list[Ruby] = []
    cursor = 0
    for segment in segments:
        surface = segment.get("text") or ""
        if not surface:
            continue
        start = text.find(surface, cursor)
        if start < 0:
            continue
        cursor = start + len(surface)
        reading = segment.get("furigana")
        if not reading:
            continue
        covered = spans[start:cursor]
        if not covered:
            continue
        ruby.append(Ruby(reading=reading, y0=covered[0][0], y1=covered[-1][1]))
    return ruby


def read_columns(img: Image.Image,
                 layout: PageLayout,
                 ocr: OcrFn,
                 annotate: AnnotateFn,
                 progress: ProgressFn | None = None) -> list[ColumnReading]:
    """Read every column of a prose page and work out where furigana goes."""
    readings: list[ColumnReading] = []
    total = len(layout.columns)

    for index, column in enumerate(layout.columns):
        chunks: list[Chunk] = []
        for y0, y1 in column_chunks(column, BOOK_GLYPHS_PER_CHUNK):
            box = (max(0, column.x0 - CROP_PAD_PX), y0,
                   min(layout.width, column.x1 + CROP_PAD_PX), y1)
            text = (ocr(img.crop(box)) or "").strip()
            if text:
                chunks.append(Chunk(y0=y0, y1=y1, text=text))

        if progress:
            progress(index + 1, total)
        if not chunks:
            continue

        column_text = "".join(chunk.text for chunk in chunks)
        readings.append(ColumnReading(
            index=index,
            column=column,
            chunks=tuple(chunks),
            text=column_text,
            ruby=tuple(_ruby_for(column_text, character_spans(chunks),
                                 annotate(column_text))),
        ))

    return readings
